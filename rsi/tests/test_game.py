"""rsi/game.py with a ladder bot (or the fake CLI) standing in for the model: zero model calls.

  uv run -q --no-project --with chess --with pytest python -m pytest rsi/tests/test_game.py -q
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import chess
import pytest

from rsi import game, player, rating, reliable
from rsi.ladder import Bot
from rsi.tests.fake_claude import make_cli

ROOT = Path(__file__).resolve().parents[2]
H = {"id": "gT", "model": "bot", "rules": "", "tools": [], "think": 0}
E100 = {"name": "E100", "elo": 93, "spec": {"kind": "policy", "temp": 8, "eps": 0.556, "depth": 1}}
RANDOM = {"name": "random", "elo": 32, "spec": {"kind": "random"}}
VIEWER_KEYS = {"game", "harness", "agent_plays", "moves", "san", "final_fen", "forfeit"}


def slot(**kw):
    s = {"id": "s01", "seed": "run|1|s01", "family": "italian", "color": "white", "split": "train", "target": 100,
         "opp": E100}
    s.update(kw)
    return s


class BotAgent:
    """A ladder rung playing as the agent, with player.choose's return shape."""

    def __init__(self, spec, seed="agent", sleep=0.0):
        self.bot, self.sleep, self.calls = Bot(spec, seed=seed), sleep, 0

    def __call__(self, board, harness, ctx):
        self.calls += 1
        time.sleep(self.sleep)
        return {"move": self.bot.move(board), "reason": "bot", "attempts": [{"kind": "ok", "move": None}],
                "calls": 1, "cost": 0.0}

    def close(self):
        self.bot.close()


class Shuffle:
    """Knight out and back forever (as agent or opponent)."""

    def __init__(self, a, b):
        self.a, self.b = chess.Move.from_uci(a), chess.Move.from_uci(b)

    def move(self, board):
        return self.a if self.a in board.legal_moves else self.b

    def __call__(self, board, harness, ctx):
        return {"move": self.move(board), "reason": "", "attempts": [{"kind": "ok"}], "calls": 1, "cost": 0.0}


def play(tmp_path, s, **kw):
    kw.setdefault("replays_dir", tmp_path / "replays")
    return game.play_game(H, s, tmp_path / "games", "RUN1", **kw)


def test_openings_split_and_lines():
    fams = game.load_openings()
    assert len(fams) == 16 and len(game.families("train")) == 12 and len(game.families("held")) == 4
    for f in fams:
        assert f["eco"] and len(f["lines"]) >= 2
        for line in f["lines"]:
            assert 4 <= len(line) <= 8
            b = chess.Board()
            for u in line:
                b.push_uci(u)
    assert game.opening_for("sicilian", "x")[1] == game.opening_for("sicilian", "x")[1]
    picks = {tuple(game.opening_for("sicilian", f"s{i}")[1]) for i in range(30)}
    assert len(picks) > 1


def test_adjudication_table():
    assert game.adjudicated_white_score(1600, 300) == 0.72 and game.adjudicated_white_score(-1600, 300) == pytest.approx(0.28)
    assert game.adjudicated_white_score(900, 1000) == 0.82 and game.adjudicated_white_score(100, 2000) == 0.5
    assert game.adjudicated_white_score(100000, 2500) == 0.98


