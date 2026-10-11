"""Fault-injection stand-in for `claude -p ... --output-format json`. No model calls.

Each call pops one mode from the file named by $FAKE_CLAUDE_MODES (space-separated; default "ok")
and, if $FAKE_CLAUDE_LOG is set, appends {argv, env, stdin_devnull, mode} to it as one JSON line.
Pass both through ask_claude(extra_env=...): the real env is allowlisted.

Modes:
  ok                 first legal move (sorted UCI) of the prompt's "FEN: ..." line, else e2e4
  move:<text>        structured {"move": <text>} (illegal, SAN, garbage: any string)
  text:<text>        plain result text, no structured output
  empty              success with empty result and no structured output
  slow:<s>           sleep s seconds, then behave as ok
  hang               spawn a grandchild (FAKE_GRANDCHILD_MARKER) and sleep 300 s
  garbage            stderr "Segmentation fault", exit 139, no JSON
  usage_limit[:s]    is_error=true, subtype=success, "usage limit reached|<now+s>" (default s=2)
  usage_far          same with a reset 5 h away
  usage_clock        "5-hour limit reached - resets 3am" (no epoch)
  rate_limit         api_error_status 429
  overloaded         api_error_status 529, "Overloaded"
  api_error          is_error=true, subtype=success, api_error_status 500
  model_400          is_error=true, subtype=success, 400 "does not support this model"
  auth               plain "Invalid API key - Please run /login", exit 1
  auth_json          api_error_status 401
  max_turns          subtype error_max_turns (the model's own failure, not infra)

Use make_cli(dir) to get an executable wrapper that runs this file with the current interpreter.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ALIASES = {"haiku": "claude-haiku-4-5-20251001", "opus": "claude-opus-4-1-20250805", "sonnet": "claude-sonnet-4-5"}


def make_cli(directory) -> str:
    """Write an executable `claude` wrapper in `directory` that runs this fake with sys.executable."""
    p = Path(directory) / "claude"
    p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n')
    p.chmod(0o755)
    return str(p)


def pop_mode() -> str:
    path = os.environ.get("FAKE_CLAUDE_MODES")
    if not path:
        return "ok"
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        modes = f.read().split()
        f.seek(0)
        f.truncate()
        f.write(" ".join(modes[1:]))
    return modes[0] if modes else "ok"


def stdin_is_devnull() -> bool:
    try:
        a, b = os.fstat(0), os.stat("/dev/null")
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        return True  # closed stdin is fine too


def arg(name, default=None):
    a = sys.argv[1:]
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def first_legal(prompt: str) -> str:
    m = re.search(r"FEN: (.+)", prompt)
    if not m:
        return "e2e4"
    try:
        import chess
    except ImportError:
        return "e2e4"
    moves = sorted(mv.uci() for mv in chess.Board(m[1].strip()).legal_moves)
    return moves[0] if moves else "0000"


def emit(model, result="", structured=None, **kw):
    out = {"type": "result", "subtype": "success", "is_error": False, "result": result,
           "usage": {"input_tokens": 400, "output_tokens": 40}, "total_cost_usd": 0.0001,
           "modelUsage": {model: {"inputTokens": 400, "outputTokens": 40}}}
    if structured is not None:
        out["structured_output"] = structured
    out.update(kw)
    print(json.dumps(out), flush=True)


def main():
    mode = pop_mode()
    prompt = arg("-p", "")
    model = ALIASES.get(arg("--model", "haiku"), arg("--model", "haiku"))
    if os.environ.get("FAKE_CLAUDE_LOG"):
        with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "stdin_devnull": stdin_is_devnull(),
                                "mode": mode}) + "\n")
    name, _, val = mode.partition(":")
    now = int(time.time())
    if name == "slow":
        time.sleep(float(val or 1))
        name = "ok"
    if name == "ok":
        mv = first_legal(prompt)
        emit(model, f"I play {mv}", {"move": mv, "reason": "first legal move"})
    elif name == "move":
        emit(model, f"I play {val}", {"move": val, "reason": "fake"})
    elif name == "text":
        emit(model, val)
    elif name == "empty":
        emit(model, "")
    elif name == "hang":
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "FAKE_GRANDCHILD_MARKER"])
        time.sleep(300)
    elif name == "garbage":
        sys.stderr.write("Segmentation fault: 11\n")
        sys.exit(139)
    elif name == "usage_limit":
        emit(model, f"Claude AI usage limit reached|{now + int(val or 2)}", is_error=True)
    elif name == "usage_far":
        emit(model, f"Claude AI usage limit reached|{now + 5 * 3600}", is_error=True)
    elif name == "usage_clock":
        emit(model, "5-hour limit reached - resets 3am", is_error=True)
    elif name == "rate_limit":
        emit(model, "API Error: 429 rate_limit_error", is_error=True, api_error_status=429)
    elif name == "overloaded":
        emit(model, "API Error: 529 Overloaded", is_error=True, api_error_status=529)
    elif name == "api_error":
        emit(model, "API Error: 500 Internal server error", is_error=True, api_error_status=500)
    elif name == "model_400":
        emit(model, "API Error: 400 Claude Code 2.1.236 does not support this model; version 2.1.280 or newer "
                    "is required", is_error=True, api_error_status=400)
    elif name == "auth":
        print("Invalid API key · Please run /login", flush=True)
        sys.exit(1)
    elif name == "auth_json":
        emit(model, "API Error: 401 authentication_error", is_error=True, api_error_status=401)
    elif name == "max_turns":
        emit(model, "", subtype="error_max_turns", is_error=True)
    else:
        sys.stderr.write(f"fake_claude: unknown mode {mode}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
