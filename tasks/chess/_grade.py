"""Grade one chess answer: python _grade.py answer.txt position.fen. Exit 0 only for a legal move that checkmates."""

import sys
from pathlib import Path

import chess

answer, fen = Path(sys.argv[1]), Path(sys.argv[2])
if not answer.exists():
    sys.exit("no answer.txt")
text = answer.read_text().split()
if len(text) != 1:
    sys.exit("answer.txt must hold exactly one move")
board = chess.Board(fen.read_text().strip())
try:
    move = chess.Move.from_uci(text[0].lower())
except ValueError:
    try:
        move = board.parse_san(text[0])
    except ValueError:
        sys.exit("not a move")
if move not in board.legal_moves:
    sys.exit("illegal move")
board.push(move)
if not board.is_checkmate():
    sys.exit("legal, but not checkmate")
print("checkmate")
