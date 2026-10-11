"""Opponent ladder: seedable Stockfish-based bots from a random mover up to ~2400 (Bonsai ladder Elo).

A rung spec is a JSON-friendly dict. Kinds:

  {"kind": "random"}                                  uniform random legal move
  {"kind": "policy", "eps": p, "temp": T, "depth": d} with prob p a uniform random legal move; otherwise Stockfish
                                                      scores every legal move at depth d (MultiPV = all) and we
                                                      sample softmax(cp / (100*T)). temp 0 = best move.
  {"kind": "sf_elo", "elo": E}                        Stockfish UCI_LimitStrength + UCI_Elo=E, searched to depth
                                                      1+int(skill level). NOT reproducible: Stockfish seeds its
                                                      pick from the clock.

All randomness in random/policy rungs comes from random.Random seeded by (seed, FEN), and the engine runs with
Threads=1, a fixed depth and `ucinewgame` before every move, so a policy rung's reply is a pure function of
(position, seed). That keeps parent/child comparisons paired and lets games be replayed exactly.

Engine failures (crash, hang, cancelled future) restart Stockfish up to twice per move; after that the move
raises InfraError("engine"): never a chess result.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import random
import shutil
from pathlib import Path

import chess
import chess.engine

try:
    from rsi.reliable import InfraError
except ImportError:  # reliable.py not built yet: same shape, so callers can catch it by kind
    class InfraError(Exception):
        """Something other than chess went wrong. Never scored as a loss."""

        def __init__(self, kind: str, detail: str = "", retry_after: float | None = None):
            super().__init__(f"{kind}: {detail[:300]}")
            self.kind, self.detail, self.retry_after = kind, detail, retry_after

STOCKFISH = "/opt/homebrew/bin/stockfish" if Path("/opt/homebrew/bin/stockfish").exists() else (
    shutil.which("stockfish") or "stockfish")
LADDER_JSON = Path(__file__).with_name("ladder.json")
MATE_CP = 2000      # mate scores are clipped to this before the softmax
HASH_MB = 16
SAFETY_SECS = 10.0  # Limit.time cap so a hung engine times out; depth <= 3 finishes in milliseconds
RESTARTS = 2
ENGINE_ERRORS = (chess.engine.EngineError, chess.engine.EngineTerminatedError, concurrent.futures.CancelledError,
                 concurrent.futures.TimeoutError, TimeoutError, OSError, BrokenPipeError)


def sf_level(elo: int) -> float:
    """Stockfish 16-19's mapping from UCI_Elo to its internal skill level (search.h, Skill::Skill)."""
    e = (elo - 1320) / (3190 - 1320)
    return min(19.0, max(0.0, ((37.2473 * e - 40.8525) * e + 22.2943) * e - 0.311438))


def load_ladder(path: Path = LADDER_JSON) -> dict:
    """The whole ladder.json document."""
    return json.loads(Path(path).read_text())


def stockfish_version(path: str = STOCKFISH) -> str:
    """Engine name as reported over UCI, e.g. 'Stockfish 19'. Compared against ladder.json by the doctor."""
    eng = chess.engine.SimpleEngine.popen_uci(path, timeout=20)
    try:
        return eng.id.get("name", "")
    finally:
        eng.quit()


class Bot:
    """One opponent on the ladder. Owns its own Stockfish process (if the rung needs one). Use as a context manager."""

    def __init__(self, spec: dict, seed: str = "0", engine_path: str = STOCKFISH):
        self.spec, self.seed, self.engine_path = dict(spec), str(seed), engine_path
        self.engine, self.restarts = None, 0
        if self.spec["kind"] not in ("random", "policy", "sf_elo"):
            raise ValueError(f"unknown rung kind {self.spec['kind']!r}")
        self.depth = 1 + int(sf_level(int(self.spec["elo"]))) if self.spec["kind"] == "sf_elo" else \
            int(self.spec.get("depth", 1))
        self._start()

    def _start(self) -> None:
        kind = self.spec["kind"]
        if kind == "random":
            return
        self.engine = chess.engine.SimpleEngine.popen_uci(self.engine_path, timeout=20)
        self.engine.configure({"Threads": 1, "Hash": HASH_MB})
        if kind == "sf_elo":
            self.engine.configure({"UCI_LimitStrength": True, "UCI_Elo": int(self.spec["elo"])})

    def rng(self, board: chess.Board) -> random.Random:
        return random.Random(f"{self.seed}|{board.fen()}")

    def move(self, board: chess.Board) -> chess.Move:
        """The rung's reply. Restarts a dead or hung engine up to twice, then raises InfraError('engine')."""
        last = None
        for attempt in range(RESTARTS + 1):
            try:
                if attempt and self.engine is None:
                    self._start()
                return self._move(board)
            except ENGINE_ERRORS as e:
                last = e
                self.close()
                self.restarts += attempt < RESTARTS
        raise InfraError("engine", f"stockfish failed {RESTARTS + 1} times: {last!r}")

    def _move(self, board: chess.Board) -> chess.Move:
        legal = list(board.legal_moves)
        if not legal:
            raise ValueError("no legal moves: the game is over")
        kind, rng = self.spec["kind"], self.rng(board)
        if kind == "random" or (kind == "policy" and rng.random() < float(self.spec.get("eps", 0.0))):
            return rng.choice(legal)
        if len(legal) == 1:
            return legal[0]
        if self.engine is None:
            self._start()
        # game=object(): a fresh game every call, so python-chess sends ucinewgame (clears the hash)
        if kind == "sf_elo":
            return self.engine.play(board, chess.engine.Limit(depth=self.depth, time=2.0), game=object()).move
        temp = float(self.spec.get("temp", 0.0))
        if temp <= 0:
            return self.engine.play(board, chess.engine.Limit(depth=self.depth, time=SAFETY_SECS),
                                    game=object()).move
        infos = self.engine.analyse(board, chess.engine.Limit(depth=self.depth, time=SAFETY_SECS),
                                    multipv=len(legal), game=object())
        scored = []
        for info in infos:
            if info.get("pv"):
                cp = info["score"].pov(board.turn).score(mate_score=100000)
                scored.append((info["pv"][0], max(-MATE_CP, min(MATE_CP, cp))))
        if not scored:
            return rng.choice(legal)
        top = max(s for _, s in scored)
        weights = [math.exp((s - top) / (100.0 * temp)) for _, s in scored]
        return rng.choices([m for m, _ in scored], weights=weights, k=1)[0]

    def close(self) -> None:
        if self.engine is not None:
            try:
                self.engine.quit()
            except Exception:  # noqa: BLE001 - already dead
                try:
                    self.engine.close()
                except Exception:  # noqa: BLE001
                    pass
            self.engine = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def label(spec: dict) -> str:
    """Short readable name for a spec, e.g. 'eps0.556_T8_d1', 'sf_elo2100', 'random'."""
    k = spec["kind"]
    if k == "random":
        return "random"
    if k == "sf_elo":
        return f"sf_elo{spec['elo']}"
    parts = [f"eps{spec['eps']:g}"] if spec.get("eps") else []
    if spec.get("temp"):
        parts.append(f"T{spec['temp']:g}")
    parts.append(f"d{spec.get('depth', 1)}")
    return "_".join(parts)
