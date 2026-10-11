"""One ladder game: the harness's player against a seeded ladder Bot, from a book line, to a real result.

  play_game(harness, slot, out_dir, run_id) -> record

- Opening: a book line from openings.json for the slot's family, picked by the seed; the agent plays slot["color"].
- Opponent: a ladder Bot at the slot's Elo (rating.rung_for_elo), seeded by the slot seed, so a replay is exact.
- Ends: mate, stalemate, insufficient material, threefold / 50-move (claimed automatically), a forfeit (three
  unusable replies in a row), or a cap at move 100 / 1500 s of play, scored FRACTIONALLY from the measured
  conversion table (ADJUDICATION): at low Elo a big eval lead converts only about half the time.
- Infra (model unreachable, engine dead, SIGTERM, a bug here) never becomes a chess result: status "infra", score
  None, and the checkpoint is kept so the next call resumes the same game without buying any move twice.
- Files: out_dir/<slot>.state.json after every ply, out_dir/<slot>.json (the record, atomic, written in `finally`),
  and a viewer copy at tasks/chess/_replays/<RUN_ID>-<vid>/<family>-<color>-<seedhash>.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import signal
import threading
import time
import traceback
from pathlib import Path

import chess
import chess.engine

from rsi import perceive, reliable
from rsi.ladder import ENGINE_ERRORS, STOCKFISH, Bot, label
from rsi.reliable import InfraError, Stopped

HERE = Path(__file__).parent
OPENINGS_JSON = HERE / "openings.json"
REPLAYS = HERE.parent / "tasks" / "chess" / "_replays"
MAX_MOVES = 100
TIME_CAP = 1500.0
ADJUDICATE_DEPTH = 12

# Leader's expected score once the eval gap reaches X centipawns, by the opponent's Elo band (measured over 1,428
# calibration games, research adjud.py; e.g. below 600 a 15-pawn lead wins 50%, draws 43%, loses 7%):
# {band upper bound: [(X, score), ...]} checked from the largest X down. Below 300 cp the game is scored 0.5.
ADJUDICATION = {
    600: [(1500, 0.72), (800, 0.67), (300, 0.61)],
    1200: [(1500, 0.90), (800, 0.82), (300, 0.72)],
    1800: [(1500, 0.93), (800, 0.87), (300, 0.79)],
    99999: [(1500, 0.98), (800, 0.95), (300, 0.89)],
}

# Record fields that differ between an interrupted-and-resumed game and an uninterrupted one.
VOLATILE = ("elapsed_s", "resumed", "started", "finished", "wall_s", "latency")


def load_openings(path: Path | str = OPENINGS_JSON) -> list[dict]:
    """[{family, eco, split, lines: [[uci, ...], ...]}, ...]"""
    return json.loads(Path(path).read_text())["families"]


def families(split: str | None = None, path: Path | str = OPENINGS_JSON) -> list[str]:
    """Family names, optionally only the "train" or "held" ones."""
    return [f["family"] for f in load_openings(path) if split in (None, f["split"])]


def _h(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


def opening_for(family: str, seed: str, path: Path | str = OPENINGS_JSON) -> tuple[dict, list[str]]:
    """(family entry, the seed's book line)."""
    fam = next((f for f in load_openings(path) if f["family"] == family), None)
    if fam is None:
        raise ValueError(f"unknown opening family {family!r}")
    return fam, list(fam["lines"][int(_h(seed, "line"), 16) % len(fam["lines"])])


def safe(s) -> str:
    """A path part the replay viewer can serve ([\\w-]+)."""
    return re.sub(r"[^\w-]", "-", str(s)) or "x"


def game_name(slot: dict) -> str:
    return safe(f"{slot['family']}-{slot.get('color', 'white')}-{_h(slot['seed'])[:8]}")


def adjudicated_white_score(cp_white: int, band_elo: float) -> float:
    """White's fractional score for a capped game, from the eval and the opponent's Elo band."""
    table = next(v for k, v in sorted(ADJUDICATION.items()) if band_elo < k)
    for x, s in table:
        if abs(cp_white) >= x:
            return s if cp_white > 0 else 1 - s
    return 0.5


def eval_white(board: chess.Board, engine_path: str = STOCKFISH, depth: int = ADJUDICATE_DEPTH) -> int:
    """Stockfish's eval of the final position in centipawns from White's side (mate = +-100000)."""
    last = None
    for _ in range(3):
        eng = None
        try:
            eng = chess.engine.SimpleEngine.popen_uci(engine_path, timeout=20)
            eng.configure({"Threads": 1, "Hash": 16})
            info = eng.analyse(board, chess.engine.Limit(depth=depth, time=10))
            return info["score"].white().score(mate_score=100000)
        except ENGINE_ERRORS as e:
            last = e
        finally:
            if eng is not None:
                try:
                    eng.quit()
                except Exception:  # noqa: BLE001 - already dead
                    pass
    raise InfraError("engine", f"adjudication eval failed 3 times: {last!r}")


def _resolve_opp(slot: dict) -> dict:
    if slot.get("opp"):
        return slot["opp"]
    from rsi.rating import rung_for_elo
    return rung_for_elo(slot.get("elo", slot.get("target", 100)))


def _default_choose(board, harness, ctx):
    from rsi import player
    return player.choose(board, harness, ctx)


def play_game(harness: dict, slot: dict, out_dir: Path | str, run_id: str, *, choose=None, opponent=None,
              max_moves: int = MAX_MOVES, time_cap: float = TIME_CAP, replays_dir: Path | str | None = REPLAYS,
              engine_path: str = STOCKFISH, log=None) -> dict:
    """Play (or resume, or return the finished record of) one game. Never raises for infra: the record says
    status "infra" and keeps the checkpoint. Stopped (SIGTERM/SIGINT) is re-raised after the record is written.

    slot: {id, seed, family, color "white"|"black", split?, target (generation centre, for rating), elo? (this
    slot's opponent target, default target), opp? ({name, spec, elo}: overrides elo), opening? ([uci]: overrides
    the book), gen?}
    choose(board, harness, ctx) -> {move, reason, attempts, calls, cost, ...}: default rsi.player.choose.
    opponent: an object with .move(board) (default: a ladder Bot for the slot, closed here).
    """
    out_dir = Path(out_dir)
    sid = safe(slot["id"])
    rec_path, state_path = out_dir / f"{sid}.json", out_dir / f"{sid}.state.json"
    if rec_path.exists():
        try:
            done = json.loads(rec_path.read_text())
            if done.get("status") == "complete":
                return done
        except ValueError:
            pass

    choose = choose or _default_choose
    seed, color = str(slot["seed"]), slot.get("color", "white")
    mine = chess.WHITE if color == "white" else chess.BLACK
    if slot.get("opening") is not None:
        fam, line = {"family": slot["family"], "eco": slot.get("eco"), "split": slot.get("split")}, list(slot["opening"])
    else:
        fam, line = opening_for(slot["family"], seed)
    opp = _resolve_opp(slot)
    name = game_name(slot)
    tools = list(harness.get("tools") or [])

    # ---- resume a checkpoint of this same game, or start from the book line
    st = None
    if state_path.exists():
        try:
            st = json.loads(state_path.read_text())
            if st.get("seed") != seed or st.get("harness") != harness.get("id"):
                st = None
        except ValueError:
            st = None
    board = chess.Board()
    if st:
        moves, per_move, elapsed, resumed = st["moves"], st["per_move"], st["elapsed_s"], st["resumed"] + 1
        started = st.get("started")
    else:
        moves, per_move, elapsed, resumed, started = list(line), [], 0.0, 0, time.time()
    san = []
    for u in moves:
        mv = chess.Move.from_uci(u)
        san.append(board.san(mv))
        board.push(mv)
    if log:
        log({"kind": "resume" if st else "opening", "game": name, "ply": len(board.move_stack), "fen": board.fen()})

    status, how, white, forfeit = "infra", None, None, None
    infra = {"kind": None, "detail": None, "reset_at": None}
    adjud, models = None, []

    def checkpoint():
        reliable.atomic_write_json(state_path, {"seed": seed, "harness": harness.get("id"), "slot": slot["id"],
                                                "moves": [m.uci() for m in board.move_stack], "per_move": per_move,
                                                "elapsed_s": round(elapsed, 2), "resumed": resumed,
                                                "started": started})

    def record() -> dict:
        score = None if white is None else (white if mine == chess.WHITE else 1 - white)
        if status != "complete":
            result = None
        elif how in ("move cap", "time cap"):
            result = "*"
        elif forfeit:
            result = "0-1" if mine == chess.WHITE else "1-0"
        else:
            result = board.result(claim_draw=True)
        calls = sum(p.get("calls") or 0 for p in per_move)
        return {
            # viewer keys (same as tasks/chess/_agent.py's replays)
            "game": name, "harness": harness.get("id"), "agent_plays": color, "moves": [m.uci() for m in board.move_stack],
            "san": san, "final_fen": board.fen(), "forfeit": forfeit,
            # result
            "status": status, "result": result, "score": score, "how": how, "adjudication": adjud,
            "infra": infra["detail"], "infra_kind": infra["kind"], "reset_at": infra["reset_at"],
            # what the game was
            "run": run_id, "slot": slot["id"], "gen": slot.get("gen"), "seed": seed, "color": color,
            "family": fam["family"], "eco": fam.get("eco"), "split": slot.get("split") or fam.get("split"),
            "opening_plies": len(line), "target": slot.get("target"), "slot_elo": slot.get("elo", slot.get("target")),
            "opp": {"name": opp.get("name"), "elo": opp.get("elo"), "spec": opp.get("spec"),
                    "label": label(opp["spec"]) if opp.get("spec") else None},
            "opp_elo": opp.get("elo"), "model": next((m for m in models if m), harness.get("model")),
            "think": harness.get("think", 0), "tools": tools, "tier": perceive.tier(tools),
            "plies": len(board.move_stack), "per_move": per_move, "calls": calls,
            "cost_usd": round(sum(p.get("cost") or 0 for p in per_move), 6),
            "max_moves": max_moves, "time_cap_s": time_cap,
            "elapsed_s": round(elapsed, 2), "resumed": resumed, "started": started, "finished": time.time(),
        }

    old_handlers = None
    if threading.current_thread() is threading.main_thread():
        old_handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        reliable.install_stop_handlers()
    bot = None
    try:
        if opponent is None:
            try:
                bot = opponent = Bot(opp["spec"], seed=f"{seed}|opp", engine_path=engine_path)
            except ENGINE_ERRORS as e:
                raise InfraError("engine", f"cannot start stockfish: {e!r}") from None
        while True:
            if board.is_game_over(claim_draw=True):
                o = board.outcome(claim_draw=True)
                white = 1.0 if o.winner == chess.WHITE else 0.0 if o.winner == chess.BLACK else 0.5
                how = o.termination.name.lower()
                break
            if board.fullmove_number > max_moves or elapsed >= time_cap:
                how = "move cap" if board.fullmove_number > max_moves else "time cap"
                cp = eval_white(board, engine_path)
                white = adjudicated_white_score(cp, opp.get("elo") or 0)
                adjud = {"cp_white": cp, "band_elo": opp.get("elo"), "white_score": white}
                break
            t0, ply = time.time(), len(board.move_stack)
            if board.turn == mine:
                r = choose(board, harness, {"history": list(san), "mine": color, "purpose": "game", "ply": ply,
                                            "game": name})
                mv = r.get("move")
                if mv is not None and not isinstance(mv, chess.Move):
                    mv = chess.Move.from_uci(str(mv))
                models.append(r.get("model"))
                pm = {"ply": ply, "move": mv.uci() if mv else None, "reason": r.get("reason") or "",
                      "attempts": r.get("attempts") or [], "calls": r.get("calls") or len(r.get("attempts") or []) or 1,
                      "cost": r.get("cost") or 0.0, "think": r.get("think_tokens", harness.get("think", 0)),
                      "wall_s": round(time.time() - t0, 2)}
                if mv is None or mv not in board.legal_moves:
                    per_move.append(pm)
                    tried = ", ".join(a.get("move") or "(no move)" for a in pm["attempts"]) or "(no reply)"
                    forfeit = f"forfeit at move {board.fullmove_number}: three unusable replies ({tried})"
                    white, how = (0.0 if mine == chess.WHITE else 1.0), "forfeit"
                    break
                per_move.append(pm)
                by = "agent"
                # Only the model's own reply time counts toward the time cap: infra backoff and usage-limit waits
                # inside a call must never push a game into adjudication.
                spent = [a["wall_s"] for a in pm["attempts"] if isinstance(a, dict) and a.get("wall_s") is not None]
                took = sum(spent) if spent else time.time() - t0
            else:
                mv, by = opponent.move(board), "opponent"
                took = time.time() - t0
            san.append(board.san(mv))
            board.push(mv)
            elapsed += took
            checkpoint()
            if log:
                log({"kind": "move", "by": by, "san": san[-1], "fen": board.fen(), "ply": ply + 1, "game": name})
        status = "complete"
    except InfraError as e:
        infra = {"kind": e.kind, "detail": str(e)[:500], "reset_at": getattr(e, "reset_at", None)}
    except Stopped as e:
        infra = {"kind": "stopped", "detail": f"stopped: {e}", "reset_at": None}
        raise
    except Exception as e:  # noqa: BLE001 - a bug here is ours, not the player's: never a chess loss
        infra = {"kind": "bug", "detail": f"{type(e).__name__}: {e} | {traceback.format_exc()[-400:]}",
                 "reset_at": None}
    finally:
        if bot is not None:
            bot.close()
        if status != "complete":
            white, how = None, None
        rec = record()
        reliable.atomic_write_json(rec_path, rec)
        if replays_dir:
            reliable.atomic_write_json(Path(replays_dir) / f"{safe(run_id)}-{safe(harness.get('id'))}" / f"{name}.json",
                                       rec, indent=1)
        if status == "complete":
            state_path.unlink(missing_ok=True)
        elif not state_path.exists() and board.move_stack:
            checkpoint()
        if old_handlers:
            signal.signal(signal.SIGTERM, old_handlers[0])
            signal.signal(signal.SIGINT, old_handlers[1])
        if log:
            log({"kind": "game_end", "game": name, "status": status, "how": how, "score": rec["score"],
                 "infra": rec["infra"]})
    return rec


def strip_volatile(rec: dict) -> dict:
    """The record without timing/resume fields: equal for a resumed game and an uninterrupted one."""
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in VOLATILE}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(rec)
