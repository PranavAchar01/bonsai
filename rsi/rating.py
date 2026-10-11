"""The agent's side of the ladder: its rating, where the opponent goes next, and which rungs it has beaten.

A game record (what run.py stores per game) needs at least:
  {"opp_elo": 93, "score": 1 | 0.5 | 0, "target": 100, "status": "complete"}
`target` is the generation's centre target the game was scheduled under (not the slot's offset); games whose
status is not "complete" (infra) never count.

  estimate(history)                 performance MAP + profile 95% CI over the last 60 complete games
  rung_for_elo(target)              {"name", "spec", "elo", "sd"}: an opponent at any Elo on the ladder
  next_target(history, state)       bootstrap doubling, then follow the estimate; returns the next state and its
                                    per-game slot targets (60% at target, 20% at +150, 20% at -150)
  milestones(history)               climb rungs beaten (CI lower bound above the rung, >= 16 games at or above it)
"""

from __future__ import annotations

import math

from rsi.elo import performance
from rsi.ladder import LADDER_JSON, label, load_ladder

WINDOW = 60
DOUBLING = (100, 200, 400, 800, 1200)  # bootstrap targets; after 1200, +100 per promotion
STEP = 100
PROMOTE_SCORE = 0.9
MIN_GAMES = 8
SPREAD = 150
MIX = (0.6, 0.2, 0.2)                  # share of games at target, target + SPREAD, target - SPREAD
SNAP = 10                              # a calibrated climb rung this close to the target is used as-is
INTERP_SD = 40                         # extra rating error of an interpolated rung


def _complete(history: list[dict]) -> list[dict]:
    return [g for g in history if g.get("status", "complete") == "complete"]


def estimate(history: list[dict], window: int = WINDOW, opp_sd: float = 0.0) -> dict:
    """Performance rating over the last `window` complete games, rung Elos fixed.
    Returns {"elo", "lo", "hi", "n", "score", ...}; elo is None with no games."""
    recent = _complete(history)[-window:]
    return performance([(g["opp_elo"], g["score"]) for g in recent], prior_sd=400.0, opp_sd=opp_sd)


def _climb(ladder: dict) -> list[dict]:
    by_name = {r["name"]: r for r in ladder["rungs"]}
    return sorted((by_name[n] for n in ladder["climb"]), key=lambda r: r["elo"])


def rung_for_elo(target: float, ladder: dict | None = None) -> dict:
    """An opponent at `target`. A calibrated climb rung within SNAP Elo is used directly. Inside the depth-1
    softmax family (T8..T0.25) ln(T) is interpolated; between the random mover and T8, uniform-random moves are
    mixed into T8 (eps). Above the family, the nearest calibrated rung (d1, sf2100, ...). Returns
    {"name", "spec", "elo", "sd"} where elo is the rating the result counts at."""
    ladder = ladder or load_ladder(LADDER_JSON)
    climb = _climb(ladder)
    near = min(climb, key=lambda r: abs(r["elo"] - target))
    if abs(near["elo"] - target) <= SNAP:
        return {"name": near["name"], "spec": dict(near["spec"]), "elo": near["elo"], "sd": near["sd"]}
    rand = next(r for r in climb if r["spec"]["kind"] == "random")
    fam = [r for r in climb if r["spec"]["kind"] == "policy" and r["spec"].get("temp")
           and not r["spec"].get("eps") and r["spec"].get("depth", 1) == 1]
    if target <= rand["elo"]:
        return {"name": rand["name"], "spec": dict(rand["spec"]), "elo": rand["elo"], "sd": rand["sd"]}
    if target < fam[0]["elo"]:
        hot = fam[0]
        eps = round((hot["elo"] - target) / (hot["elo"] - rand["elo"]), 3)
        spec = {"kind": "policy", "temp": hot["spec"]["temp"], "eps": eps, "depth": 1}
        sd = math.hypot(max(hot["sd"], rand["sd"]), INTERP_SD)
        return {"name": label(spec), "spec": spec, "elo": round(target), "sd": round(sd)}
    for a, b in zip(fam, fam[1:]):
        if a["elo"] <= target <= b["elo"]:
            f = (target - a["elo"]) / (b["elo"] - a["elo"])
            t0, t1 = math.log(a["spec"]["temp"]), math.log(b["spec"]["temp"])
            spec = {"kind": "policy", "temp": round(math.exp(t0 + f * (t1 - t0)), 3), "depth": 1}
            sd = math.hypot(max(a["sd"], b["sd"]), INTERP_SD)
            return {"name": label(spec), "spec": spec, "elo": round(target), "sd": round(sd)}
    above = [r for r in climb if r["elo"] >= fam[-1]["elo"]]
    r = min(above, key=lambda r: abs(r["elo"] - target))
    return {"name": r["name"], "spec": dict(r["spec"]), "elo": r["elo"], "sd": r["sd"]}


