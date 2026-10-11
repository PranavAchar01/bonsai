"""One move from the fixed player model (haiku via `claude -p`), shaped by the harness RSI may change.

Harness fields read here (the self-modification surface; everything else is frozen):
  rules    str            system prompt (<= 1200 chars)
  lessons  [{id, text}]   playbook lines appended to the system prompt (<= 8, each <= 220 chars; plain str ok)
  tools    [str]          perceive.TOOLS shown as text under the board (tier <= 2)
  think    int            MAX_THINKING_TOKENS for the call (0, 1024, 2048); timeout = 60 + think/50 s
  model    str            pinned model id (run.json player.model when missing)

choose(board, harness, ctx) asks for {"move", "reason"} through --json-schema and parses the move as UCI or SAN.
Every reply gets a kind:
  ok       a legal move
  illegal  a move was named but it is not legal here      -> chess: the player is asked again (max 3 replies)
  format   nothing that parses as a move (empty reply, prose, error_max_turns)  -> chess, same as illegal
Infra (timeout, crash, API error, rate or usage limit, auth) is retried inside reliable.ask_claude and never counts
as a chess attempt; if it persists choose() raises InfraAbort and the caller voids the game or exam position.
"""

from __future__ import annotations

import json
import os
import re
import time

import chess

from rsi import perceive, reliable

LESSON_CAP, LESSON_CHARS, RULES_CHARS, REASON_WORDS = 8, 220, 1200, 25
CHESS_ATTEMPTS = 3
THINK_MAX = 4096
SEED_RULES = "You are a chess player. Look at the position and choose a good legal move."

SCHEMA = {
    "type": "object",
    "properties": {
        "move": {"type": "string", "description": "your move in UCI (e2e4, e7e8q) or SAN (Nf3, exd5, O-O)"},
        "reason": {"type": "string", "description": f"why, in at most {REASON_WORDS} words"},
    },
    "required": ["move", "reason"],
    "additionalProperties": False,
}

_UCI = re.compile(r"\b([a-h])([1-8])[\s\-x:]*([a-h])([1-8])(?:=?([qrbn]))?\b", re.I)
_SAN = re.compile(r"\b(O-O-O|O-O|0-0-0|0-0|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=?[QRBN])?|"
                  r"[a-h][1-8](?:=?[QRBN])?)(?=[\s+#!?.,;:)\]\"']|$)")


class InfraAbort(reliable.InfraError):
    """The model could not be reached for this move after all retries. The game (or exam position) is void, not lost.
    Carries .kind, .detail, .reset_at and .retryable from the last InfraError."""


def seed_harness(run_cfg: dict | None = None, think: int | None = None) -> dict:
    """The seed harness g0: set.json's rules, the frozen interface tool, think = run.json's default."""
    cfg = run_cfg or reliable.run_config()
    return {"id": "g0", "parent": None, "model": cfg["player"]["model"], "rules": SEED_RULES, "lessons": [],
            "tools": list(cfg["harness_limits"]["seed_tools"]),
            "think": cfg.get("think_default", 0) if think is None else think}


def lesson_lines(h: dict) -> list[str]:
    out = []
    for l in (h.get("lessons") or [])[:LESSON_CAP]:
        text = l.get("text", "") if isinstance(l, dict) else str(l)
        if text.strip():
            out.append(text.strip()[:LESSON_CHARS])
    return out


def system_prompt(h: dict) -> str:
    lessons = lesson_lines(h)
    rules = (h.get("rules") or SEED_RULES).strip()[:RULES_CHARS]
    return rules + ("\n\nLessons from your past games:\n" + "\n".join(f"- {l}" for l in lessons) if lessons else "")


