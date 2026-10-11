"""Ladder, Elo and rating tests. Stockfish only, no model calls. About 30 s."""

import concurrent.futures
import os
import random
import signal

import chess
import pytest

from rsi import elo, ladder, rating
from rsi.ladder import Bot, InfraError

LADDER = ladder.load_ladder()
SPEC = {r["name"]: r["spec"] for r in LADDER["rungs"]}
VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def material(board, color):
    return sum(v * len(board.pieces(p, color)) for p, v in VALUE.items())


def play(white: dict, black: dict, seed: str, max_plies: int = 200):
    """One bot game. Returns (white_score, uci moves); at the cap, >= 3 points of material wins, else a draw."""
    board = chess.Board()
    with Bot(white, seed + "|w") as w, Bot(black, seed + "|b") as b:
        while not board.is_game_over(claim_draw=True) and len(board.move_stack) < max_plies:
            board.push((w if board.turn == chess.WHITE else b).move(board))
    moves = [m.uci() for m in board.move_stack]
    if board.is_game_over(claim_draw=True):
        winner = board.outcome(claim_draw=True).winner
        return (0.5 if winner is None else float(winner == chess.WHITE)), moves
    d = material(board, chess.WHITE) - material(board, chess.BLACK)
    return (1.0 if d >= 3 else 0.0 if d <= -3 else 0.5), moves


def score_vs(a: dict, b: dict, n: int, tag: str, max_plies: int = 200) -> float:
    """a's mean score over n games, colours alternating."""
    total = 0.0
    for i in range(n):
        if i % 2 == 0:
            total += play(a, b, f"{tag}{i}", max_plies)[0]
        else:
            total += 1 - play(b, a, f"{tag}{i}", max_plies)[0]
    return total / n


# ---- ladder.json ---------------------------------------------------------------------------------------------

def test_ladder_json_shape():
    names = {r["name"] for r in LADDER["rungs"]}
    assert set(LADDER["climb"]) <= names and LADDER["start"] == "E100"
    elos = [next(r["elo"] for r in LADDER["rungs"] if r["name"] == n) for n in LADDER["climb"]]
    assert elos == sorted(elos), "climb must be in Elo order"
    assert LADDER["stockfish_version"] == ladder.stockfish_version()


# ---- Bot -----------------------------------------------------------------------------------------------------

def test_softmax_games_replay_identically():
    pairs = [("T2", "T4"), ("E100", "T8"), ("T1", "T2.8"), ("d1", "T5.7")]
    first = [play(SPEC[a], SPEC[b], f"repro{i}", max_plies=40)[1] for i in range(5) for a, b in pairs]
    again = [play(SPEC[a], SPEC[b], f"repro{i}", max_plies=40)[1] for i in range(5) for a, b in pairs]
    assert len(first) == 20 and first == again
    other = [play(SPEC[a], SPEC[b], f"other{i}", max_plies=40)[1] for i in range(5) for a, b in pairs[:1]]
    assert other != first[::4][:5], "a different seed should give different games"


def test_same_position_same_seed_same_move():
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    with Bot(SPEC["T2"], "s") as a, Bot(SPEC["T2"], "s") as b:
        assert a.move(board) == b.move(board) == a.move(board)


def test_engine_killed_mid_game_restarts_with_same_move():
    board = chess.Board()
    with Bot(SPEC["T1"], seed="x") as bot:
        for _ in range(6):
            board.push(bot.move(board))
        expected = bot.move(board)
        os.kill(bot.engine.transport.get_pid(), signal.SIGKILL)
        assert bot.move(board) == expected
        assert bot.restarts == 1


def test_cancelled_future_restarts():
    board = chess.Board()
    with Bot(SPEC["T2"], seed="c") as bot:
        expected = bot.move(board)

        def boom(*a, **k):
            raise concurrent.futures.CancelledError()

        bot.engine.analyse = boom  # this engine object is discarded on restart
        assert bot.move(board) == expected and bot.restarts == 1


def test_engine_that_cannot_restart_is_infra():
    board = chess.Board()
    with Bot(SPEC["T2"], seed="d") as bot:
        bot.engine_path = "/nonexistent/stockfish"
        os.kill(bot.engine.transport.get_pid(), signal.SIGKILL)
        with pytest.raises(InfraError) as e:
            bot.move(board)
        assert e.value.kind == "engine" and bot.restarts == 2


def test_strength_is_monotonic_quick():
    # calibrated 93 vs 32 -> expected ~0.59 (0.55 in calibration). These seeded games give 0.70: the material
    # adjudication at ply 200 here turns more long games into wins than the calibration's eval cap at ply 400.
    e100 = score_vs(SPEC["E100"], SPEC["random"], 40, "e100v")
    assert 0.4 <= e100 <= 0.75, e100
    t1 = score_vs(SPEC["T1"], SPEC["random"], 4, "t1v")
    assert t1 >= 0.875 and t1 > e100


