"""Staged paired exam: is the child harness better than its parent on the same held-out positions?

Both arms answer each position FRESH (never the parent's stored game move: positions were picked because the parent
erred there, so reusing that move would flatter any child by regression to the mean). The two answers of a position
run back to back in a random order, so load and time of day hit both arms alike. Each answer is scored as win-points
lost versus Stockfish's best move. A position where either arm hit infra is void and drops out of BOTH arms.

Looks at 40 / 80 / 120 positions. At a look the child is kept if P(child better) >= 0.997 / 0.99 / 0.97 (the smaller
of a bootstrap of the paired mean and a sign-flip test), and dropped for futility if P < 0.30. More than 10% void -> inconclusive (re-queued, never pruned).
A removal-only child (a shed) is kept at the last look if its mean gain >= -0.5. A think increase must also gain
>= 1.0 win-point per +1024 tokens.

The player is a callable choose(board, harness, ctx) -> {move: chess.Move|None, reason, attempts, calls, cost, infra},
and score(pos, move) -> loss, so the whole exam runs offline with fakes.
"""

from __future__ import annotations

import hashlib
import random
from concurrent.futures import ThreadPoolExecutor

import chess

# Spec said 0.99/0.97/0.90; offline A/A gave 10-11% false accepts with those, so the looks are tightened to keep
# A/A false accept <= 5% (measured 2.9% no ties, 3.6% at 70% tied positions; 1500 sims each).
LOOKS = ((40, 0.997), (80, 0.99), (120, 0.97))
FUTILITY = 0.30
VOID_MAX = 0.10
SHED_MARGIN = -0.5
THINK_GAIN_PER_1024 = 1.0
PRESCREEN_P = 0.5
PRESCREEN_UNUSABLE = 0.05
BOOT = 10_000


def required_gain(parent: dict, child: dict) -> float:
    """Extra mean gain a think increase must show to pay for itself."""
    up = int(child.get("think") or 0) - int(parent.get("think") or 0)
    return THINK_GAIN_PER_1024 * up / 1024 if up > 0 else 0.0


def p_better(diffs: list[float], rng: random.Random, margin: float = 0.0, B: int = BOOT) -> float:
    """Bootstrap P(mean diff > margin); a resample exactly at the margin counts half (A/A with many ties)."""
    n = len(diffs)
    if not n:
        return 0.5
    hit = 0.0
    for _ in range(B):
        m = sum(rng.choices(diffs, k=n)) / n
        hit += 1.0 if m > margin + 1e-12 else 0.5 if abs(m - margin) <= 1e-12 else 0.0
    return hit / B


def p_signflip(diffs: list[float], rng: random.Random, B: int = BOOT) -> float:
    """Randomisation P(child better): share of random sign flips whose mean falls below the observed mean (ties half).
    Exact under A/A, where the two arms of a position are exchangeable, whatever the ties and tails."""
    n = len(diffs)
    if not n:
        return 0.5
    obs, hit = sum(diffs), 0.0
    for _ in range(B):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs)
        hit += 1.0 if s < obs - 1e-9 else 0.5 if abs(s - obs) <= 1e-9 else 0.0
    return hit / B


def ci90(diffs: list[float], rng: random.Random, B: int = 2000) -> list[float]:
    n = len(diffs)
    if not n:
        return [0.0, 0.0]
    boots = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(B))
    return [round(boots[int(0.05 * B)], 2), round(boots[int(0.95 * B) - 1], 2)]


def decide(diffs: list[float], void: int, answered: int, look: int, *, looks=LOOKS, futility: float = FUTILITY,
           shed: bool = False, min_gain: float = 0.0, void_max: float = VOID_MAX, rng: random.Random | None = None,
           B: int = BOOT, final: bool = False) -> tuple[str, float, float]:
    """(decision, P(child better), mean gain) at look index `look`. decision: accept | reject | continue | inconclusive."""
    rng = rng or random.Random(0)
    if answered and void / answered > void_max:
        return "inconclusive", 0.5, 0.0
    mean = sum(diffs) / len(diffs) if diffs else 0.0
    last = final or look == len(looks) - 1
    if shed:
        p = p_better(diffs, rng, SHED_MARGIN, B)
        if p < futility:
            return "reject", p, mean
        if last:
            return ("accept" if mean >= SHED_MARGIN else "reject"), p, mean
        return "continue", p, mean
    # Gate on the smaller of the bootstrap and the sign-flip P: the bootstrap alone is anti-conservative on few,
    # heavy-tailed, tie-rich diffs (A/A false accept 6-12% in simulation); the sign-flip test is exact under A/A.
    p = min(p_better(diffs, rng, 0.0, B), p_signflip(diffs, rng, B))
    if p >= looks[look][1] and mean >= min_gain:
        return "accept", p, mean
    if p < futility or last:
        return "reject", p, mean
    return "continue", p, mean