def user_prompt(board: chess.Board, history: list[str], color: bool, h: dict, rejected: list[str]) -> str:
    side = "white" if color == chess.WHITE else "black"
    tools = perceive.render(board, color, list(h.get("tools") or []))
    return (
        f"You are playing {side} against a chess engine. It is your move.\n\n"
        f"Moves so far: {' '.join(history) or '(none)'}\nFEN: {board.fen()}\n\n{board}\n\n"
        "Uppercase is White, lowercase is Black, rank 8 is the top row, file a is the left column.\n"
        + (tools.rstrip() + "\n" if tools.strip() else "")
        + (f"Rejected replies, do not repeat them: {'; '.join(rejected)}\n" if rejected else "")
        + 'Answer with JSON {"move": "<UCI like e2e4 or SAN like Nf3>", "reason": "<at most '
        + f'{REASON_WORDS} words>"}}.'
    )


def parse_move(board: chess.Board, text: str) -> tuple[chess.Move | None, str]:
    """(legal move or None, kind) for a reply. Accepts UCI (e2e4, e2-e4, E2E4, e7e8q, e7e8=Q) or SAN (Nf3, exd5,
    O-O, 0-0, e8=Q+). A promotion written without a piece is read as a queen. kind is ok / illegal / format."""
    s = (text or "").strip()
    if not s:
        return None, "format"
    named = False
    # A bare token first: the whole reply is the move.
    tok = s.strip(" .!?\"'`").replace("0-0-0", "O-O-O").replace("0-0", "O-O")
    m = _UCI.fullmatch(tok)
    if m:
        mv = _uci(board, m)
        return (mv, "ok") if mv else (None, "illegal")
    if tok[:1] in "nrqk" and len(tok) > 1:  # "nf3": a lowercase piece letter (not b: bxc3 is a pawn)
        tok = tok[0].upper() + tok[1:]
    try:
        return board.parse_san(tok.rstrip("+#!?")), "ok"
    except chess.AmbiguousMoveError:
        named = True
    except (chess.IllegalMoveError, chess.InvalidMoveError, ValueError):
        named = bool(_SAN.fullmatch(tok.rstrip("+#!?")))
    # Otherwise the last move-looking token in the text (models put the decision last).
    found = []
    for m in _UCI.finditer(s):
        found.append((m.start(), _uci(board, m)))
    for m in _SAN.finditer(s.replace("0-0-0", "O-O-O").replace("0-0", "O-O")):
        try:
            found.append((m.start(), board.parse_san(m[1])))
        except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError, ValueError):
            found.append((m.start(), None))
    if found:
        legal = [mv for _, mv in sorted(found, key=lambda x: x[0]) if mv]
        return (legal[-1], "ok") if legal else (None, "illegal")
    return None, "illegal" if named else "format"


def _uci(board: chess.Board, m: re.Match) -> chess.Move | None:
    base = f"{m[1]}{m[2]}{m[3]}{m[4]}".lower()
    cands = [base + (m[5] or "").lower()] if m[5] else [base, base + "q"]
    for c in cands:
        try:
            mv = chess.Move.from_uci(c)
        except ValueError:
            continue
        if mv in board.legal_moves:
            return mv
    return None


def _reply_text(r: dict) -> tuple[str, str]:
    """(move text, reason) from a CLI reply: structured output first, then JSON in the text, then the text."""
    st = r.get("structured")
    if isinstance(st, dict) and (st.get("move") or "").strip():
        return str(st["move"]), str(st.get("reason") or "")
    text = r.get("text") or ""
    j = re.search(r"\{[^{}]*\"move\"[^{}]*\}", text)
    if j:
        try:
            d = json.loads(j[0])
            return str(d.get("move") or ""), str(d.get("reason") or "")
        except ValueError:
            pass
    return text, ""


def _short(reason: str) -> str:
    return " ".join(reason.split()[:REASON_WORDS])


def _color(board: chess.Board, ctx: dict) -> bool:
    c = ctx.get("mine")
    if isinstance(c, bool):
        return c
    if c in ("white", "black"):
        return chess.WHITE if c == "white" else chess.BLACK
    return board.turn


def _history(board: chess.Board, ctx: dict) -> list[str]:
    if ctx.get("history") is not None:
        return list(ctx["history"])
    b, out = board.root(), []
    for mv in board.move_stack:
        out.append(b.san(mv))
        b.push(mv)
    return out


