"""Fault-injection tests for rsi/reliable.py, using fake_claude (zero model calls).

  uv run -q --no-project --with chess --with pytest python -m pytest rsi/tests/test_reliable.py -q
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rsi import reliable
from rsi.tests.fake_claude import make_cli

ROOT = Path(__file__).resolve().parents[2]
FEN_PROMPT = "FEN: rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """ask(modes..., **kw): run ask_claude against the fake CLI with the given per-call modes."""
    cli = make_cli(tmp_path)
    modes, log = tmp_path / "modes.txt", tmp_path / "calls.jsonl"
    monkeypatch.setattr(reliable, "BACKOFF", (0.05, 0.05, 0.05))

    def ask(*m, **kw):
        modes.write_text(" ".join(m))
        kw.setdefault("timeout", 20)
        kw.setdefault("extra_env", {"FAKE_CLAUDE_MODES": str(modes), "FAKE_CLAUDE_LOG": str(log)})
        return reliable.ask_claude(kw.pop("prompt", FEN_PROMPT), "sys", cli=cli, **kw)

    ask.modes, ask.calls = modes, lambda: reliable.read_jsonl(log)
    return ask


def leftovers():
    return subprocess.run(["pgrep", "-f", "FAKE_GRANDCHILD_MARKER"], capture_output=True, text=True).stdout.split()


def infra(fn) -> reliable.InfraError:
    with pytest.raises(reliable.InfraError) as e:
        fn()
    return e.value


def test_ok_returns_move_model_and_clean_call(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://example.invalid")
    r = fake("ok", think_tokens=1024, schema={"type": "object"})
    assert r["kind"] == "ok" and r["structured"]["move"] == "a2a3" and r["attempts"] == 1
    assert r["model"] == "claude-haiku-4-5-20251001"
    (call,) = fake.calls()
    argv, env = call["argv"], call["env"]
    assert call["stdin_devnull"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands", "--safe-mode",
                 "--json-schema"):
        assert flag in argv
    assert env["MAX_THINKING_TOKENS"] == "1024" and env["DISABLE_AUTOUPDATER"] == "1"
    assert "CLAUDE_EFFORT" not in env and "ANTHROPIC_BASE_URL" not in env and "CLAUDECODE" not in env


def test_hang_is_group_killed_then_retried(fake):
    t0 = time.time()
    r = fake("hang", "ok", timeout=3, attempts=2)
    assert r["attempts"] == 2 and time.time() - t0 < 15
    time.sleep(0.5)
    assert not leftovers()


def test_always_hang_is_timeout_with_no_orphans(fake):
    e = infra(lambda: fake("hang", "hang", timeout=2, attempts=2))
    assert e.kind == "timeout"
    time.sleep(0.5)
    assert not leftovers()


def test_cli_crash(fake):
    assert infra(lambda: fake("garbage", "garbage", attempts=2)).kind == "cli_crash"


def test_missing_binary_is_cli_crash(tmp_path):
    e = infra(lambda: reliable.ask_claude("x", cli=str(tmp_path / "nope"), attempts=1))
    assert e.kind == "cli_crash"


def test_is_error_with_subtype_success_is_api_error_and_retried(fake):
    r = fake("api_error", "ok", attempts=2)
    assert r["attempts"] == 2
    assert infra(lambda: fake("api_error", "api_error", attempts=2)).kind == "api_error"


def test_bad_model_400_not_retried(fake):
    e = infra(lambda: fake("model_400", "ok", attempts=3))
    assert e.kind == "api_error" and not e.retryable
    assert fake.modes.read_text().strip() == "ok"


def test_rate_limit_and_overloaded_backoff_then_ok(fake):
    r = fake("rate_limit", "overloaded", "ok", attempts=3)
    assert r["attempts"] == 3
    assert infra(lambda: fake("rate_limit", attempts=1)).kind == "rate_limit"


def test_usage_limit_waits_for_reset_then_ok(fake):
    t0 = time.time()
    r = fake("usage_limit:2", "ok", attempts=2)
    assert r["attempts"] == 2 and time.time() - t0 >= 1


def test_usage_limit_far_away_raised_at_once_with_reset(fake):
    t0 = time.time()
    e = infra(lambda: fake("usage_far", "ok", attempts=3))
    assert e.kind == "usage_limit" and e.reset_at > time.time() + 4 * 3600
    assert time.time() - t0 < 10 and fake.modes.read_text().strip() == "ok"


def test_usage_limit_clock_text_parsed():
    now = time.mktime((2026, 10, 11, 22, 0, 0, 0, 0, -1))
    t = reliable.reset_epoch("5-hour limit reached - resets 3am", now)
    assert time.localtime(t)[:4] == (2026, 10, 12, 3)
    e = infra(lambda: reliable.classify(0, json.dumps({"is_error": True, "subtype": "success",
                                                       "result": "You're out of extra usage"}), ""))
    assert e.kind == "usage_limit" and e.reset_at is None


@pytest.mark.parametrize("mode", ["auth", "auth_json"])
def test_auth_not_retried(fake, mode):
    e = infra(lambda: fake(mode, "ok", attempts=3))
    assert e.kind == "auth"
    assert fake.modes.read_text().strip() == "ok"


def test_model_failures_are_chess_not_infra(fake):
    assert fake("move:a1a8")["structured"]["move"] == "a1a8"
    r = fake("max_turns", "ok", attempts=2)
    assert r["attempts"] == 1 and r["subtype"] == "error_max_turns" and r["structured"] is None


def test_log_callback_sees_each_failed_attempt(fake):
    events = []
    fake("garbage", "rate_limit", "ok", attempts=3, log=events.append)
    assert [e["what"] for e in events] == ["cli_crash", "rate_limit"]


def test_stopped_kills_the_call(fake, tmp_path):
    """SIGTERM during a call raises Stopped (not swallowed by retries) and leaves no orphan."""
    modes = tmp_path / "m2.txt"
    modes.write_text("hang")
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from rsi import reliable\n"
            "reliable.install_stop_handlers()\n"
            "try:\n"
            "    reliable.ask_claude('x', cli=%r, timeout=60, extra_env={'FAKE_CLAUDE_MODES': %r})\n"
            "except reliable.Stopped:\n"
            "    print('stopped', flush=True)\n") % (str(ROOT), make_cli(tmp_path), str(modes))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    for _ in range(50):
        if leftovers():
            break
        time.sleep(0.2)
    p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=20)
    assert "stopped" in out
    time.sleep(0.5)
    assert not leftovers()


def test_jsonl_concurrent_writers_and_torn_tail(tmp_path):
    log = tmp_path / "a.jsonl"
    log.write_text('{"torn": tru')  # a crash mid-write left half a line, no newline
    code = ("import sys; sys.path.insert(0, %r); from rsi import reliable\n"
            "for i in range(250): reliable.append_jsonl(%r, {'w': int(sys.argv[1]), 'i': i, 'pad': 'x' * 3000})"
            ) % (str(ROOT), str(log))
    procs = [subprocess.Popen([sys.executable, "-c", code, str(w)]) for w in range(4)]
    assert all(p.wait() == 0 for p in procs)
    rows = reliable.read_jsonl(log)
    assert len(rows) == 1000 and len({(r["w"], r["i"]) for r in rows}) == 1000
    with open(log, "a") as f:
        f.write('{"half": ')
    assert len(reliable.read_jsonl(log)) == 1000
    assert reliable.read_jsonl(tmp_path / "missing.jsonl") == []


def test_runlock_refuses_second_and_is_freed_by_sigkill(tmp_path):
    lockf = tmp_path / "heavy.lock"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sys,time; sys.path.insert(0, %r); from rsi import reliable\n"
                               "l = reliable.RunLock(%r, 'test holder'); assert l.acquire(); print('held', flush=True)\n"
                               "time.sleep(60)" % (str(ROOT), str(lockf))], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    mine = reliable.RunLock(lockf, "second")
    assert not mine.acquire() and mine.held_elsewhere()
    assert mine.holder()["pid"] == holder.pid
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait()
    assert not mine.held_elsewhere()
    with mine:
        assert mine.holder()["note"] == "second"
    assert reliable.RunLock().path == Path.home() / ".bonsai" / "heavy.lock"


def test_atomic_write_round_trips_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "sub" / "state.json"
    reliable.atomic_write_json(p, {"big": "y" * 2_000_000})
    reliable.atomic_write_json(p, {"v": 2}, indent=1)
    assert json.loads(p.read_text()) == {"v": 2}
    assert [f.name for f in p.parent.iterdir()] == ["state.json"]


def test_run_json_is_frozen_config():
    cfg = reliable.run_config()
    assert cfg["cli"]["path"] == reliable.CLAUDE == "/opt/homebrew/bin/claude"
    assert cfg["proposer"]["model"] == "opus" and cfg["think_default"] == 0
    assert cfg["workers"] == 3 and cfg["max_usd"] == 50 and cfg["max_hours"] == 10
