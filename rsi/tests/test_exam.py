"""Staged paired exam with fake players and a fake scorer: no model calls, no Stockfish."""

import random
import threading

import chess
import pytest

from rsi import exam as X


def make_positions(n, seed=1):
    rng, out = random.Random(seed), []
    while len(out) < n:
        b = chess.Board()
        for _ in range(rng.randrange(4, 30)):
            moves = list(b.legal_moves)
            if not moves:
                break
            b.push(rng.choice(moves))
        legal = sorted(m.uci() for m in b.legal_moves)
        if len(legal) >= 2:
            out.append({"id": f"p{len(out)}", "fen": b.fen(), "history": [], "mine": "white" if b.turn else "black",
                        "good": legal[0], "bad": legal[-1]})
    return out


POS = make_positions(120)
BY_ID = {p["id"]: p for p in POS}


def score(pos, move):
    return 0.0 if move == pos["good"] else 30.0


class Fake:
    """Plays the good move with probability 1 - harness['err']; seeded per (position, arm, harness) call."""

    def __init__(self, infra_rate=0.0, unusable=None):
        self.calls, self.lock, self.infra_rate, self.unusable = [], threading.Lock(), infra_rate, unusable or {}

    def __call__(self, board, harness, ctx):
        with self.lock:
            self.calls.append((ctx["pos_id"], ctx["arm"]))
            k = len(self.calls)
        rng = random.Random(f"{ctx['pos_id']}|{harness['name']}|{k}")
        if rng.random() < self.infra_rate and harness["name"] == "child":
            return {"move": None, "infra": True, "attempts": [], "calls": 1, "cost": 0}
        p = BY_ID[ctx["pos_id"]]
        mv = p["good"] if rng.random() >= harness["err"] else p["bad"]
        bad = self.unusable.get(harness["name"], 0)
        atts = [{"kind": "illegal", "move": "a1a8"}] * bad + [{"kind": "ok", "move": mv}]
        return {"move": chess.Move.from_uci(mv), "reason": "r", "attempts": atts, "calls": len(atts), "cost": 0.001,
                "infra": False}


A = {"name": "parent", "err": 0.4, "think": 0}


def test_clearly_better_child_accepted_early():
    r = X.run_exam(POS, A, {"name": "child", "err": 0.05, "think": 0}, Fake(), score, seed="s1", B=2000)
    assert r["decision"] == "accept" and r["positions"] == 40 and r["p"] >= 0.99
    assert r["a"]["mean_loss"] > r["b"]["mean_loss"] and r["calls"] == 80


def test_clearly_worse_child_stopped_for_futility():
    r = X.run_exam(POS, A, {"name": "child", "err": 0.8, "think": 0}, Fake(), score, seed="s2", B=2000)
    assert r["decision"] == "reject" and r["positions"] == 40


def test_void_positions_drop_both_arms_and_too_many_is_inconclusive():
    r = X.run_exam(POS, A, {"name": "child", "err": 0.05, "think": 0}, Fake(infra_rate=0.03), score, seed="s3",
                   B=2000)
    assert r["void"] >= 1 and r["n"] == r["positions"] - r["void"] and r["decision"] == "accept"
    r = X.run_exam(POS, A, {"name": "child", "err": 0.05, "think": 0}, Fake(infra_rate=0.3), score, seed="s3",
                   B=2000)
    assert r["decision"] == "inconclusive" and not r["accept"]


def test_player_exception_is_void_not_loss():
    def boom(board, harness, ctx):
        raise RuntimeError("cli died")
    r = X.run_exam(POS[:40], A, {"name": "child", "err": 0}, boom, score, seed="s4", workers=1, B=200)
    assert r["decision"] == "inconclusive" and r["void"] == 40


def test_answers_interleaved_and_resumable():
    fake, cache, saved = Fake(), {}, []
    child = {"name": "child", "err": 0.4, "think": 0}
    r1 = X.run_exam(POS[:40], A, child, fake, score, seed="s5", workers=1, B=500, cache=cache,
                    on_answer=lambda *a: saved.append(a))
    pairs = [fake.calls[i:i + 2] for i in range(0, len(fake.calls), 2)]
    assert all(a[0] == b[0] and {a[1], b[1]} == {"a", "b"} for a, b in pairs)  # both arms of a position adjacent
    assert {a[1] for a, _ in pairs} == {"a", "b"}  # order within a pair is randomised
    assert len(saved) == 80
    fake2 = Fake()
    r2 = X.run_exam(POS[:40], A, child, fake2, score, seed="s5", workers=1, B=500, cache=cache)
    assert fake2.calls == [] and r2["gain"] == r1["gain"]  # nothing bought twice


def test_shed_kept_when_not_worse():
    same = {"name": "child", "err": 0.0, "think": 0}
    r = X.run_exam(POS, {**A, "err": 0.0}, same, Fake(), score, seed="s6", shed=True, B=1000)
    assert r["decision"] == "accept" and r["positions"] == 120 and r["gain"] == 0


def test_think_increase_needs_gain_per_1024():
    assert X.required_gain({"think": 0}, {"think": 1024}) == 1.0
    assert X.required_gain({"think": 2048}, {"think": 1024}) == 0.0
    d, p, mean = X.decide([0.5] * 120, 0, 120, 2, min_gain=1.0)
    assert p == 1.0 and mean == 0.5 and d == "reject"
    assert X.decide([1.5] * 40, 0, 40, 0, min_gain=1.0)[0] == "accept"


def test_bootstrap_counts_ties_half():
    assert X.p_better([0.0] * 30, random.Random(0), B=100) == 0.5
    assert X.p_better([], random.Random(0)) == 0.5


def test_prescreen_kills_on_unusable_replies_and_on_worse():
    child = {"name": "child", "err": 0.4, "think": 0}
    r = X.prescreen(POS[:16], A, child, Fake(unusable={"child": 1}), score, seed="p1")
    assert r["kill"] and r["unusable_delta"] >= 0.05
    r = X.prescreen(POS[:16], A, {"name": "child", "err": 0.05}, Fake(), score, seed="p2")
    assert not r["kill"] and r["p"] >= 0.5
    r = X.prescreen(POS[:16], A, {"name": "child", "err": 0.9}, Fake(), score, seed="p3")
    assert r["kill"]


@pytest.mark.parametrize("tie_rate", [0.0, 0.5])
def test_aa_false_accept_at_most_5pct(tie_rate):
    rng = random.Random(1)
    # Heavy-tailed per-move win-point losses like the mined seed games: ~15% of moves lose >= 10.
    losses = [rng.choice([0, 0, 0, 1, 2, 3, 5, 8]) if rng.random() > 0.15 else rng.uniform(10, 50)
              for _ in range(2000)]
    r = X.simulate_aa(losses, sims=600, seed=31, B=1000, tie_rate=tie_rate)
    assert r["false_accept"] <= 0.05, r
