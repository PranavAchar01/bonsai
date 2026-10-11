"""Elo maths for the ladder (pure Python).

expected(diff)              logistic expected score for a rating difference
performance(results)        one player's rating from games vs known-rated opponents: MAP with a weak prior and a
                            profile-likelihood 95% interval
fit_ratings(games, anchors) Bradley-Terry over many players (ladder calibration), shifted onto the anchors
bootstrap(games, anchors)   percentile CIs for fit_ratings by resampling games within each pairing
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

K = math.log(10) / 400.0


def expected(diff: float) -> float:
    """Expected score of a player rated `diff` above the opponent."""
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def loglik(r: float, results) -> float:
    """results: [(opp_elo, score)], score in [0, 1]."""
    ll = 0.0
    for opp, s in results:
        p = min(1 - 1e-12, max(1e-12, expected(r - opp)))
        ll += s * math.log(p) + (1 - s) * math.log(1 - p)
    return ll


def performance(results, prior_mean: float | None = None, prior_sd: float = 400.0, opp_sd: float = 0.0) -> dict:
    """Rating from [(opp_elo, score)]: posterior mode on a 1-Elo grid, 95% interval where the log-posterior drops
    by 1.92. prior_mean defaults to the mean opponent; the prior only matters at 100% or 0% scores. opp_sd (the
    opponents' own rating error) is added to the interval in quadrature."""
    results = [(float(o), float(s)) for o, s in results]
    if not results:
        return {"elo": prior_mean, "lo": None, "hi": None, "n": 0, "score": 0.0}
    opps = [o for o, _ in results]
    pm = prior_mean if prior_mean is not None else sum(opps) / len(opps)

    def post(r):
        return loglik(r, results) - 0.5 * ((r - pm) / prior_sd) ** 2

    lo_g, hi_g = int(min(opps)) - 1500, int(max(opps)) + 1500
    grid = range(lo_g, hi_g + 1)
    vals = [post(r) for r in grid]
    best_i = max(range(len(vals)), key=vals.__getitem__)
    inside = [grid[i] for i, v in enumerate(vals) if v >= vals[best_i] - 1.92]
    est, lo, hi = grid[best_i], inside[0], inside[-1]
    if opp_sd:
        lo = est - math.sqrt((est - lo) ** 2 + (1.96 * opp_sd) ** 2)
        hi = est + math.sqrt((hi - est) ** 2 + (1.96 * opp_sd) ** 2)
    n, score = len(results), sum(s for _, s in results)
    return {"elo": est, "lo": round(lo), "hi": round(hi), "n": n, "score": score,
            "avg_opp": round(sum(opps) / n), "all_won": score == n, "all_lost": score == 0}


def se_from_games(n: int, p: float = 0.5, draw_rate: float = 0.0) -> float:
    """Approximate 1-sigma Elo error after n games scoring p against a known opponent."""
    var_game = max(p * (1 - p) - draw_rate / 4.0, 1e-6)
    return math.sqrt(var_game / n) / (K * p * (1 - p))


def _pairs(games):
    """games: iterable of (a, b, score_a) -> {(a, b): [n, score_a_sum]} with a < b."""
    acc = defaultdict(lambda: [0, 0.0])
    for a, b, s in games:
        if a > b:
            a, b, s = b, a, 1.0 - s
        acc[(a, b)][0] += 1
        acc[(a, b)][1] += s
    return acc


def fit_ratings(games, anchors: dict, prior_draws: float = 1.0, iters: int = 5000, tol: float = 1e-9) -> dict:
    """Bradley-Terry by MM (Hunter 2004), draws as half points, `prior_draws` virtual draws per pairing so 100%
    scores stay finite. Returns {player: elo} shifted so anchors sit at their given ratings."""
    acc = _pairs(games)
    players = sorted({p for ab in acc for p in ab})
    wins, n = defaultdict(float), {}
    for (a, b), (cnt, sa) in acc.items():
        wins[a] += sa + 0.5 * prior_draws
        wins[b] += (cnt - sa) + 0.5 * prior_draws
        n[(a, b)] = cnt + prior_draws
    gamma = {p: 1.0 for p in players}
    for _ in range(iters):
        new = {}
        for p in players:
            denom = sum(c / (gamma[a] + gamma[b]) for (a, b), c in n.items() if p in (a, b))
            new[p] = wins[p] / denom if denom > 0 else gamma[p]
        g = math.exp(sum(math.log(v) for v in new.values()) / len(new))
        new = {p: v / g for p, v in new.items()}
        delta = max(abs(math.log(new[p]) - math.log(gamma[p])) for p in players)
        gamma = new
        if delta < tol:
            break
    elo = {p: 400.0 * math.log10(gamma[p]) for p in players}
    present = {p: v for p, v in anchors.items() if p in elo}
    if present:
        shift = sum(v - elo[p] for p, v in present.items()) / len(present)
        elo = {p: r + shift for p, r in elo.items()}
    return elo


def bootstrap(games, anchors: dict, reps: int = 200, seed: int = 1, prior_draws: float = 1.0) -> dict:
    """{player: (lo, hi, mean, sd)}: 95% percentile intervals from resampling games within each pairing."""
    by_pair = defaultdict(list)
    for g in games:
        by_pair[(g[0], g[1])].append(g)
    rng = random.Random(seed)
    samples = defaultdict(list)
    for _ in range(reps):
        res = [rng.choice(gs) for gs in by_pair.values() for _ in gs]
        for p, r in fit_ratings(res, anchors, prior_draws=prior_draws, iters=2000, tol=1e-7).items():
            samples[p].append(r)
    out = {}
    for p, xs in samples.items():
        xs.sort()
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1))
        out[p] = (xs[int(0.025 * len(xs))], xs[int(0.975 * len(xs)) - 1], m, sd)
    return out
