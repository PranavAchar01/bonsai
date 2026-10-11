"""Mistake miner: Stockfish post-game analysis of the agent's moves, motif tags, and the proposer's digest.

  python -m rsi.mine <game.json|dir> ... [--nodes 100000] [--out digest.md] [--json mined.json]

Only TRAIN-family games may be mined into a digest: held-family games feed the exam bank (rsi.bank) and must never
reach the proposer, so mine_games() and digest() raise HeldGameError on any game whose split is not "train".
Games whose status is not "complete" (infra) are dropped silently: infra never appears in the digest.

Game record fields used (from rsi.game): moves [uci], color|agent_plays, split, status, opening_plies, game|slot,
per_move [{ply, reason|said, attempts:[{kind, move|text}]}].

Scoring follows Lichess: win% = 50 + 50*(2/(1+exp(-0.00368208*cp)) - 1) from the mover's side; a move's loss is the
drop in the mover's win%; >=15 blunder, >=10 mistake, >=5 inaccuracy.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import math
import sys
from pathlib import Path

import chess
import chess.engine

from rsi import perceive as P

STOCKFISH = "/opt/homebrew/bin/stockfish"
NODES = 100_000
MATE_CP = 10_000
DIGEST_TOKENS = 2500
CHARS_PER_TOKEN = 3.5  # conservative for markdown with FENs


class HeldGameError(AssertionError):
    """A held-family game reached the miner. Held games are exam material only."""


class EngineDown(Exception):
    """Stockfish died and would not restart. Infra, not chess."""


# ------------------------------------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------------------------------------


def win_pct(cp: int) -> float:
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def accuracy(loss: float) -> float:
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * loss) - 3.1669))


def cp_of(score: chess.engine.PovScore, pov: bool) -> int:
    return score.pov(pov).score(mate_score=MATE_CP)


def classify(loss: float) -> str | None:
    return "blunder" if loss >= 15 else "mistake" if loss >= 10 else "inaccuracy" if loss >= 5 else None


def phase(board: chess.Board, ply: int) -> str:
    heavy = P.material(board, chess.WHITE) + P.material(board, chess.BLACK)
    return "opening" if ply < 20 else "endgame" if heavy <= 26 else "middlegame"


class Engine:
    """Stockfish for analysis: fixed nodes (load-independent), Threads=1, nice 10, restarted up to twice."""

    def __init__(self, nodes: int = NODES, path: str = STOCKFISH, hash_mb: int = 16):
        self.nodes, self.path, self.hash_mb, self.eng = nodes, path, hash_mb, None
        self._start()

    def _start(self) -> None:
        self.eng = chess.engine.SimpleEngine.popen_uci(["nice", "-n", "10", self.path], setpgrp=True)
        self.eng.configure({"Threads": 1, "Hash": self.hash_mb})

    def analyse(self, board: chess.Board, multipv: int | None = None, game=None):
        for attempt in range(3):
            try:
                return self.eng.analyse(board, chess.engine.Limit(nodes=self.nodes), multipv=multipv, game=game)
            except (chess.engine.EngineTerminatedError, chess.engine.EngineError, concurrent.futures.CancelledError) as e:
                last = e
                self._kill()
                if attempt < 2:
                    self._start()
        raise EngineDown(f"stockfish down after 2 restarts: {last!r}")

    def _kill(self) -> None:
        try:
            self.eng.quit()
        except Exception:  # already dead
            try:
                self.eng.close()
            except Exception:
                pass

    def close(self) -> None:
        self._kill()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ------------------------------------------------------------------------------------------------
# Motifs
# ------------------------------------------------------------------------------------------------

MOTIF_TEXT = {
    "allowed_mate_in_1": "allowed mate in one",
    "missed_mate_in_1": "had mate in one and played something else",
    "hung_moved_piece": "moved a piece to a square where it is lost",
    "hung_other_piece": "move left another piece loose (defender moved away / line opened)",
    "ignored_threat": "a piece was already attacked and the move did not save it",
    "missed_free_capture": "an opponent piece could be won and the move ignored it",
    "losing_capture": "captured into a losing exchange",
    "hung_pawn": "pawn moved or captured onto a square where it is simply lost",
    "walked_into_fork": "allowed a fork",
    "missed_fork": "missed a winning fork",
    "positional": "no tactical motif: slow or planless move that still cost win chance",
}

# Was the fact behind the motif on the board BEFORE the move ("pre": a static tool could show it), or created by the
# move ("post": only checking the position after the candidate move reveals it)? Plus the tier-2 tool that shows it.
VISIBILITY = {
    "ignored_threat": ("pre", "threats"),
    "missed_free_capture": ("pre", "hanging"),
    "missed_mate_in_1": ("pre", "checks"),
    "missed_fork": ("pre", None),
    "hung_moved_piece": ("post", None),
    "hung_other_piece": ("post", None),
    "hung_pawn": ("post", None),
    "losing_capture": ("post", None),
    "allowed_mate_in_1": ("post", None),
    "walked_into_fork": ("post", None),
    "positional": ("-", None),
}


def tag(board: chess.Board, mv: chess.Move, best: chess.Move | None, reply_pv: list[chess.Move], mine: bool) -> list[str]:
    """Engine-free motif labels for one agent move. The engine only supplies the best move and the punishing reply."""
    tags = []
    m1 = P.mates_in_one(board)
    if m1 and mv not in m1:
        tags.append("missed_mate_in_1")
    gain = P.see(board, mv) if board.is_capture(mv) else 0
    if board.is_capture(mv) and gain < 0:
        tags.append("losing_capture")
    threatened_before = dict(P.en_prise(board, mine, 2))
    free = [m for m in board.generate_legal_captures() if P.see(board, m) >= 2]
    after = board.copy()
    after.push(mv)
    if P.mates_in_one(after):
        tags.append("allowed_mate_in_1")
    threatened_after = dict(P.en_prise(after, mine, 2))
    if threatened_after.get(mv.to_square, 0) - max(gain, 0) >= 2 and not after.is_check():
        tags.append("hung_moved_piece")  # net of what the move captured, so even trades are not "hung"
    elif (board.piece_type_at(mv.from_square) == chess.PAWN
          and P.threat_on(after, mv.to_square) - max(gain, 0) >= 1 and not after.is_check()):
        tags.append("hung_pawn")
    if [s for s in threatened_after if s != mv.to_square and s not in threatened_before]:
        tags.append("hung_other_piece")
    still = [s for s in threatened_after if s in threatened_before and s != mv.from_square]
    if still and gain < max(threatened_before[s] for s in still):
        tags.append("ignored_threat")
    if free and mv not in free and best in free:
        tags.append("missed_free_capture")
    if reply_pv and reply_pv[0] in after.legal_moves:
        a2 = after.copy()
        a2.push(reply_pv[0])
        if P.is_fork(a2, reply_pv[0]):
            tags.append("walked_into_fork")
    if best is not None and best != mv and best in board.legal_moves:
        b2 = board.copy()
        b2.push(best)
        if P.is_fork(b2, best) and not b2.is_checkmate():
            tags.append("missed_fork")
    return tags


def reply_cause(fen: str, att: dict) -> str:
    """Why a model reply was unusable, in categories the proposer can act on. Parses UCI or SAN like the player."""
    if att.get("kind") == "format":
        return "no move found in the reply"
    text = (att.get("move") or att.get("text") or att.get("raw") or "").strip()
    b = chess.Board(fen)
    m = None
    try:
        m = chess.Move.from_uci(text)
    except ValueError:
        try:
            b.parse_san(text)
            return "legal move rejected (parser mismatch)"
        except chess.AmbiguousMoveError:
            return "ambiguous SAN (two pieces can go there)"
        except chess.IllegalMoveError:
            return "SAN names a move that is not legal here"
        except ValueError:
            return "malformed move"
    p = b.piece_at(m.from_square)
    if p is None or p.color != b.turn:
        return "moved a piece that is not there (board misread)"
    if b.is_pseudo_legal(m):
        return "ignored check or pin"
    return "piece cannot move there (blocked line / wrong geometry)"


# ------------------------------------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------------------------------------


def agent_color(rec: dict) -> bool:
    c = rec.get("color") or rec.get("agent_plays")
    return chess.WHITE if c == "white" else chess.BLACK


def evaluate_game(rec: dict, engine: Engine) -> dict:
    """Per-agent-move rows for one complete game, split-agnostic (rsi.bank uses this on held games).

    One search per position, Lichess style: the played move's value is the next position's eval from the mover's side.
    """
    mine = agent_color(rec)
    moves = [chess.Move.from_uci(u) for u in rec["moves"]]
    opening = int(rec.get("opening_plies") or 0)
    said = {m.get("ply"): m for m in rec.get("per_move") or []}
    gid = rec.get("game") or rec.get("slot") or rec.get("id")
    board, infos = chess.Board(rec.get("start_fen") or chess.STARTING_FEN), []
    start = board.copy()
    for i in range(len(moves) + 1):
        infos.append(engine.analyse(board, game=gid))
        if i < len(moves):
            board.push(moves[i])
    board, hist, out, max_win = start, [], [], 0.0
    for i, mv in enumerate(moves):
        mover = board.turn
        if mover == mine and i >= opening:
            before, after = cp_of(infos[i]["score"], mover), cp_of(infos[i + 1]["score"], mover)
            w0, w1 = win_pct(before), win_pct(after)
            loss = max(0.0, w0 - w1)
            best = (infos[i].get("pv") or [None])[0]
            reply_pv = infos[i + 1].get("pv") or []
            pm = said.get(i) or {}
            out.append({
                "ply": i, "move_no": board.fullmove_number, "fen": board.fen(), "history": list(hist),
                "played": board.san(mv), "uci": mv.uci(), "best": board.san(best) if best else None,
                "best_uci": best.uci() if best else None, "cp_before": before, "cp_after": after,
                "win_before": round(w0, 1), "win_after": round(w1, 1), "loss": round(loss, 1), "class": classify(loss),
                "accuracy": round(accuracy(loss), 1), "phase": phase(board, i),
                "motifs": tag(board, mv, best, reply_pv, mine) if loss >= 5 else [],
                "punish": _san_line(board, mv, reply_pv[:3]),
                "reason": (pm.get("reason") or pm.get("said") or "")[-200:],
                "rejected": [reply_cause(board.fen(), a) for a in pm.get("attempts") or []
                             if a.get("kind") in ("illegal", "format")],
            })
            max_win = max(max_win, w1)
        hist.append(board.san(mv))
        board.push(mv)
    final_cp = cp_of(infos[-1]["score"], mine)
    return {"game": gid, "split": rec.get("split"), "family": rec.get("family"), "color": "white" if mine else "black",
            "opp_elo": rec.get("opp_elo"), "result": rec.get("result"), "score": rec.get("score"), "moves": out,
            "final_cp": final_cp, "max_win": round(max_win, 1),
            "converted": not (max_win >= 90 and win_pct(final_cp) < 70 and not board.is_checkmate())}


def _san_line(board: chess.Board, mv: chess.Move, pv: list[chess.Move]) -> str:
    b = board.copy()
    out = [b.san(mv)]
    b.push(mv)
    for m in pv:
        if m not in b.legal_moves:
            break
        out.append(b.san(m))
        b.push(m)
    return " ".join(out)


def _check_train(recs) -> None:
    held = [r.get("game") or r.get("slot") for r in recs if r.get("split") != "train"]
    if held:
        raise HeldGameError(f"mine/digest take train-family games only; got non-train games {held[:5]}")


def mine_games(records: list[dict], engine: Engine | None = None, nodes: int = NODES) -> list[dict]:
    """Analyse the complete TRAIN games among `records`. Raises HeldGameError on any held game; skips infra games."""
    _check_train(records)
    done = [r for r in records if r.get("status", "complete") == "complete" and r.get("moves")]
    own = engine is None
    engine = engine or Engine(nodes)
    try:
        return [evaluate_game(r, engine) for r in done]
    finally:
        if own:
            engine.close()


# ------------------------------------------------------------------------------------------------
# Stats and digest
# ------------------------------------------------------------------------------------------------


def summarize(games: list[dict]) -> dict:
    """Numbers the digest, the lesson ledger and the run log share. motifs[t].per100 is the trend metric."""
    moves = [m for g in games for m in g["moves"]]
    n = len(moves) or 1
    classes = collections.Counter(m["class"] for m in moves if m["class"])
    motif_n, motif_loss = collections.Counter(), collections.Counter()
    for m in moves:
        for t in m["motifs"] or (["positional"] if m["class"] else []):
            motif_n[t] += 1
            motif_loss[t] += m["loss"]
    total = sum(m["loss"] for m in moves) or 1.0
    rejected = collections.Counter(c for m in moves for c in m["rejected"])
    return {
        "games": len(games), "agent_moves": len(moves), "mean_loss": round(sum(m["loss"] for m in moves) / n, 2),
        "accuracy": round(sum(m["accuracy"] for m in moves) / n, 1),
        "blunders": classes["blunder"], "mistakes": classes["mistake"], "inaccuracies": classes["inaccuracy"],
        "blunders_per_game": round(classes["blunder"] / max(1, len(games)), 2),
        "motifs": {t: {"n": motif_n[t], "per100": round(100 * motif_n[t] / n, 1),
                       "loss_share": round(motif_loss[t] / total, 3)} for t in motif_n},
        "rejected": dict(rejected), "rejected_per100": round(100 * sum(rejected.values()) / n, 1),
        "phases": dict(collections.Counter(m["phase"] for m in moves if m["class"] in ("mistake", "blunder"))),
        "not_converted": sum(not g["converted"] for g in games),
    }


def ledger(harness: dict, stats: dict) -> list[dict]:
    """Each current lesson's target motif rate when it was added vs now (per 100 agent moves)."""
    rows = []
    for l in harness.get("lessons") or []:
        now = (stats["motifs"].get(l.get("motif")) or {}).get("per100", 0.0)
        rows.append({"id": l.get("id"), "text": l.get("text", ""), "motif": l.get("motif"),
                     "gen_added": l.get("gen_added"), "rate_at_add": l.get("rate_at_add"), "rate_now": now})
    return rows


