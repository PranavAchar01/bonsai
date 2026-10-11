"""rsi/player.py against the fake CLI (zero model calls).

  uv run -q --no-project --with chess --with pytest python -m pytest rsi/tests/test_player.py -q
"""

import subprocess
import time

import chess
import pytest

from rsi import exam, player, reliable
from rsi.tests.fake_claude import make_cli

H = {"id": "t0", "model": "claude-haiku-4-5-20251001", "rules": "Play solid chess.",
     "lessons": [{"id": "L1", "text": "Check every capture before you play it."}], "tools": ["legal_moves"], "think": 0}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """play(modes..., board=, harness=, **kw): player.choose against the fake CLI with the given per-call modes."""
    cli = make_cli(tmp_path)
    modes, log = tmp_path / "modes.txt", tmp_path / "calls.jsonl"
    monkeypatch.setattr(reliable, "BACKOFF", (0.05, 0.05, 0.05))
    env = {"FAKE_CLAUDE_MODES": str(modes), "FAKE_CLAUDE_LOG": str(log)}

    def play(*m, board=None, harness=None, ctx=None, **kw):
        modes.write_text(" ".join(m))
        kw.setdefault("timeout", 20)
        return player.choose(board or chess.Board(), harness or H, ctx, cli=cli, extra_env=env, **kw)

    play.calls = lambda: reliable.read_jsonl(log)
    play.cli, play.env, play.modes = cli, env, modes
    return play


def argv_of(call, flag):
    a = call["argv"]
    return a[a.index(flag) + 1]


def test_ok_structured_move_and_clean_call(fake):
    r = fake("ok", harness={**H, "think": 1024})
    assert r["move"] == chess.Move.from_uci("a2a3") and r["uci"] == "a2a3" and not r["infra"]
    assert r["calls"] == 1 and r["attempts"][0]["kind"] == "ok" and r["model"] == "claude-haiku-4-5-20251001"
    assert r["reason"] == "first legal move" and r["think_tokens"] == 1024 and r["cost"] > 0
    (c,) = fake.calls()
    assert c["stdin_devnull"] and argv_of(c, "--tools") == "" and argv_of(c, "--model") == H["model"]
    assert '"move"' in argv_of(c, "--json-schema") and '"reason"' in argv_of(c, "--json-schema")
    system, prompt = argv_of(c, "--system-prompt"), argv_of(c, "-p")
    assert "Play solid chess." in system and "Check every capture" in system
    assert "Legal moves" in prompt and "FEN: " + chess.STARTING_FEN in prompt and "You are playing white" in prompt
    assert c["env"]["MAX_THINKING_TOKENS"] == "1024"


def test_san_and_uci_variants(fake):
    assert fake("move:Nf3")["uci"] == "g1f3"
    assert fake("move:e2-e4")["uci"] == "e2e4"
    assert fake("text:e2e4")["uci"] == "e2e4"  # no structured output: parsed from the text


def test_illegal_then_legal_feeds_back_rejection(fake):
    r = fake("move:e2e5", "move:d4")
    assert r["uci"] == "d2d4" and r["calls"] == 2
    assert [a["kind"] for a in r["attempts"]] == ["illegal", "ok"]
    second = fake.calls()[1]
    assert "e2e5" in argv_of(second, "-p") and "Rejected" in argv_of(second, "-p")


def test_three_unusable_replies_is_a_forfeit_not_infra(fake):
    r = fake("move:e2e5", "empty", "move:Ke2")
    assert r["move"] is None and not r["infra"] and r["calls"] == 3
    assert [a["kind"] for a in r["attempts"]] == ["illegal", "format", "illegal"]


def test_model_own_failure_is_chess(fake):
    r = fake("max_turns", "ok")  # error_max_turns with is_error: unusable reply, not infra
    assert [a["kind"] for a in r["attempts"]] == ["format", "ok"] and r["uci"] == "a2a3"


