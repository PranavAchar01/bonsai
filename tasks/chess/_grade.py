"""Grade one game: python _grade.py game-record.json game.json. Exit 0 if the agent held its own.

Pass: the agent won or drew, or it reached the move limit without a forfeit and without being
more than `margin` points of material behind (pawn 1, knight 3, bishop 3, rook 5, queen 9).
"""

import json
import sys
from pathlib import Path

import chess

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
record, cfg = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
if not record.exists():
    sys.exit("no game was played")
rec = json.loads(record.read_text())
mine, board = (chess.WHITE if cfg["color"] == "white" else chess.BLACK), chess.Board()
if rec["moves"][: len(cfg["opening"])] != cfg["opening"]:
    sys.exit("the game did not start from the set opening")
for uci in rec["moves"]:
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        sys.exit(f"illegal move in the record: {uci}")
    board.push(move)
if rec["forfeit"]:
    sys.exit(rec["forfeit"])
if board.is_checkmate():
    if board.turn == mine:
        sys.exit(f"checkmated at move {board.fullmove_number}")
    print("won by checkmate")
    sys.exit(0)
if board.is_game_over():
    print("drawn")
    sys.exit(0)
if board.fullmove_number <= cfg["moves"]:
    sys.exit("the game stopped early")
count = lambda color: sum(v * len(board.pieces(p, color)) for p, v in VALUE.items())  # noqa: E731
diff = count(mine) - count(not mine)
if diff < -cfg["margin"]:
    sys.exit(f"alive at move {cfg['moves']} but {-diff} points of material behind")
print(f"held to move {cfg['moves']}, material {diff:+d}")