def _fmt(x) -> str:
    return "-" if x is None else f"{x:g}" if isinstance(x, (int, float)) else str(x)


def digest(games: list[dict], harness: dict | None = None, history: list[dict] | None = None,
           pruned: list[dict] | None = None, knobs: dict | None = None, title: str = "",
           max_tokens: int = DIGEST_TOKENS) -> tuple[str, dict]:
    """Proposer-facing markdown (<= max_tokens) and the stats dict.

    history: earlier generations' stats, newest first, e.g. [{"gen": 3, "motifs": {...}}, ...] (only 2 are shown).
    pruned:  [{"change": "...", "exam": {"n", "gain", "p"}}]   knobs: {"think 0": "5 s, $0.003/call", ...}
    """
    _check_train(games)
    stats = summarize(games)
    hist = (history or [])[:2]
    head = [f"# Mistake digest{(' - ' + title) if title else ''}", "",
            f"{stats['games']} practice games, {stats['agent_moves']} of your moves analysed (book moves excluded).",
            f"Win chance lost per move: {stats['mean_loss']}% | accuracy {stats['accuracy']}% | "
            f"{stats['blunders']} blunders, {stats['mistakes']} mistakes, {stats['inaccuracies']} inaccuracies "
            f"({stats['blunders_per_game']} blunders/game)."]
    if stats["not_converted"]:
        head.append(f"Winning positions (>=90% win chance) not converted: {stats['not_converted']} games.")
    if stats["phases"]:
        head.append("Mistakes+blunders by phase: " + ", ".join(f"{p} {c}" for p, c in
                                                                sorted(stats["phases"].items(), key=lambda x: -x[1])))
    # Motif table with trend
    cols = " | ".join(f"gen {h.get('gen', '?')}" for h in hist)
    table = ["", "## Where the win chance went (per 100 of your moves; trend vs earlier generations)", "",
             f"| motif | n | per100 now | {cols + ' | ' if cols else ''}share of loss | visible |",
             "|---|---|---|" + "---|" * len(hist) + "---|---|"]
    order = sorted(stats["motifs"], key=lambda t: -stats["motifs"][t]["loss_share"])
    for t in order:
        s = stats["motifs"][t]
        prev = " | ".join(_fmt(((h.get("motifs") or {}).get(t) or {}).get("per100", 0.0)) for h in hist)
        when, tool = VISIBILITY.get(t, ("-", None))
        vis = {"pre": "before the move" + (f" ({tool} tool)" if tool else ""), "post": "only after the move"}.get(when, "-")
        table.append(f"| {MOTIF_TEXT.get(t, t)} [{t}] | {s['n']} | {s['per100']} | {prev + ' | ' if prev else ''}"
                     f"{s['loss_share']:.0%} | {vis} |")
    # Rejected replies
    rej = []
    if stats["rejected"]:
        rej = ["", f"## Unusable replies: {sum(stats['rejected'].values())} ({stats['rejected_per100']} per 100 moves)",
               ", ".join(f"{c} x{k}" for k, c in sorted(stats["rejected"].items(), key=lambda x: -x[1]))]
    # Lesson ledger, pruned list, knob costs
    led = ledger(harness or {}, stats)
    tail = ["", "## Lesson ledger (target motif per 100 moves: when added -> now)"]
    tail += [f"- {r['id']} [{r['motif']}] gen {_fmt(r['gen_added'])}: {_fmt(r['rate_at_add'])} -> {_fmt(r['rate_now'])}"
             f"  \"{r['text']}\"" for r in led] or ["(no lessons yet)"]
    tail += ["", "## Already tried and pruned (do not repeat)"]
    tail += [f"- {p.get('change')}  (exam n={_fmt((p.get('exam') or {}).get('n'))}, "
             f"gain {_fmt((p.get('exam') or {}).get('gain'))}, P(better) {_fmt((p.get('exam') or {}).get('p'))})"
             for p in pruned or []] or ["(none)"]
    if knobs:
        tail += ["", "## Knob costs (measured)"] + [f"- {k}: {v}" for k, v in knobs.items()]
    # Examples: top 3 motifs by loss share, <= 2 each, at most one per game, costliest first
    top = [t for t in order if t != "positional"][:3] or order[:1]
    ex_blocks, shown = [], set()
    for t in top:
        cands = sorted(((g, m) for g in games for m in g["moves"] if m["class"] and t in (m["motifs"] or ["positional"])),
                       key=lambda x: -x[1]["loss"])
        used = set()
        for g, m in cands:
            if g["game"] in used or len(used) >= 2 or (g["game"], m["ply"]) in shown:
                continue
            used.add(g["game"])
            shown.add((g["game"], m["ply"]))
            side = "White" if g["color"] == "white" else "Black"
            b = [f"- [{t}] move {m['move_no']} as {side}, FEN `{m['fen']}`",
                 f"  played {m['played']}, engine preferred {m['best']}; win {m['win_before']}% -> {m['win_after']}% "
                 f"({m['class']}); then: {m['punish']}"]
            if m.get("reason"):
                b.append(f"  player's reason: {m['reason'][-120:]!r}")
            ex_blocks.append("\n".join(b))
    budget = int(max_tokens * CHARS_PER_TOKEN)
    while True:
        ex = (["", "## Worst moves of the top motifs"] + ex_blocks) if ex_blocks else []
        text = "\n".join(head + table + rej + ex + tail) + "\n"
        if len(text) <= budget or not ex_blocks:
            break
        ex_blocks.pop()
    if len(text) > budget:
        text = text[: budget - 20] + "\n...(truncated)\n"
    stats["digest_tokens_est"] = round(len(text) / CHARS_PER_TOKEN)
    return text, stats


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------


def load(paths: list[str]) -> list[dict]:
    files = []
    for p in map(Path, paths):
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return [json.loads(f.read_text()) for f in files]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--nodes", type=int, default=NODES)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    games = mine_games(load(a.paths), nodes=a.nodes)
    text, stats = digest(games)
    print(text)
    if a.out:
        Path(a.out).write_text(text)
    if a.json:
        Path(a.json).write_text(json.dumps({"stats": stats, "games": games}, indent=1))
    print(f"(digest ~{stats['digest_tokens_est']} tokens)", file=sys.stderr)


if __name__ == "__main__":
    main()
