"""Run rsi.run unattended: one heavy run per machine, no sleep, restart on crash, kill on stall, wait out limits.

  python -m rsi.supervise --run ID [--detach] [--stall 900] [-- <command ...>]

- takes the RunLock (~/.bonsai/heavy.lock): a second supervisor is refused; the kernel frees it if this dies
- `caffeinate -dimsu -w <this pid>` keeps the Mac awake for exactly as long as the supervisor lives
- the command (default: uv run ... python -m rsi.run --run ID) runs in its own session, detached from any terminal/server.py
- watchdog: if runs/rsi/<ID>/events.jsonl does not change for --stall seconds, the child's whole tree is killed
  and restarted (run.py resumes from state.json; at most the current move is bought again)
- exit 0 = done; 75 = pause (sleep until the reset epoch in the last "pause" event, else --pause s), then retry;
  77 = fatal (auth, changed config): stop; anything else / stall = restart after 30 / 120 / 600 s
- every decision goes to runs/rsi/<ID>/supervise.jsonl; the child's output to runs/rsi/<ID>/run.log
--detach starts the supervisor itself in a new session (survives the terminal) and prints its pid.
--args passes extra arguments to the default command, e.g. --args "--generations 8".
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from rsi import reliable

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs" / "rsi"
EXIT_RETRY, EXIT_FATAL = 75, 77
BACKOFF = (30, 120, 600)
STALL = 900
PAUSE = 600
HEALTHY_S = 600  # a child that ran this long resets the crash backoff
UV = shutil.which("uv") or "/opt/homebrew/bin/uv"


def py_cmd(*args: str) -> list[str]:
    """python with python-chess, through uv (a `uv run` interpreter lives in a temp env that vanishes when its uv
    exits, so a detached process must not reuse sys.executable)."""
    if os.path.exists(UV):
        return [UV, "run", "-q", "--no-project", "--with", "chess", "python", *args]
    return [sys.executable, *args]


def descendants(pid: int) -> list[int]:
    """Every live descendant of pid (children first), found through ps so other sessions are included."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            found.append(c)
            todo.append(c)
    return found


def ancestors(pid: int | None = None) -> list[int]:
    """Parent, grandparent, ... of pid (this process by default)."""
    out, p = [], pid or os.getpid()
    for _ in range(20):
        try:
            p = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(p)], capture_output=True, text=True,
                                   timeout=5).stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            break
        if p <= 1:
            break
        out.append(p)
    return out


def kill_tree(pid: int, include_root: bool = True, sig: int = signal.SIGKILL) -> None:
    """Kill pid's descendants (and their process groups when they lead one, e.g. claude calls started with
    start_new_session), then pid itself."""
    targets = descendants(pid) + ([pid] if include_root else [])
    for p in targets:
        try:
            if os.getpgid(p) == p and p != os.getpgid(0):
                os.killpg(p, sig)
            else:
                os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def last_pause(events: Path, since: float) -> dict | None:
    """The newest "pause" event written after `since` (run.py writes one before exiting 75)."""
    rows = [e for e in reliable.read_jsonl(events) if e.get("kind") == "pause" and (e.get("t") or 0) >= since]
    return rows[-1] if rows else None


def pause_wait(ev: dict | None, default: float = PAUSE, now: float | None = None, margin: float = 60) -> float:
    """Seconds to sleep after exit 75: until the reset epoch + margin if one is known (and < 12 h away)."""
    now = time.time() if now is None else now
    reset = (ev or {}).get("reset_at")
    if reset and 0 < reset - now < 12 * 3600:
        return reset - now + margin
    return default


