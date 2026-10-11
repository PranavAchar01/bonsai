"""Cheap, engine-free chess perception built on python-chess.

Two consumers:
  * mine.py   -- tags the agent's mistakes with motifs (uses material, see, threat_on, en_prise, mates_in_one, is_fork)
  * player.py -- the optional "sight tools" a harness switches on, rendered as text under the board

Sight tools carry an assist tier so every reported Elo says how much help the player had:
  1 legality / board reading (legal_moves, pieces)
  2 static perception, no lookahead past the exchange on one square (threats, hanging, checks, pins)
Tier 3 (mate-in-one, "this move hangs") is deliberately not offered here.
"""

from __future__ import annotations

import chess

VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
        chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
ORDER = [chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN]


def sq(s: int) -> str:
    return chess.square_name(s)


def san(board: chess.Board, move: chess.Move) -> str:
    """SAN with mate shown as plain check: '#' would leak a tier-3 mate-in-one signal."""
    return board.san(move).replace("#", "+")


def side(color: bool) -> str:
    return "White" if color == chess.WHITE else "Black"


# ---------------------------------------------------------------------------------------------
# Helpers (also used by mine.py)
# ---------------------------------------------------------------------------------------------


def material(board: chess.Board, color: bool) -> int:
    return sum(VAL[p] * len(board.pieces(p, color)) for p in VAL if p != chess.KING)


def _as_mover(board: chess.Board, color: bool) -> chess.Board | None:
    """Copy of board with `color` to move (null move if needed); None if that would be illegal (side in check)."""
    b = board.copy(stack=False)
    if b.turn != color:
        if b.is_check():
            return None
        b.push(chess.Move.null())
        if b.was_into_check():
            return None
    return b


def _lva_capture(board: chess.Board, square: int) -> chess.Move | None:
    """Least valuable legal capture onto square for the side to move."""
    best = None
    for m in board.generate_legal_captures(to_mask=chess.BB_SQUARES[square]):
        if board.is_en_passant(m):
            continue
        v = VAL[board.piece_type_at(m.from_square)]
        if best is None or v < best[0]:
            best = (v, m)
    return best[1] if best else None


def _see_square(board: chess.Board, square: int) -> int:
    """Gain for the side to move from continuing an exchange on square (it may stop, so >= 0)."""
    m = _lva_capture(board, square)
    if m is None:
        return 0
    victim = board.piece_type_at(square)
    b = board.copy(stack=False)
    b.push(m)
    return max(0, VAL[victim] - _see_square(b, square))


def see(board: chess.Board, move: chess.Move) -> int:
    """Static exchange evaluation of a capture: material the mover nets after the full exchange."""
    if not board.is_capture(move):
        return 0
    victim = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)
    b = board.copy(stack=False)
    b.push(move)
    return VAL[victim] - _see_square(b, move.to_square)


def threat_on(board: chess.Board, square: int) -> int:
    """Material the owner of the piece on square loses if the other side could exchange there now (0 = safe)."""
    piece = board.piece_at(square)
    if piece is None or piece.piece_type == chess.KING:
        return 0
    b = _as_mover(board, not piece.color)
    if b is None:
        return 0
    return max((see(b, m) for m in b.generate_legal_captures(to_mask=chess.BB_SQUARES[square])), default=0)


def en_prise(board: chess.Board, color: bool, min_loss: int = 1) -> list[tuple[int, int]]:
    """[(square, loss)] for pieces of `color` the opponent could win material on right now, biggest first."""
    out = []
    for s, p in board.piece_map().items():
        if p.color == color and p.piece_type != chess.KING:
            loss = threat_on(board, s)
            if loss >= min_loss:
                out.append((s, loss))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def winning_captures(board: chess.Board, color: bool) -> list[tuple[chess.Move, int]]:
    """[(move, gain)] legal captures by `color` that net material by SEE, best first (needs `color` to move)."""
    if board.turn != color:
        return []
    caps = [(m, see(board, m)) for m in board.generate_legal_captures()]
    return sorted([c for c in caps if c[1] > 0], key=lambda x: (-x[1], x[0].uci()))


def mates_in_one(board: chess.Board) -> list[chess.Move]:
    """Mating moves for the side to move. Miner-only: never exposed as a sight tool."""
    out = []
    for m in board.legal_moves:
        board.push(m)
        if board.is_checkmate():
            out.append(m)
        board.pop()
    return out


def fork_targets(board: chess.Board, square: int) -> list[int]:
    """Enemy pieces the piece on square hits profitably (king, worth more, or undefended)."""
    piece = board.piece_at(square)
    if piece is None:
        return []
    hits = []
    for t in board.attacks(square):
        q = board.piece_at(t)
        if q is None or q.color == piece.color:
            continue
        if q.piece_type == chess.KING or VAL[q.piece_type] > VAL[piece.piece_type] or not board.is_attacked_by(q.color, t):
            hits.append(t)
    return hits