def test_mate_ending_record_and_replay(tmp_path):
    agent = BotAgent({"kind": "policy", "depth": 1})  # finds Qh4#
    try:
        rec = play(tmp_path, slot(color="black", opening=["f2f3", "e7e5", "g2g4"], opp=RANDOM), choose=agent)
    finally:
        agent.close()
    assert rec["status"] == "complete" and rec["how"] == "checkmate" and rec["score"] == 1.0 and rec["result"] == "0-1"
    assert rec["moves"][-1] == "d8h4" and rec["san"][-1] == "Qh4#" and rec["opening_plies"] == 3
    assert rec["per_move"][0]["ply"] == 3 and rec["calls"] == 1
    for k in ("opp_elo", "score", "target", "status"):  # what rating.py needs
        assert k in rec
    assert rating.estimate([rec])["n"] == 1
    replay = tmp_path / "replays" / "RUN1-gT" / f"{rec['game']}.json"
    assert replay.exists() and VIEWER_KEYS <= set(json.loads(replay.read_text()))
    assert rec["game"].startswith("italian-black-") and not (tmp_path / "games" / "s01.state.json").exists()
    # a finished slot is returned as is, never replayed
    again = play(tmp_path, slot(color="black", opening=["f2f3", "e7e5", "g2g4"], opp=RANDOM),
                 choose=lambda *a: pytest.fail("finished game was replayed"))
    assert again == rec


def test_threefold_is_claimed(tmp_path):
    rec = play(tmp_path, slot(opening=[]), choose=Shuffle("g1f3", "f3g1"), opponent=Shuffle("g8f6", "f6g8"))
    assert rec["status"] == "complete" and rec["how"] == "threefold_repetition" and rec["score"] == 0.5
    assert rec["result"] == "1/2-1/2" and rec["plies"] == 7  # claimable before the repeating 8th ply


def test_move_cap_is_adjudicated_fractionally(tmp_path):
    agent = BotAgent({"kind": "random"})
    try:
        rec = play(tmp_path, slot(opp=RANDOM), choose=agent, max_moves=8)
    finally:
        agent.close()
    assert rec["status"] == "complete" and rec["how"] == "move cap" and rec["result"] == "*"
    assert chess.Board(rec["final_fen"]).fullmove_number == 9
    allowed = {0.5} | {s for t in game.ADJUDICATION.values() for _, s in t} | {1 - s for t in game.ADJUDICATION.values() for _, s in t}
    assert any(abs(rec["score"] - a) < 1e-9 for a in allowed) and rec["adjudication"]["band_elo"] == 32


def test_time_cap(tmp_path):
    agent = BotAgent({"kind": "random"}, sleep=0.2)
    try:
        rec = play(tmp_path, slot(), choose=agent, time_cap=0.5)
    finally:
        agent.close()
    assert rec["how"] == "time cap" and rec["status"] == "complete" and agent.calls <= 4


def test_forfeit_is_a_loss(tmp_path):
    def bad(board, h, ctx):
        return {"move": None, "reason": "", "attempts": [{"kind": "illegal", "move": "e2e5"}] * 3, "calls": 3, "cost": 0.0}
    rec = play(tmp_path, slot(), choose=bad)
    assert rec["status"] == "complete" and rec["how"] == "forfeit" and rec["score"] == 0.0 and rec["result"] == "0-1"
    assert "three unusable replies" in rec["forfeit"] and rec["calls"] == 3


def test_unreachable_model_is_infra_then_resumes(tmp_path, monkeypatch):
    """Real player.choose through the fake CLI: two good moves, then the CLI crashes on every retry."""
    cli, modes = make_cli(tmp_path), tmp_path / "modes.txt"
    monkeypatch.setattr(reliable, "BACKOFF", (0.05, 0.05, 0.05))
    env = {"FAKE_CLAUDE_MODES": str(modes)}
    ch = lambda b, h, ctx: player.choose(b, h, ctx, cli=cli, extra_env=env, timeout=20)  # noqa: E731
    h = {**H, "model": "haiku", "tools": ["legal_moves"]}
    modes.write_text("ok ok garbage garbage garbage")
    rec = game.play_game(h, slot(), tmp_path / "games", "RUN1", choose=ch, max_moves=8, replays_dir=None)
    assert rec["status"] == "infra" and rec["score"] is None and rec["infra_kind"] == "cli_crash"
    assert len(rec["per_move"]) == 2 and (tmp_path / "games" / "s01.state.json").exists()
    assert rating.estimate([rec])["n"] == 0  # infra never reaches a rating
    modes.write_text("")  # model is back
    rec2 = game.play_game(h, slot(), tmp_path / "games", "RUN1", choose=ch, max_moves=8, replays_dir=None)
    assert rec2["status"] == "complete" and rec2["resumed"] == 1 and rec2["moves"][:len(rec["moves"])] == rec["moves"]
    assert rec2["model"] == "claude-haiku-4-5-20251001" and rec2["tier"] == 1


