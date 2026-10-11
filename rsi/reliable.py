"""Reliability primitives for long unattended RSI runs.

  ask_claude()       one model call: allowlisted env, no stdin, own process group, hard timeout,
                     errors classified (never scored as chess), retries with backoff
  atomic_write_json  crash-safe file write (tmp + fsync + rename)
  append_jsonl       multi-process-safe JSONL append (flock + O_APPEND + fsync, heals a torn tail)
  read_jsonl         reader that skips a torn or garbage line
  RunLock            single-instance lock on ~/.bonsai/heavy.lock (flock: freed by the kernel on death)
  Stopped            raised by the SIGTERM/SIGINT handler; a BaseException so no retry loop swallows it
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
RUN_JSON = HERE / "run.json"
HEAVY_LOCK = Path.home() / ".bonsai" / "heavy.lock"


def run_config(path: Path | str = RUN_JSON) -> dict:
    """The frozen run config (rsi/run.json, or the copy saved under runs/rsi/<RUN_ID>/)."""
    return json.loads(Path(path).read_text())


# Pinned by absolute path: a stale ~/.local/bin/claude 2.0.37 is also on PATH.
CLAUDE = os.environ.get("BONSAI_CLAUDE") or "/opt/homebrew/bin/claude"

# Retry waits between attempts (seconds). Tests shrink these.
BACKOFF = (5, 20, 60)
# A usage limit whose reset is at most this far away is slept through; further away it is raised,
# so the run exits 75 and the supervisor sleeps until the reset.
USAGE_WAIT_MAX = 900

KINDS = ("ok", "timeout", "cli_crash", "api_error", "rate_limit", "usage_limit", "auth")

# Only these variables reach `claude -p`. Everything a parent session exports (CLAUDE_EFFORT=xhigh,
# CLAUDECODE, session ids, ANTHROPIC_*) is dropped, so the call behaves the same from a terminal,
# server.py or a Claude Code Bash tool. Auth comes from the keychain via HOME/USER.
ENV_ALLOW = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
             "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")

# Light, isolated call (all checked against `claude --help` 2.1.236):
#   --safe-mode               no user hooks (caveman, cavemem), plugins, skills listing or CLAUDE.md
#   --tools ""                no tool definitions: ~40k -> ~1.6k input tokens per call
#   --no-session-persistence  no transcript per call in ~/.claude/projects
#   --strict-mcp-config + empty --mcp-config   no MCP servers at all
LIGHT_FLAGS = ["--safe-mode", "--tools", "", "--no-session-persistence", "--disable-slash-commands",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']


class InfraError(Exception):
    """Something other than chess went wrong. Never scored as a loss: retried, or the game is void."""

    def __init__(self, kind: str, detail: str = "", reset_at: float | None = None, retryable: bool = True):
        super().__init__(f"{kind}: {detail[:300]}")
        self.kind, self.detail, self.reset_at, self.retryable = kind, detail, reset_at, retryable


class Stopped(BaseException):
    """Raised by the SIGTERM/SIGINT handler. A BaseException on purpose: no retry loop may swallow it."""


def install_stop_handlers() -> None:
    """Turn SIGTERM/SIGINT into Stopped, so `finally` blocks write their records and kill children."""
    def stop(signum, _frame):
        raise Stopped(f"signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def clean_env(think_tokens: int = 0, extra: dict | None = None) -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    env.update({
        "MAX_THINKING_TOKENS": str(int(think_tokens)),  # 0 = no extended thinking (the 14-110 s calls)
        "CLAUDE_CODE_MAX_RETRIES": "2",                 # fail fast inside the CLI; we retry outside
        "API_TIMEOUT_MS": "60000",                      # one HTTP request may not hang for 10 minutes
        "DISABLE_AUTOUPDATER": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    env.update(extra or {})
    return env


def call_timeout(think_tokens: int = 0) -> float:
    """Per-call timeout: 60 s plus 1 s per 50 thinking tokens allowed."""
    return 60 + think_tokens / 50


def kill_group(p: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the whole process group, so no grandchild survives."""
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            p.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_group(cmd: list[str], timeout: float, env: dict | None = None, cwd=None) -> tuple[int, str, str]:
    """subprocess.run with stdin=DEVNULL and a timeout that kills the whole process group."""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env, cwd=cwd, start_new_session=True)
    except OSError as e:  # ENOENT / EACCES: the binary moved (brew upgrade mid-run)
        raise InfraError("cli_crash", f"cannot start {cmd[0]}: {e}") from None
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(p)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise InfraError("timeout", f"no reply in {timeout:.0f}s") from None
    except BaseException:  # Stopped, KeyboardInterrupt: never leave the call running behind us
        kill_group(p)
        raise
    kill_group(p)  # reap anything the CLI left in its group
    return p.returncode, out, err