def is_fork(board_after: chess.Board, move: chess.Move) -> bool:
    """After `move` was pushed: does the moved piece hit two or more worthwhile targets?"""
    return len(fork_targets(board_after, move.to_square)) >= 2


def pins(board: chess.Board, color: bool) -> list[tuple[int, int]]:
    """[(pinned square, pinner square)] for pieces of `color` absolutely pinned to their king."""
    out = []
    king = board.king(color)
    if king is None:
        return out
    for s, p in board.piece_map().items():
        if p.color == color and p.piece_type != chess.KING and board.is_pinned(color, s):
            ray = board.pin(color, s)
            pinner = next((t for t in chess.SquareSet(ray) if (q := board.piece_at(t)) and q.color != color), None)
            out.append((s, pinner))
    return sorted(out)


# ---------------------------------------------------------------------------------------------
# Sight tools: fn(board, color) -> one text block; `color` is the player's side.
# ---------------------------------------------------------------------------------------------


def _legal_moves(board: chess.Board, color: bool) -> str:
    if board.turn != color:
        return "Legal moves: (not your turn)"
    moves = sorted(board.legal_moves, key=lambda m: san(board, m))
    return "Legal moves: " + " ".join(f"{san(board, m)}({m.uci()})" for m in moves)


def _pieces(board: chess.Board, color: bool) -> str:
    lines = []
    for who, c in (("Your", color), ("Opponent", not color)):
        parts = []
        for pt in ORDER:
            squares = [sq(s) for s in board.pieces(pt, c)]
            if squares:
                parts.append(f"{NAME[pt]} {','.join(squares)}")
        lines.append(f"{who} pieces ({side(c)}): " + "; ".join(parts))
    return "\n".join(lines)


def _threats(board: chess.Board, color: bool) -> str:
    th = en_prise(board, color)
    if not th:
        return "Threats against you: none of your pieces can be won right now."
    return "Threats against you: " + ", ".join(
        f"your {NAME[board.piece_type_at(s)]} on {sq(s)} can be won (you lose {loss})" for s, loss in th)


def _hanging(board: chess.Board, color: bool) -> str:
    b = _as_mover(board, color)
    caps = winning_captures(b, color) if b is not None else []
    if not caps:
        return "Opponent pieces you can win: none."
    return "Opponent pieces you can win: " + ", ".join(
        f"{san(b, m)} takes the {NAME[b.piece_type_at(m.to_square) or chess.PAWN]} on {sq(m.to_square)} (+{g})"
        for m, g in caps)


def _checks(board: chess.Board, color: bool) -> str:
    lines = []
    if board.is_check() and board.turn == color:
        attackers = [sq(s) for s in board.checkers()]
        lines.append(f"You are in check from {', '.join(attackers)}.")
    if board.turn == color:
        give = sorted(san(board, m) for m in board.legal_moves if board.gives_check(m))
        lines.append("Checks you can give: " + (" ".join(give) if give else "none"))
    else:
        lines.append("Checks you can give: (not your turn)")
    return "\n".join(lines)


def _pins(board: chess.Board, color: bool) -> str:
    def fmt(lst, c):
        return ", ".join(f"{NAME[board.piece_type_at(s)]} {sq(s)} (by {sq(p) if p is not None else '?'})"
                         for s, p in lst) or "none"
    return (f"Your pieces pinned to your king: {fmt(pins(board, color), color)}. "
            f"Opponent pieces pinned to their king: {fmt(pins(board, not color), not color)}.")


TOOLS = {
    "legal_moves": {"tier": 1, "describe": "Every legal move, in SAN with UCI in brackets.", "fn": _legal_moves},
    "pieces": {"tier": 1, "describe": "Where every piece stands, yours and the opponent's.", "fn": _pieces},
    "threats": {"tier": 2, "describe": "Your pieces the opponent could win material on right now (static exchange).",
                "fn": _threats},
    "hanging": {"tier": 2, "describe": "Opponent pieces you can capture for a net material gain (static exchange).",
                "fn": _hanging},
    "checks": {"tier": 2, "describe": "Whether you are in check, and every checking move you have.", "fn": _checks},
    "pins": {"tier": 2, "describe": "Pieces (yours and theirs) absolutely pinned to their king.", "fn": _pins},
}


def unknown(tools: list[str]) -> list[str]:
    """Names in `tools` that are not sight tools (the proposer validator rejects these)."""
    return [t for t in tools if t not in TOOLS]


def tier(tools: list[str]) -> int:
    """Assist tier of a tool set: the highest tier switched on, 0 for none."""
    return max((TOOLS[t]["tier"] for t in tools if t in TOOLS), default=0)


def render(board: chess.Board, color: bool, tools: list[str]) -> str:
    """Text blocks for the enabled tools, in the given order. Unknown names raise ValueError."""
    bad = unknown(tools)
    if bad:
        raise ValueError(f"unknown sight tools: {bad}")
    seen, out = set(), []
    for t in tools:
        if t not in seen:
            seen.add(t)
            out.append(TOOLS[t]["fn"](board, color))
    return "\n".join(out)
