"""Preflight for an unattended RSI run. Prints OK / WARN / FAIL per check; exit 1 on any FAIL.

  python -m rsi.doctor [--call] [--supervised]

Checks: AC power, the pinned CLI (path, version vs run.json, light-call flags), Stockfish version vs ladder.json,
free disk >= 5 GB, ~/.bonsai/heavy.lock free, and server.py's job (active -> WARN, the run drops to 2 workers).
--call adds a haiku canary and an opus canary (about $0.01 together) and prints the resolved model ids.
run.py reuses canary(), stockfish_check() and server_busy() for its per-generation gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from rsi import reliable
from rsi.ladder import LADDER_JSON, STOCKFISH, load_ladder, stockfish_version
from rsi.supervise import ancestors

ROOT = Path(__file__).resolve().parent.parent
FLAGS = ("--safe-mode", "--tools", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
         "--json-schema", "--mcp-config")
SERVER_STATE = "http://127.0.0.1:8765/api/state"
SERVER_LOCK = Path.home() / "helloworld" / ".heavy-lock"


def sh(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, repr(e)


def power() -> tuple[bool, str]:
    rc, out = sh(["pmset", "-g", "batt"])
    if rc != 0:
        return False, f"pmset failed: {out[:100]}"
    return "AC Power" in out, " | ".join(l.strip() for l in out.splitlines()[:2])


def cli_check(cfg: dict) -> list[tuple[str, str, str]]:
    path, want = cfg["cli"]["path"], cfg["cli"]["version"]
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return [("FAIL", "claude CLI", f"{path} missing or not executable")]
    got = reliable.cli_version(path)
    rows = [("OK" if got == want else "FAIL", "claude CLI version", f"{got} at {os.path.realpath(path)} (run.json: {want})")]
    rc, h = sh([path, "--help"], timeout=30)
    missing = [f for f in FLAGS if f not in h]
    rows.append(("FAIL" if missing else "OK", "light-call flags", f"missing {missing}" if missing else "all present"))
    return rows


def canary(model: str, cli: str | None = None, timeout: float = 90, extra_env: dict | None = None) -> dict:
    """One tiny real call. {"ok", "model" (resolved id), "wall_s", "cost", "error", "kind", "reset_at"}."""
    t0 = time.time()
    try:
        r = reliable.ask_claude("Reply with the single word: ok", "You answer in one word.", model, timeout=timeout,
                                attempts=2, cli=cli, extra_env=extra_env)
        return {"ok": True, "model": r["model"], "wall_s": round(time.time() - t0, 1), "cost": r["cost"],
                "text": (r["text"] or "")[:20]}
    except reliable.InfraError as e:
        return {"ok": False, "kind": e.kind, "error": str(e)[:200], "reset_at": e.reset_at,
                "retryable": e.retryable, "wall_s": round(time.time() - t0, 1), "cost": 0.0}


def stockfish_check(path: str = STOCKFISH, ladder_path: Path = LADDER_JSON) -> tuple[bool, str]:
    """Stockfish answers and its version matches ladder.json (the rung Elos were calibrated on it)."""
    want = load_ladder(ladder_path).get("stockfish_version")
    try:
        got = stockfish_version(path)
    except Exception as e:  # noqa: BLE001 - any failure to start is a FAIL
        return False, f"cannot start {path}: {e!r}"[:200]
    return (bool(got) and got == want), f"{got} (ladder.json: {want})"


def disk_free_gb(path: Path = ROOT) -> float:
    return shutil.disk_usage(path).free / 1e9


def server_busy(url: str = SERVER_STATE) -> tuple[bool, str]:
    """Is the user's server.py running a job (its HTTP state, its heavy lock, or live chess agents)?"""
    try:
        job = json.loads(urllib.request.urlopen(url, timeout=2).read()).get("job") or {}
        if job.get("status") == "running":
            return True, f"server.py job running ({job.get('set')})"
    except (OSError, ValueError):
        pass
    owner = SERVER_LOCK / "owner"
    if owner.exists():
        try:
            pid = int(owner.read_text().strip())
            os.kill(pid, 0)
            return True, f"server.py heavy-lock held by live pid {pid}"
        except (OSError, ValueError):
            pass
    rc, out = sh(["pgrep", "-f", "tasks/chess/_agent.py"])
    if rc == 0 and out.strip():
        return True, f"{len(out.split())} tasks/chess/_agent.py process(es) live"
    return False, "idle"


def workers_for(cfg: dict) -> tuple[int, str]:
    busy, why = server_busy()
    return (cfg.get("workers_when_server_busy", 2), why) if busy else (cfg.get("workers", 3), why)


def check(cfg: dict | None = None, call: bool = False, supervised: bool = False) -> list[tuple[str, str, str]]:
    cfg = cfg or reliable.run_config()
    rows = []
    ac, detail = power()
    rows.append(("OK" if ac else ("FAIL" if cfg.get("doctor", {}).get("require_ac", True) else "WARN"), "AC power", detail))
    rows += cli_check(cfg)
    sf_ok, detail = stockfish_check(cfg.get("stockfish", {}).get("path", STOCKFISH))
    rows.append(("OK" if sf_ok else "FAIL", "stockfish version", detail))
    free, need = disk_free_gb(), cfg.get("doctor", {}).get("min_free_gb", 5)
    rows.append(("OK" if free >= need else "FAIL", "free disk", f"{free:.0f} GB (need {need})"))
    lock = reliable.RunLock(Path(os.path.expanduser(cfg.get("paths", {}).get("heavy_lock", str(reliable.HEAVY_LOCK)))))
    held = lock.held_elsewhere()
    holder = lock.holder() or {}
    ours = supervised and holder.get("pid") in ancestors()
    rows.append(("OK" if not held or ours else "FAIL", "heavy lock", f"held by {holder}" if held else f"free ({lock.path})"))
    busy, why = server_busy()
    w = cfg.get("workers_when_server_busy", 2) if busy else cfg.get("workers", 3)
    rows.append(("WARN" if busy else "OK", "server.py job", f"{why} -> run uses {w} workers"))
    if call:
        for role, model in (("haiku canary", cfg["player"]["model"]), ("opus canary", cfg["proposer"]["model"])):
            c = canary(model, cfg["cli"]["path"], timeout=120)
            if not c["ok"]:
                rows.append(("FAIL", role, f"{c['kind']}: {c['error']}"))
                continue
            pinned = role.startswith("haiku") and c["model"] != cfg["player"]["model"]
            rows.append(("FAIL" if pinned else "OK", role, f"resolved {c['model']} in {c['wall_s']}s, ${c['cost']:.4f}"))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--call", action="store_true", help="also make the haiku and opus canary calls")
    ap.add_argument("--supervised", action="store_true", help="the heavy lock may be held by our parent")
    ap.add_argument("--run-json", default=str(reliable.RUN_JSON))
    a = ap.parse_args(argv)
    rows = check(reliable.run_config(a.run_json), call=a.call, supervised=a.supervised)
    for level, name, detail in rows:
        print(f"{level:4} {name:22} {detail}", flush=True)
    levels = [r[0] for r in rows]
    print(f"\n{levels.count('OK')} ok, {levels.count('WARN')} warn, {levels.count('FAIL')} fail")
    return 1 if "FAIL" in levels else 0


if __name__ == "__main__":
    sys.exit(main())