_USAGE = re.compile(r"usage limit|limit reached|out of (extra )?usage|(5-hour|weekly|session) limit", re.I)
_RATE = re.compile(r"rate.?limit|\b429\b|\b529\b|overloaded", re.I)
_AUTH = re.compile(r"/login|log ?in|authenticat|oauth|invalid api key|\b401\b|\b403\b|credit balance", re.I)
_EPOCH = re.compile(r"\|(\d{10})\b")  # "Claude AI usage limit reached|<reset epoch>"
_CLOCK = re.compile(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)  # "resets 3pm", "resets at 14:30"


def reset_epoch(text: str, now: float | None = None) -> float | None:
    """The usage-limit reset time in a CLI message, or None if it gives none."""
    now = time.time() if now is None else now
    if m := _EPOCH.search(text):
        return float(m[1])
    if m := _CLOCK.search(text):
        h, mi, ap = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
        if ap:
            h = h % 12 + (12 if ap == "pm" else 0)
        if h > 23 or mi > 59:
            return None
        t = datetime.fromtimestamp(now).replace(hour=h, minute=mi, second=0, microsecond=0)
        if t.timestamp() <= now:
            t += timedelta(days=1)
        return t.timestamp()
    return None


def _from_text(text: str, default: str) -> InfraError:
    if _USAGE.search(text):
        return InfraError("usage_limit", text, reset_at=reset_epoch(text))
    if _RATE.search(text):
        return InfraError("rate_limit", text)
    if _AUTH.search(text):
        return InfraError("auth", text, retryable=False)
    return InfraError(default, text)


def classify(rc: int, out: str, err: str) -> dict:
    """Return the CLI's JSON result, or raise InfraError with a kind the run can act on.

    The CLI may report a failure with is_error=true while subtype says "success" (e.g. the 400
    "does not support this model"), so is_error is checked on its own.
    """
    try:
        j = json.loads(out)
        if not isinstance(j, dict):
            raise ValueError
    except ValueError:
        blob = (out + "\n" + err).strip()[-600:]
        e = _from_text(blob, "cli_crash")
        e.detail = f"rc={rc} {e.detail}"
        raise e from None
    text = str(j.get("result") or j.get("error") or j.get("subtype") or "")
    status = j.get("api_error_status")
    if status in (401, 403):
        raise InfraError("auth", f"HTTP {status}: {text[:300]}", retryable=False)
    if status == 429 or status == 529:
        e = _from_text(text, "rate_limit")
        if e.kind not in ("usage_limit", "rate_limit"):
            e = InfraError("rate_limit", f"HTTP {status}: {text[:300]}")
        raise e
    if j.get("subtype") in ("error_max_turns", "error_max_structured_output_retries"):
        return j  # the model's own failure: the caller treats it as an unusable reply, which is chess
    if j.get("is_error") or status:
        e = _from_text(text, "api_error")
        if e.kind == "api_error":
            e.detail = f"HTTP {status}: {text}" if status else text
            e.retryable = status not in (400, 404)  # a bad model id or request will not fix itself
        raise e
    if j.get("subtype") not in ("success", None):
        raise InfraError("api_error", f"subtype={j.get('subtype')} {text}")
    return j


def resolved_model(j: dict) -> str | None:
    """The concrete model id that answered (from modelUsage), e.g. claude-haiku-4-5-20251001."""
    mu = j.get("modelUsage") or {}
    return max(mu, key=lambda k: (mu[k] or {}).get("outputTokens", 0)) if mu else j.get("model")


