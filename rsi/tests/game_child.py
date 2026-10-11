"""Subprocess helper for test_game: plays one bot-as-agent game, optionally dying at a ply.

  python -m rsi.tests.game_child <out_dir> <replays_dir> [kill_at_ply] [sig]

sig "KILL": the agent SIGKILLs its own process when asked to move at kill_at_ply (only the first time: a marker
file in out_dir). sig "HANG": the agent sleeps there, so the test can send SIGTERM mid-move.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

from rsi.game import play_game
from rsi.ladder import Bot

HARNESS = {"id": "gT", "model": "bot", "rules": "", "tools": [], "think": 0}
SLOT = {"id": "s01", "seed": "t|0|s01", "family": "italian", "color": "white", "split": "train", "target": 100,
        "opp": {"name": "E100", "elo": 93, "spec": {"kind": "policy", "temp": 8, "eps": 0.556, "depth": 1}}}
AGENT = {"kind": "policy", "temp": 2, "depth": 1}
MAX_MOVES = 30


def main():
    out, replays = Path(sys.argv[1]), Path(sys.argv[2])
    kill_at = int(sys.argv[3]) if len(sys.argv) > 3 else -1
    sig = sys.argv[4] if len(sys.argv) > 4 else "KILL"
    marker = out / "killed.marker"
    bot = Bot(AGENT, seed="agent")

    def choose(board, harness, ctx):
        if len(board.move_stack) == kill_at and not marker.exists():
            marker.write_text("1")
            if sig == "KILL":
                os.kill(os.getpid(), signal.SIGKILL)
            print("HANGING", flush=True)
            time.sleep(120)
        return {"move": bot.move(board), "reason": "bot", "attempts": [{"kind": "ok"}], "calls": 1, "cost": 0.0}

    try:
        rec = play_game(HARNESS, SLOT, out, "TEST", choose=choose, max_moves=MAX_MOVES, replays_dir=replays)
        print(json.dumps({"status": rec["status"], "how": rec["how"]}))
    finally:
        bot.close()


if __name__ == "__main__":
    main()
