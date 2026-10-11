"""Exam position bank: positions from HELD-family games, a frozen anchor set, and a Stockfish eval cache.

  bank.jsonl    one position per line, appended after each generation's held games are analysed
  anchor.jsonl  the first ANCHOR_N bank positions after the seed, frozen forever (progress curve)
  evalcache.json  {fen: centipawns from White's side}, shared by every exam so no position is searched twice

A position enters the bank when the agent lost >= MIN_LOSS win-points there in its game, or when it is sharp
(the best move beats the second best by >= SHARP_GAP win-points). Exams always ask for FRESH answers from both arms:
the stored game move is never used as the parent's answer.

Position fields: id, fen, history [san], mine, src, family, opp_elo, gen, game_loss, best, sharp.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
from pathlib import Path

import chess

from rsi import mine as M

MIN_LOSS = 10.0
SHARP_GAP = 15.0
ANCHOR_N = 120
FORFEIT_LOSS = 50.0  # no usable move after all attempts: scored as a catastrophic move


def pos_id(fen: str) -> str:
    return hashlib.sha1(fen.encode()).hexdigest()[:12]


class Scorer:
    """Win-chance loss of a move vs the best move, from cached Stockfish evals. Thread-safe (one engine, one lock)."""

    def __init__(self, cache_path: Path | str | None = None, nodes: int = M.NODES, engine: M.Engine | None = None):
        self.path = Path(cache_path) if cache_path else None
        self.cache = json.loads(self.path.read_text()) if self.path and self.path.exists() else {}
        self.nodes, self.engine, self.lock, self.dirty = nodes, engine, threading.Lock(), 0

    def cp(self, board: chess.Board) -> int:
        """Eval from White's side; mate and stalemate handled without the engine."""
        key = board.fen()
        if key not in self.cache:
            if board.is_checkmate():
                self.cache[key] = -M.MATE_CP if board.turn == chess.WHITE else M.MATE_CP
            elif board.is_game_over(claim_draw=False):
                self.cache[key] = 0
            else:
                if self.engine is None:
                    self.engine = M.Engine(self.nodes)
                self.cache[key] = M.cp_of(self.engine.analyse(board)["score"], chess.WHITE)
            self.dirty += 1
        return self.cache[key]

    def loss(self, fen: str, move: chess.Move | str | None) -> float:
        """Win-points the mover gives up by playing `move` (None = forfeit) instead of the engine's best."""
        if move is None:
            return FORFEIT_LOSS
        with self.lock:
            b = chess.Board(fen)
            pov = 1 if b.turn == chess.WHITE else -1
            best = pov * self.cp(b)
            b.push(chess.Move.from_uci(move) if isinstance(move, str) else move)
            after = pov * self.cp(b)
        return round(max(0.0, M.win_pct(best) - M.win_pct(after)), 1)

    def __call__(self, pos: dict, move) -> float:
        return self.loss(pos["fen"], move)

    def save(self) -> None:
        if self.path and self.dirty:
            from rsi import reliable
            with self.lock:
                reliable.atomic_write_json(self.path, self.cache)
                self.dirty = 0

    def close(self) -> None:
        self.save()
        if self.engine is not None:
            self.engine.close()
            self.engine = None


def candidates(rec: dict, analysed: dict, engine: M.Engine, gen: int, cache: dict | None = None) -> list[dict]:
    """Bank entries from one analysed held game. Fills `cache` with the evals already computed."""
    out = []
    for m in analysed["moves"]:
        b = chess.Board(m["fen"])
        if cache is not None:
            pov = 1 if b.turn == chess.WHITE else -1
            cache.setdefault(m["fen"], pov * m["cp_before"])
        sharp = False
        if m["loss"] < MIN_LOSS and b.legal_moves.count() >= 2:
            infos = engine.analyse(b, multipv=2)
            if len(infos) >= 2:
                w = [M.win_pct(M.cp_of(i["score"], b.turn)) for i in infos[:2]]
                sharp = w[0] - w[1] >= SHARP_GAP
        if m["loss"] >= MIN_LOSS or sharp:
            out.append({"id": pos_id(m["fen"]), "fen": m["fen"], "history": m["history"], "mine": analysed["color"],
                        "src": f"{analysed['game']}:{m['ply']}", "family": rec.get("family"),
                        "opp_elo": rec.get("opp_elo"), "gen": gen, "game_loss": m["loss"], "best": m["best_uci"],
                        "sharp": sharp})
    return out


def add_held_games(records: list[dict], bank_path: Path | str, gen: int, scorer: Scorer | None = None,
                   engine: M.Engine | None = None) -> int:
    """Analyse complete HELD games and append their new positions to bank.jsonl. Returns how many were added."""
    from rsi import reliable
    train = [r.get("game") or r.get("slot") for r in records if r.get("split") != "held"]
    if train:
        raise ValueError(f"the bank takes held-family games only; got {train[:5]}")
    done = [r for r in records if r.get("status", "complete") == "complete" and r.get("moves")]
    seen = {p["id"] for p in reliable.read_jsonl(bank_path)}
    own = engine is None
    engine = engine or M.Engine()
    added = 0
    try:
        for r in done:
            a = M.evaluate_game(r, engine)
            for p in candidates(r, a, engine, gen, scorer.cache if scorer else None):
                if p["id"] not in seen:
                    seen.add(p["id"])
                    reliable.append_jsonl(bank_path, p)
                    added += 1
    finally:
        if own:
            engine.close()
    if scorer:
        scorer.dirty += 1
        scorer.save()
    return added


def freeze_anchor(bank_path: Path | str, anchor_path: Path | str, n: int = ANCHOR_N) -> list[dict]:
    """Write anchor.jsonl once from the first n bank positions; afterwards it is only read, never rewritten."""
    from rsi import reliable
    anchor_path = Path(anchor_path)
    if anchor_path.exists():
        return reliable.read_jsonl(anchor_path)
    rows = reliable.read_jsonl(bank_path)[:n]
    if len(rows) < n:
        raise ValueError(f"bank has {len(rows)} positions, anchor needs {n}")
    tmp = anchor_path.with_name(anchor_path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(anchor_path)
    return rows


def select(bank: list[dict], elo: float | None, n: int, seed: str = "", exclude: set[str] | None = None) -> list[dict]:
    """n positions nearest the champion's rung (by opponent Elo), shuffled with a fixed seed so looks are random
    subsets. Positions without an opp_elo rank last."""
    pool = [p for p in bank if not exclude or p["id"] not in exclude]
    rng = random.Random(seed)
    rng.shuffle(pool)  # random tie-break among equally near positions
    if elo is not None:
        pool.sort(key=lambda p: abs(p["opp_elo"] - elo) if p.get("opp_elo") is not None else float("inf"))
    picked = pool[:n]
    rng.shuffle(picked)
    return picked
