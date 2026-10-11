"""Build the chess set: one folder per game against Stockfish.

  python tasks/chess/build.py

Games differ by opening and by which colour the agent plays.
"""

import json
import shutil
from pathlib import Path

HERE = Path(__file__).parent
OPENINGS = {
    "italian": "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5",
    "sicilian": "e2e4 c7c5 g1f3 d7d6 d2d4 c5d4",
    "french": "e2e4 e7e6 d2d4 d7d5 b1c3 g8f6",
    "caro-kann": "e2e4 c7c6 d2d4 d7d5 b1c3 d5e4",
    "queens-gambit": "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6",
    "kings-indian": "d2d4 g8f6 c2c4 g7g6 b1c3 f8g7",
    "english": "c2c4 e7e5 b1c3 g8f6 g1f3 b8c6",
    "london": "d2d4 d7d5 c1f4 g8f6 e2e3 c7c5",
    "ruy-lopez": "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6",
    "scandinavian": "e2e4 d7d5 e4d5 d8d5 b1c3 d5a5",
}
for old in HERE.glob("game-*"):
    shutil.rmtree(old)
for name, line in OPENINGS.items():
    for color in ("white", "black"):
        d = HERE / f"game-{name}-{color}"
        (d / ".docs").mkdir(parents=True)
        (d / "game.json").write_text(json.dumps(
            {"color": color, "opening": line.split(), "skill": 0, "moves": 18, "margin": 3}, indent=1))  # fmt: skip
        (d / ".docs/instructions.md").write_text(
            f"# {name}, playing {color}\n\nPlay {color} against Stockfish (skill level 0) from the {name} opening.\n"
            "The game runs to move 18. You pass if you win, draw, or reach move 18 without an illegal-move forfeit "
            "and no more than 3 points of material behind.\n"
        )
print(len(OPENINGS) * 2, "games")