def test_garbage_is_infra_retried_separately(fake):
    r = fake("garbage", "ok")
    assert r["uci"] == "a2a3" and r["calls"] == 1 and r["attempts"][0]["infra_retries"] == 1
    with pytest.raises(player.InfraAbort) as e:
        fake("garbage", "garbage", "garbage")
    assert e.value.kind == "cli_crash" and isinstance(e.value, reliable.InfraError)


def test_hang_is_killed_without_orphans(fake):
    t0 = time.time()
    with pytest.raises(player.InfraAbort) as e:
        fake("hang", timeout=1.5, attempts=1)
    assert e.value.kind == "timeout" and time.time() - t0 < 15
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "FAKE_GRANDCHILD_MARKER"], capture_output=True, text=True).stdout.split()
    assert not left


def test_is_error_with_subtype_success_is_infra(fake):
    with pytest.raises(player.InfraAbort) as e:
        fake("api_error", "api_error", "api_error")
    assert e.value.kind == "api_error" and len(fake.calls()) == 3
    with pytest.raises(player.InfraAbort) as e:
        fake("model_400", "ok")  # a bad model id will not fix itself: one call only
    assert e.value.kind == "api_error" and not e.value.retryable


def test_usage_limit_near_waits_far_raises(fake):
    t0 = time.time()
    r = fake("usage_limit:1", "ok")
    assert r["uci"] == "a2a3" and time.time() - t0 >= 0.9
    with pytest.raises(player.InfraAbort) as e:
        fake("usage_far", "ok", "ok")
    assert e.value.kind == "usage_limit" and e.value.reset_at and e.value.reset_at > time.time() + 3600


def test_auth_not_retried(fake):
    with pytest.raises(player.InfraAbort) as e:
        fake("auth", "ok")
    assert e.value.kind == "auth" and not e.value.retryable


def test_black_to_move_and_history(fake):
    b = chess.Board()
    b.push_uci("e2e4")
    r = fake("move:e5", board=b, ctx={"history": ["e4"], "mine": "black"})
    assert r["uci"] == "e7e5"
    p = argv_of(fake.calls()[0], "-p")
    assert "You are playing black" in p and "Moves so far: e4" in p


def test_exam_interface(fake):
    """exam.answer() drives choose(board, harness, ctx) and reads move/attempts/infra."""
    pos = {"id": "p1", "fen": chess.STARTING_FEN, "history": [], "mine": "white"}
    fake.modes.write_text("move:e2e5 ok")
    ch = lambda b, h, ctx: player.choose(b, h, ctx, cli=fake.cli, extra_env=fake.env, timeout=20)  # noqa: E731
    a = exam.answer(pos, H, ch, "A")
    assert a["move"] == "a2a3" and a["unusable"] == 1 and not a["infra"]
    fake.modes.write_text("auth")
    assert exam.answer(pos, H, ch, "B")["infra"]


@pytest.mark.parametrize("text,fen,want", [
    ("e2e4", chess.STARTING_FEN, "e2e4"), ("E2E4", chess.STARTING_FEN, "e2e4"), ("e2 e4", chess.STARTING_FEN, "e2e4"),
    ("Nf3", chess.STARTING_FEN, "g1f3"), ("nf3", chess.STARTING_FEN, "g1f3"), ("Nf3+!", chess.STARTING_FEN, "g1f3"),
    ("I will play d4.", chess.STARTING_FEN, "d2d4"), ('{"move": "c2c4"}', chess.STARTING_FEN, "c2c4"),
    ("e7e8", "8/4P3/8/8/8/8/k7/4K3 w - - 0 1", "e7e8q"), ("e8=N", "8/4P3/8/8/8/8/k7/4K3 w - - 0 1", "e7e8n"),
    ("0-0", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"), ("O-O-O", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"),
    ("e2e5", chess.STARTING_FEN, None), ("Ke2", chess.STARTING_FEN, None), ("", chess.STARTING_FEN, None),
    ("hello", chess.STARTING_FEN, None),
])
def test_parse_move(text, fen, want):
    mv, kind = player.parse_move(chess.Board(fen), text)
    assert (mv.uci() if mv else None) == want
    assert kind == ("ok" if want else ("format" if text in ("", "hello") else "illegal"))