def _cli(cli: str | None) -> str:
    if cli:
        return cli
    if os.environ.get("BONSAI_CLAUDE"):
        return os.environ["BONSAI_CLAUDE"]
    try:
        return reliable.run_config()["cli"]["path"]
    except (OSError, ValueError, KeyError):
        return reliable.CLAUDE


def _model(h: dict) -> str:
    if h.get("model"):
        return h["model"]
    try:
        return reliable.run_config()["player"]["model"]
    except (OSError, ValueError, KeyError):
        return "haiku"


def choose(board: chess.Board, harness: dict, ctx: dict | None = None, *, cli: str | None = None,
           extra_env: dict | None = None, timeout: float | None = None, attempts: int | None = None,
           log=None) -> dict:
    """The move this harness plays here.

    Returns {move: chess.Move|None, uci, reason, attempts: [{kind, move, reason, wall_s, cost, ...}], calls, cost,
    infra: False, think_tokens, latency, model}. move is None after CHESS_ATTEMPTS unusable replies (a forfeit;
    chess, not infra). Raises InfraAbort when the model cannot be reached (after reliable's retries).
    ctx: {history: [san], mine: "white"|"black"|bool, ...}; both default from the board.
    """
    ctx = ctx or {}
    color, history = _color(board, ctx), _history(board, ctx)
    think = max(0, min(int(harness.get("think") or 0), THINK_MAX))
    system, model = system_prompt(harness), _model(harness)
    rejected, atts, cost, t0, resolved = [], [], 0.0, time.time(), None
    for _ in range(CHESS_ATTEMPTS):
        prompt = user_prompt(board, history, color, harness, rejected)
        try:
            r = reliable.ask_claude(prompt, system, model, think_tokens=think, schema=SCHEMA,
                                    timeout=timeout or reliable.call_timeout(think), attempts=attempts or 3,
                                    cli=_cli(cli), extra_env=extra_env, log=log)
        except reliable.InfraError as e:
            raise InfraAbort(e.kind, e.detail, reset_at=e.reset_at, retryable=e.retryable) from None
        cost += r.get("cost") or 0.0
        resolved = r.get("model") or resolved
        text, reason = _reply_text(r)
        if r.get("subtype") not in ("success", None):  # error_max_turns etc.: the model's own failure
            mv, kind = None, "format"
        else:
            mv, kind = parse_move(board, text)
        att = {"kind": kind, "move": mv.uci() if mv else (text.strip()[:40] or None), "reason": _short(reason),
               "wall_s": r.get("wall_s"), "cost": r.get("cost"), "in_tokens": r.get("in_tokens"),
               "out_tokens": r.get("out_tokens"), "infra_retries": (r.get("attempts") or 1) - 1,
               "subtype": r.get("subtype")}
        if kind != "ok" and text.strip():
            att["text"] = text.strip()[-200:]
        atts.append(att)
        if kind == "ok":
            return {"move": mv, "uci": mv.uci(), "reason": _short(reason), "attempts": atts, "calls": len(atts),
                    "cost": round(cost, 6), "infra": False, "think_tokens": think,
                    "latency": round(time.time() - t0, 2), "model": resolved}
        shown = (text.strip()[:30] or "(empty reply)") if kind == "format" else text.strip()[:30]
        rejected.append(f"{shown} ({'not a legal move here' if kind == 'illegal' else 'no move found'})")
    return {"move": None, "uci": None, "reason": "", "attempts": atts, "calls": len(atts), "cost": round(cost, 6),
            "infra": False, "think_tokens": think, "latency": round(time.time() - t0, 2), "model": resolved}


def main():
    """python -m rsi.player [FEN ...]: one real move per FEN with the seed harness (prints latency and cost)."""
    import sys
    h = seed_harness()
    for fen in sys.argv[1:] or [chess.STARTING_FEN]:
        b = chess.Board(fen)
        r = choose(b, h, {})
        print(json.dumps({"fen": fen, "move": r["uci"], "reason": r["reason"], "calls": r["calls"],
                          "latency": r["latency"], "cost": r["cost"], "model": r["model"],
                          "kinds": [a["kind"] for a in r["attempts"]]}))


if __name__ == "__main__":
    main()