def test_engine_failure_is_infra(tmp_path):
    rec = play(tmp_path, slot(), choose=Shuffle("g1f3", "f3g1"), engine_path="/nonexistent/stockfish")
    assert rec["status"] == "infra" and rec["infra_kind"] == "engine" and rec["score"] is None


def child(out, replays, *extra, wait=True):
    cmd = [sys.executable, "-m", "rsi.tests.game_child", str(out), str(replays), *map(str, extra)]
    if wait:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    return subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_sigkill_and_sigterm_resume_identical(tmp_path):
    ref_dir, kill_dir, term_dir = tmp_path / "ref", tmp_path / "kill", tmp_path / "term"
    assert child(ref_dir, tmp_path / "r1").returncode == 0
    ref = json.loads((ref_dir / "s01.json").read_text())
    assert ref["status"] == "complete" and ref["plies"] > 10

    # SIGKILL at ply 10: no record (finally never ran), checkpoint at ply 10; the rerun resumes and matches.
    p = child(kill_dir, tmp_path / "r2", 10, "KILL")
    assert p.returncode == -signal.SIGKILL and not (kill_dir / "s01.json").exists()
    assert len(json.loads((kill_dir / "s01.state.json").read_text())["moves"]) == 10
    assert child(kill_dir, tmp_path / "r2").returncode == 0
    resumed = json.loads((kill_dir / "s01.json").read_text())
    assert resumed["resumed"] == 1 and game.strip_volatile(resumed) == game.strip_volatile(ref)

    # SIGTERM mid-move: the record is written (infra, stopped), the checkpoint kept; the rerun matches too.
    p = child(term_dir, tmp_path / "r3", 10, "HANG", wait=False)
    deadline = time.time() + 60
    while not (term_dir / "killed.marker").exists() and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(0.3)
    os.kill(p.pid, signal.SIGTERM)
    p.wait(30)
    stopped = json.loads((term_dir / "s01.json").read_text())
    assert stopped["status"] == "infra" and stopped["infra_kind"] == "stopped" and stopped["plies"] == 10
    assert (term_dir / "s01.state.json").exists()
    assert child(term_dir, tmp_path / "r3").returncode == 0
    assert game.strip_volatile(json.loads((term_dir / "s01.json").read_text())) == game.strip_volatile(ref)


def test_slot_without_opp_uses_rung_for_elo(tmp_path):
    agent = BotAgent({"kind": "random"})
    s = slot(target=100, opp=None, family="slav", split="held")
    try:
        rec = play(tmp_path, s, choose=agent, max_moves=6)
    finally:
        agent.close()
    assert rec["opp"]["name"] == "E100" and rec["opp_elo"] == 93 and rec["split"] == "held"
    assert rec["moves"][:rec["opening_plies"]] in game.load_openings()[12]["lines"]


def test_infra_waits_do_not_count_toward_the_time_cap(tmp_path):
    # each reply took 0.01 s of model time but the call spent 0.2 s in (simulated) backoff: no time cap
    bot = Bot({"kind": "random"}, seed="agent")

    def slow(board, h, ctx):
        time.sleep(0.2)
        return {"move": bot.move(board), "reason": "", "attempts": [{"kind": "ok", "wall_s": 0.01}], "calls": 1,
                "cost": 0.0}
    try:
        rec = play(tmp_path, slot(), choose=slow, time_cap=0.5, max_moves=8)
    finally:
        bot.close()
    assert rec["status"] == "complete" and rec["how"] != "time cap" and rec["elapsed_s"] < 0.5 + 8