def _seed(*parts) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def answer(pos: dict, harness: dict, choose, arm: str, purpose: str = "exam") -> dict:
    """One fresh answer. Any exception from the player is infra (void), never a chess loss."""
    board = chess.Board(pos["fen"])
    ctx = {"history": pos.get("history") or [], "mine": pos.get("mine"), "pos_id": pos.get("id"), "arm": arm,
           "purpose": purpose}
    try:
        r = choose(board, harness, ctx)
    except Exception as e:  # InfraAbort, engine trouble: void. Stopped is a BaseException and propagates.
        return {"infra": True, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    atts = r.get("attempts") or []
    mv = r.get("move")
    if mv is not None and not isinstance(mv, str):
        mv = mv.uci()
    if mv is not None and chess.Move.from_uci(mv) not in board.legal_moves:
        mv = None  # an illegal "chosen" move is the player's failure, scored as a forfeit
    return {"infra": bool(r.get("infra")), "move": mv, "calls": r.get("calls") or len(atts) or 1,
            "cost": r.get("cost") or 0.0, "replies": len(atts) or 1,
            "unusable": sum(1 for a in atts if isinstance(a, dict) and a.get("kind") in ("illegal", "format")),
            "reason": (r.get("reason") or "")[:200]}


def _answer_range(positions, lo, hi, parent, child, choose, workers, seed, cache, on_answer, purpose):
    """Answers for positions[lo:hi], both arms of a position adjacent and in a seeded random order."""
    jobs = []
    for i in range(lo, hi):
        pid = positions[i].get("id") or str(i)
        arms = [("a", parent), ("b", child)]
        if random.Random(_seed(seed, pid)).random() < 0.5:
            arms.reverse()
        jobs += [(i, pid, arm, h) for arm, h in arms if (pid, arm) not in cache]

    def run(job):
        i, pid, arm, h = job
        ans = answer(positions[i], h, choose, arm, purpose)
        cache[(pid, arm)] = ans
        if on_answer:
            on_answer(pid, arm, ans)
        return ans

    if workers <= 1:
        for j in jobs:
            run(j)
    else:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(run, jobs))


def _rows(positions, n, cache, score):
    rows, void = [], 0
    for i, p in enumerate(positions[:n]):
        pid = p.get("id") or str(i)
        a, b = cache[(pid, "a")], cache[(pid, "b")]
        if a["infra"] or b["infra"]:
            void += 1
            rows.append({"id": pid, "void": True})
            continue
        la, lb = score(p, a["move"]), score(p, b["move"])
        rows.append({"id": pid, "a": a["move"], "b": b["move"], "loss_a": la, "loss_b": lb, "diff": round(la - lb, 2)})
    return rows, void


def _arm_summary(rows, cache, arm):
    done = [r for r in rows if not r.get("void")]
    key = "loss_" + arm
    ans = [cache[(r["id"], arm)] for r in done]
    replies = sum(x["replies"] for x in ans) or 1
    return {"mean_loss": round(sum(r[key] for r in done) / max(1, len(done)), 2),
            "blunders": sum(r[key] >= 15 for r in done), "forfeits": sum(r[arm] is None for r in done),
            "unusable_rate": round(sum(x["unusable"] for x in ans) / replies, 3)}