# ---- elo / rating (synthetic results) --------------------------------------------------------------------------

def simulate(true_elo, opp_elos, rng):
    out = []
    for o in opp_elos:
        p = elo.expected(true_elo - o)
        u = rng.random()
        out.append({"opp_elo": o, "score": 1.0 if u < p - 0.05 else 0.5 if u < p + 0.05 else 0.0,
                    "target": o, "status": "complete"})
    return out


def test_estimate_covers_truth():
    rng, hits = random.Random(3), 0
    for true in (150, 600, 1100, 1500):
        hist = simulate(true, [true + rng.choice((-150, 0, 0, 0, 150)) for _ in range(60)], rng)
        est = rating.estimate(hist)
        hits += est["lo"] <= true <= est["hi"]
        assert est["n"] == 60 and est["hi"] - est["lo"] < 400
    assert hits >= 3


def test_estimate_ignores_infra_and_handles_sweeps():
    hist = [{"opp_elo": 93, "score": 1.0, "status": "complete"}] * 10
    hist += [{"opp_elo": 93, "score": 0.0, "status": "infra"}] * 10
    est = rating.estimate(hist)
    assert est["n"] == 10 and est["all_won"] and 93 < est["elo"] < 1000 and est["lo"] > 93 - 50
    assert rating.estimate([])["elo"] is None


def test_rung_for_elo():
    assert rating.rung_for_elo(100)["name"] == "E100" and rating.rung_for_elo(100)["elo"] == 93
    assert rating.rung_for_elo(0)["spec"] == {"kind": "random"}
    low = rating.rung_for_elo(60)
    assert low["spec"]["temp"] == 8 and 0.556 < low["spec"]["eps"] < 1 and low["elo"] == 60
    temps = [rating.rung_for_elo(e)["spec"]["temp"] for e in (250, 450, 650, 900, 1200, 1550)]
    assert temps == sorted(temps, reverse=True), temps
    mid = rating.rung_for_elo(1050)
    assert 1.0 < mid["spec"]["temp"] < 1.25 and mid["elo"] == 1050 and mid["sd"] > 40
    assert rating.rung_for_elo(1700)["name"] == "d1"
    assert rating.rung_for_elo(2050)["name"] == "sf2500"
    with Bot(low["spec"], "z") as b:  # interpolated specs are playable
        assert b.move(chess.Board()) in chess.Board().legal_moves


def test_bootstrap_doubles_then_steps_then_tracks():
    state, hist, seen = None, [], []
    state = rating.next_target(hist, state)
    assert state["target"] == 100 and state["bootstrap"] and state["slots"] == [100] * 24
    for _ in range(7):  # always wins at its target
        hist += [{"opp_elo": state["target"], "score": 1.0, "target": state["target"]}] * 8
        state = rating.next_target(hist, state, n_games=8)
        seen.append(state["target"])
        assert state["event"] == "promote"
    assert seen == [200, 400, 800, 1200, 1300, 1400, 1500]
    hist += [{"opp_elo": 1500, "score": s, "target": 1500} for s in (1, 0, 0.5, 0, 1, 0, 0, 0.5)]
    state = rating.next_target(hist, state)
    assert state["event"] == "bootstrap_done" and not state["bootstrap"]
    assert abs(state["target"] - state["estimate"]["elo"]) < 1


def test_tracking_mix_and_demotion():
    state = {"target": 800.0, "bootstrap": False}
    hist = [{"opp_elo": 800, "score": 0.0, "target": 800} for _ in range(10)]
    hist += [{"opp_elo": 600, "score": 0.5, "target": 800} for _ in range(4)]
    nxt = rating.next_target(hist, state, n_games=24)
    assert nxt["event"] == "demote" and nxt["target"] < 800 and not nxt["bootstrap"]
    slots, t = nxt["slots"], nxt["target"]
    assert len(slots) == 24
    assert (slots.count(t), slots.count(t + 150), slots.count(t - 150)) == (14, 5, 5)
    first = slots[:16]  # the train part of a batch carries the same mix
    assert first.count(t + 150) >= 3 and first.count(t - 150) >= 3


def test_milestones():
    rng = random.Random(5)
    hist = simulate(900, [750] * 40, rng)
    beaten = rating.milestones(hist)
    assert "T2" in beaten and "random" in beaten and "T1.25" not in beaten
    assert rating.milestones(hist[:10]) == []  # too few games


def test_fit_ratings_recovers_anchor_scale():
    rng = random.Random(1)
    true = {"a": 0.0, "b": 200.0, "c": 400.0}
    games = []
    for x, y in (("a", "b"), ("b", "c"), ("a", "c")):
        for _ in range(200):
            games.append((x, y, float(rng.random() < elo.expected(true[x] - true[y]))))
    fit = elo.fit_ratings(games, {"a": 0.0})
    assert abs(fit["b"] - 200) < 60 and abs(fit["c"] - 400) < 80