class Supervisor:
    def __init__(self, cmd: list[str], run_dir: Path, *, lock: Path | str = reliable.HEAVY_LOCK, stall: float = STALL,
                 backoff=BACKOFF, pause: float = PAUSE, max_restarts: int = 50, poll: float = 5.0,
                 caffeinate: bool = True, env: dict | None = None, reset_margin: float = 60):
        self.cmd, self.dir = cmd, Path(run_dir)
        self.events, self.log = self.dir / "events.jsonl", self.dir / "supervise.jsonl"
        self.lock = reliable.RunLock(lock, " ".join(cmd)[:200])
        self.stall, self.backoff, self.pause, self.max_restarts, self.poll = stall, tuple(backoff), pause, \
            max_restarts, poll
        self.caffeinate, self.env, self.child, self.awake = caffeinate, env, None, None
        self.reset_margin = reset_margin

    def note(self, kind: str, **d) -> None:
        reliable.append_jsonl(self.log, {"t": time.time(), "kind": kind, "pid": os.getpid(), **d})
        print(f"supervise: {kind} {d}", flush=True)

    def stop_child(self, grace: float = 20) -> None:
        c = self.child
        if c is None or c.poll() is not None:
            return
        tree = descendants(c.pid)
        try:
            os.killpg(c.pid, signal.SIGTERM)  # run.py kills its own claude/stockfish children on SIGTERM
            c.wait(grace)
        except subprocess.TimeoutExpired:
            pass
        except ProcessLookupError:
            pass
        kill_tree(c.pid)
        for p in tree:  # anything that was reparented while we waited
            try:
                os.killpg(p, signal.SIGKILL) if os.getpgid(p) == p else os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            c.wait(5)
        except subprocess.TimeoutExpired:
            pass

    def _idle(self, started: float) -> float:
        try:
            last = self.events.stat().st_mtime
        except OSError:
            last = 0.0
        return time.time() - max(last, started)

    def run(self) -> int:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.lock.acquire():
            self.note("refused", holder=self.lock.holder())
            print(f"supervise: another run holds {self.lock.path}: {self.lock.holder()}", file=sys.stderr)
            return 2
        if self.caffeinate and sys.platform == "darwin":
            self.awake = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())], stdin=subprocess.DEVNULL)

        def on_signal(signum, _f):
            raise reliable.Stopped(f"signal {signum}")
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        crashes, restarts, rc_final = 0, 0, 1
        env = {**os.environ, **(self.env or {}), "RSI_SUPERVISED": "1"}
        try:
            while True:
                with open(self.dir / "run.log", "a") as out:
                    self.child = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                                  start_new_session=True, cwd=ROOT, env=env)
                started = time.time()
                self.note("start", child=self.child.pid, restart=restarts, cmd=self.cmd)
                rc = None
                while rc is None:
                    time.sleep(self.poll)
                    rc = self.child.poll()
                    if rc is None and self._idle(started) > self.stall:
                        self.note("stall", idle_s=round(self._idle(started)), child=self.child.pid)
                        self.stop_child(grace=10)
                        rc = "stall"
                ran = time.time() - started
                kill_tree(self.child.pid, include_root=False)  # no orphan survives a finished run either
                if rc == 0:
                    self.note("done", ran_s=round(ran))
                    rc_final = 0
                    break
                if rc == EXIT_FATAL:
                    self.note("fatal", rc=rc, ran_s=round(ran))
                    rc_final = EXIT_FATAL
                    break
                restarts += 1
                if restarts > self.max_restarts:
                    self.note("give_up", rc=rc, restarts=restarts)
                    break
                if rc == EXIT_RETRY:
                    ev = last_pause(self.events, started - 1)
                    wait = pause_wait(ev, self.pause, margin=self.reset_margin)
                    self.note("pause", rc=rc, wait_s=round(wait, 1), reason=(ev or {}).get("reason"),
                              reset_at=(ev or {}).get("reset_at"))
                else:
                    crashes = 1 if ran > HEALTHY_S else crashes + 1
                    wait = self.backoff[min(crashes, len(self.backoff)) - 1]
                    self.note("restart_wait", rc=rc, wait_s=wait, ran_s=round(ran))
                time.sleep(wait)
        except reliable.Stopped as e:
            self.note("stop", why=str(e))
            rc_final = 143
        finally:
            self.stop_child()
            if self.awake and self.awake.poll() is None:
                self.awake.terminate()
            self.lock.release()
        return rc_final


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, help="RUN_ID (runs/rsi/<RUN_ID>/)")
    ap.add_argument("--root", default=str(RUNS))
    ap.add_argument("--lock", default=str(reliable.HEAVY_LOCK))
    ap.add_argument("--stall", type=float, default=STALL)
    ap.add_argument("--pause", type=float, default=PAUSE, help="sleep after exit 75 when no reset time is known")
    ap.add_argument("--reset-margin", type=float, default=60, help="extra sleep past a usage-limit reset")
    ap.add_argument("--backoff", default=",".join(map(str, BACKOFF)))
    ap.add_argument("--max-restarts", type=int, default=50)
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--no-caffeinate", action="store_true")
    ap.add_argument("--detach", action="store_true", help="start in a new session and return at once")
    ap.add_argument("--args", default="", help='extra rsi.run arguments, e.g. "--generations 8" or "--seed-only"')
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command (default: python -m rsi.run --run ID ARGS)")
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    cmd = cmd or py_cmd("-m", "rsi.run", "--run", a.run, "--root", a.root, *shlex.split(a.args))
    run_dir = Path(a.root) / a.run
    if a.detach:
        run_dir.mkdir(parents=True, exist_ok=True)
        argv2 = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--detach"]
        with open(run_dir / "supervise.out", "a") as out:
            p = subprocess.Popen(py_cmd("-m", "rsi.supervise", *argv2), stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True, cwd=ROOT)
        print(f"supervisor pid {p.pid}; log {run_dir / 'supervise.jsonl'}")
        return 0
    sup = Supervisor(cmd, run_dir, lock=a.lock, stall=a.stall, pause=a.pause,
                     backoff=[float(x) for x in a.backoff.split(",")], max_restarts=a.max_restarts, poll=a.poll,
                     caffeinate=not a.no_caffeinate, reset_margin=a.reset_margin)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