def ask_claude(prompt: str, system: str = "", model: str = "haiku", *, think_tokens: int = 0,
               schema: dict | str | None = None, timeout: float | None = None, attempts: int = 3,
               cli: str | None = None, extra_env: dict | None = None, cwd=None, log=None) -> dict:
    """One model reply, retried on infra faults with BACKOFF waits.

    Returns {"kind": "ok", "text", "structured", "model", "subtype", "cost", "in_tokens", "out_tokens",
    "wall_s", "attempts"}. Raises InfraError (kind in KINDS) when every attempt fails; auth and bad
    requests are not retried, and a usage limit further away than USAGE_WAIT_MAX is raised at once.
    `log(event_dict)` is called for every failed attempt.
    """
    cmd = [cli or CLAUDE, "-p", prompt, "--model", model, "--output-format", "json", "--max-turns", "1",
           *LIGHT_FLAGS]
    if system:
        cmd += ["--system-prompt", system]
    if schema is not None:
        cmd += ["--json-schema", schema if isinstance(schema, str) else json.dumps(schema)]
    timeout = timeout or call_timeout(think_tokens)
    env = clean_env(think_tokens, extra_env)
    t_start, last = time.time(), None
    for i in range(attempts):
        t0 = time.time()
        try:
            j = classify(*run_group(cmd, timeout, env, cwd))
            u = j.get("usage") or {}
            return {"kind": "ok", "text": j.get("result") or "", "structured": j.get("structured_output"),
                    "model": resolved_model(j), "subtype": j.get("subtype"), "cost": j.get("total_cost_usd") or 0.0,
                    "in_tokens": u.get("input_tokens"), "out_tokens": u.get("output_tokens"),
                    "wall_s": round(time.time() - t0, 2), "attempts": i + 1}
        except InfraError as e:
            last = e
            if log:
                log({"kind": "infra", "what": e.kind, "attempt": i + 1, "wall_s": round(time.time() - t0, 1),
                     "detail": e.detail[-200:]})
            if not e.retryable or i == attempts - 1:
                break
            if e.kind == "usage_limit":
                wait = (e.reset_at - time.time()) if e.reset_at else None
                if wait is None or wait > USAGE_WAIT_MAX:
                    break  # let the supervisor sleep until the reset
                time.sleep(max(0.0, wait) + random.uniform(0, 2))
            else:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)] * random.uniform(1, 1.2))
    last.detail = f"{last.detail} (after {i + 1} attempt(s), {time.time() - t_start:.0f}s)"
    raise last


def cli_version(cli: str | None = None) -> str | None:
    """`claude --version` -> '2.1.236', or None if it cannot run."""
    try:
        rc, out, _ = run_group([cli or CLAUDE, "--version"], 30, clean_env())
    except InfraError:
        return None
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m[0] if rc == 0 and m else None


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path | str, obj, indent=None) -> None:
    """Write JSON so a reader sees the old file or the new one, never a partial one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(obj, indent=indent))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def append_jsonl(path: Path | str, obj) -> None:
    """One whole line per call, even with several writer processes; durable once this returns."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj) + "\n").encode()
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Heal a torn tail left by a crash mid-write, so the new record starts on its own line.
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        os.write(fd, data)
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_jsonl(path: Path | str) -> list:
    """Every whole record; a torn or garbage line is skipped (and reported on stderr), never fatal."""
    p = Path(path)
    try:
        f = open(p, "rb")
    except FileNotFoundError:
        return []
    with f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # no half-appended line from a live writer
        try:
            raw = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    rows, bad = [], 0
    for line in raw.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            bad += 1
    if bad:
        print(f"read_jsonl: skipped {bad} unreadable line(s) in {p}", file=sys.stderr, flush=True)
    return rows


class RunLock:
    """At most one heavy run per lock file. flock is dropped by the kernel when the holder exits or is
    SIGKILLed, so there is no stale lock and no pid-reuse problem (unlike mkdir + pid file)."""

    def __init__(self, path: Path | str = HEAVY_LOCK, note: str = ""):
        self.path, self.note, self.fd = Path(path), note, None

    def acquire(self, wait: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise
        self.fd = fd
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"pid": os.getpid(), "note": self.note, "since": time.time()}).encode(), 0)
        return True

    def held_elsewhere(self) -> bool:
        """True if another process holds the lock (does not take it)."""
        if self.fd is not None:
            return False
        if not self.acquire():
            return True
        self.release()
        return False

    def holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text() or "null")
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        if not self.acquire():
            raise SystemExit(f"another run holds {self.path}: {self.holder()}")
        return self

    def __exit__(self, *a):
        self.release()