def run_exam(positions: list[dict], parent: dict, child: dict, choose, score, *, looks=LOOKS,
             futility: float = FUTILITY, shed: bool = False, min_gain: float | None = None,
             void_max: float = VOID_MAX, workers: int = 3, seed: str = "", B: int = BOOT,
             cache: dict | None = None, on_answer=None) -> dict:
    """Staged paired exam of child (arm b) vs parent (arm a). Returns the verdict with per-position rows.

    cache: {(pos_id, arm): answer} already bought (resume); filled in place. on_answer(pos_id, arm, answer) persists.
    """
    cache = {} if cache is None else cache
    min_gain = required_gain(parent, child) if min_gain is None else min_gain
    rng = random.Random(_seed("boot", seed))
    looks = tuple((min(n, len(positions)), t) for n, t in looks)
    done, history = 0, []
    decision, p, mean, rows, void = "inconclusive", 0.5, 0.0, [], 0
    for k, (n, _) in enumerate(looks):
        if n <= done:
            continue
        _answer_range(positions, done, n, parent, child, choose, workers, seed, cache, on_answer, "exam")
        done = n
        rows, void = _rows(positions, n, cache, score)
        diffs = [r["diff"] for r in rows if not r.get("void")]
        final = n >= len(positions) or k == len(looks) - 1
        decision, p, mean = decide(diffs, void, n, k, looks=looks, futility=futility, shed=shed, min_gain=min_gain,
                                   void_max=void_max, rng=rng, B=B, final=final)
        history.append({"n": n, "p": round(p, 3), "gain": round(mean, 2), "void": void, "decision": decision})
        if decision != "continue":
            break
    diffs = [r["diff"] for r in rows if not r.get("void")]
    used = [cache[(r["id"], arm)] for r in rows for arm in ("a", "b") if (r["id"], arm) in cache]
    return {"decision": decision, "accept": decision == "accept", "n": len(diffs), "void": void, "positions": done,
            "gain": round(mean, 2), "p": round(p, 3), "ci90": ci90(diffs, rng), "shed": shed, "min_gain": min_gain,
            "wins": sum(d > 0 for d in diffs), "losses": sum(d < 0 for d in diffs), "ties": sum(d == 0 for d in diffs),
            "a": _arm_summary(rows, cache, "a"), "b": _arm_summary(rows, cache, "b"), "looks": history,
            "calls": sum(x.get("calls") or 0 for x in used), "cost": round(sum(x.get("cost") or 0 for x in used), 4),
            "rows": rows}


def prescreen(positions: list[dict], parent: dict, child: dict, choose, score, *, workers: int = 3,
              seed: str = "", B: int = 2000, cache: dict | None = None, on_answer=None) -> dict:
    """A filter, never an accept: kill the child if P(better) < 0.5 or its unusable-reply rate rises >= 5 points."""
    cache = {} if cache is None else cache
    _answer_range(positions, 0, len(positions), parent, child, choose, workers, seed, cache, on_answer, "prescreen")
    rows, void = _rows(positions, len(positions), cache, score)
    diffs = [r["diff"] for r in rows if not r.get("void")]
    p = p_better(diffs, random.Random(_seed("pre", seed)), 0.0, B) if diffs else 0.5
    a, b = _arm_summary(rows, cache, "a"), _arm_summary(rows, cache, "b")
    up = round(b["unusable_rate"] - a["unusable_rate"], 3)
    kill = bool(diffs) and (p < PRESCREEN_P or up >= PRESCREEN_UNUSABLE)
    return {"kill": kill, "p": round(p, 3), "gain": round(sum(diffs) / len(diffs), 2) if diffs else 0.0,
            "n": len(diffs), "void": void, "unusable_delta": up, "a": a, "b": b, "rows": rows}


def simulate_aa(losses: list[float], sims: int = 400, n_positions: int = 120, seed: int = 7, B: int = 1000,
                tie_rate: float = 0.0, looks=LOOKS, **kw) -> dict:
    """Offline A/A: both arms draw from the same per-move loss distribution. Returns the false-accept rate.

    tie_rate: share of positions where both arms give the same move (diff 0), as a stochastic but similar player does.
    """
    rng = random.Random(seed)
    accepts, stages = 0, []
    for _ in range(sims):
        diffs = [0.0 if rng.random() < tie_rate else rng.choice(losses) - rng.choice(losses)
                 for _ in range(n_positions)]
        for k, (n, _) in enumerate(looks):
            d, _, _ = decide(diffs[:n], 0, n, k, looks=looks, rng=rng, B=B, **kw)
            if d != "continue":
                accepts += d == "accept"
                stages.append(n)
                break
    return {"sims": sims, "false_accept": accepts / sims, "mean_positions": sum(stages) / max(1, len(stages))}