def step_up(target: float) -> float:
    """Next bootstrap target: 100 -> 200 -> 400 -> 800 -> 1200, then +100."""
    return next((t for t in DOUBLING if t > target), target + STEP)


def slot_targets(target: float, n: int, mix=MIX, spread: float = SPREAD) -> list[float]:
    """n per-game targets: ~60% at target, ~20% at +spread, ~20% at -spread, interleaved so any prefix (e.g. the
    train vs held split of a batch) carries the same mix."""
    hi, lo = round(mix[1] * n), round(mix[2] * n)
    kinds = [(target, n - hi - lo), (target + spread, hi), (target - spread, lo)]
    order = sorted(((j + 0.5) / c, k, t) for k, (t, c) in enumerate(kinds) for j in range(c))
    return [t for _, _, t in order]


def next_target(history: list[dict], state: dict | None = None, n_games: int = 24, window: int = WINDOW) -> dict:
    """The opponent target for the next generation.

    Bootstrap (calibration, not improvement): start at 100; once >= 8 complete games were played at the current
    target, a score >= 0.9 doubles it (100, 200, 400, 800, 1200, then +100); anything less ends the bootstrap and
    the target becomes the estimate. Afterwards the target follows the estimate; a CI upper bound below the old
    target (>= 8 games at it) is logged as a demotion.

    state: the previous return value (None = fresh start). Returns {"target", "bootstrap", "event", "slots",
    "estimate"}; event is one of None, "promote", "bootstrap_done", "demote"; slots holds n_games per-game targets.
    """
    state = state or {"target": float(load_ladder().get("start_target", 100)), "bootstrap": True}
    target, boot, event = float(state["target"]), bool(state["bootstrap"]), None
    games = _complete(history)
    est = estimate(games, window)
    at = [g for g in games if g.get("target") is not None and abs(g["target"] - target) < 0.5]
    if boot:
        if len(at) >= MIN_GAMES:
            if sum(g["score"] for g in at) / len(at) >= PROMOTE_SCORE:
                target, event = float(step_up(target)), "promote"
            else:
                boot, event = False, "bootstrap_done"
                target = float(est["elo"])
    else:
        if len(at) >= MIN_GAMES and est["hi"] is not None and est["hi"] < target:
            event = "demote"
        if est["elo"] is not None:
            target = float(est["elo"])
    slots = [target] * n_games if boot else slot_targets(target, n_games)
    return {"target": target, "bootstrap": boot, "event": event, "slots": slots, "estimate": est}


def milestones(history: list[dict], ladder: dict | None = None, window: int = WINDOW, min_games: int = 16) -> list[str]:
    """Climb rungs the agent has beaten: estimate CI lower bound > the rung's Elo, with >= min_games complete
    games (in the window) against opponents rated at or above it."""
    ladder = ladder or load_ladder(LADDER_JSON)
    recent = _complete(history)[-window:]
    est = estimate(recent, window)
    if est["lo"] is None:
        return []
    return [r["name"] for r in _climb(ladder)
            if est["lo"] > r["elo"] and sum(g["opp_elo"] >= r["elo"] for g in recent) >= min_games]
