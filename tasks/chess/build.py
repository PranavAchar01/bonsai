"""Build the chess set: mate-in-one positions found in seeded random games.

  uv run --no-project --with chess python tasks/chess/build.py

Each puzzle folder holds the position and the instructions. There is no answer file:
the grader replays the agent's move and asks the rules of chess whether it is checkmate.
"""

import random
import shutil
from pathlib import Path

import chess

HERE = Path(__file__).parent
COUNT = 24


def find(rng: random.Random) -> chess.Board | None:
    board = chess.Board()
    for _ in range(rng.randint(30, 90)):
        if board.is_game_over():
            return None
        board.push(rng.choice(list(board.legal_moves)))
    for move in board.legal_moves:
        board.push(move)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return board
    return None


def main() -> None:
    rng, seen, boards = random.Random(11), set(), []
    while len(boards) < COUNT:
        b = find(rng)
        if b and b.board_fen() not in seen:
            seen.add(b.board_fen())
            boards.append(b)
    for old in HERE.glob("m1-*"):
        shutil.rmtree(old)
    for i, b in enumerate(boards, 1):
        d = HERE / f"m1-{i:02d}"
        (d / ".docs").mkdir(parents=True)
        (d / "position.fen").write_text(b.fen() + "\n")
        side = "White" if b.turn else "Black"
        (d / ".docs/instructions.md").write_text(
            f"# Mate in one\n\n{side} to move. There is a move that checkmates at once.\n\n"
            f"FEN: `{b.fen()}`\n\n```\n{b}\n```\n\n"
            "Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.\n\n"
            "Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, "
            "or `e7e8q` for a promotion). One move, nothing else.\n"
        )
    print(f"{len(boards)} puzzles")


if __name__ == "__main__":
    main()
