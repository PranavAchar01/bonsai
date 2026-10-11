"""The chess agent: plays one game against Stockfish, one model call per move.

Bonsai runs this in the game's working folder. The harness's rules are the system prompt for
every move, so rewriting the rules changes how it plays. The whole game goes to game-record.json,
which the grader replays move by move.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import chess
import chess.engine

harness = json.loads(Path(os.environ["BONSAI_HARNESS"]).read_text())
cfg = json.loads((Path(os.environ["BONSAI_TASK"]) / "game.json").read_text())
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
MOVE = re.compile(r"\b([a-h][1-8][a-h][1-8][qrbn]?)\b")
mine = chess.WHITE if cfg["color"] == "white" else chess.BLACK


def ask(board: chess.Board, history: list[str], rejected: list[str]) -> str:
    prompt = (
        f"You are playing {cfg['color']} against a chess engine. It is your move.\n\n"
        f"Moves so far: {' '.join(history) or '(none)'}\nFEN: {board.fen()}\n\n{board}\n\n"
        "Uppercase is White, lowercase is Black, rank 8 is the top row, file a is the left column.\n"
        + (f"These moves were rejected as illegal, do not repeat them: {', '.join(rejected)}\n" if rejected else "")
        + "End your reply with your move in UCI form on its own line, for example e2e4 or e7e8q."
    )
    out = subprocess.run(
        [CLAUDE, "-p", prompt, "--model", harness["model"], "--output-format", "json", "--system-prompt", harness["rules"],
         "--max-turns", "1", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],  # fmt: skip
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    try:
        reply = json.loads(out.stdout).get("result") or ""
    except ValueError:
        reply = ""
    found = MOVE.findall(reply.lower())
    return found[-1] if found else reply.strip()[-12:]


board, history, end = chess.Board(), [], None
for uci in cfg["opening"]:
    history.append(board.san(chess.Move.from_uci(uci)))
    board.push_uci(uci)
start = len(board.move_stack)
engine = chess.engine.SimpleEngine.popen_uci(shutil.which("stockfish") or "/opt/homebrew/bin/stockfish")
engine.configure({"Skill Level": cfg["skill"]})
try:
    while not board.is_game_over() and board.fullmove_number <= cfg["moves"]:
        if board.turn == mine:
            rejected = []
            while len(rejected) < 3:
                text = ask(board, history, rejected)
                try:
                    move = chess.Move.from_uci(text)
                except ValueError:
                    move = None
                if move in board.legal_moves:
                    break
                rejected.append(text or "(no move)")
            else:
                end = f"forfeit at move {board.fullmove_number}: three illegal moves ({', '.join(rejected)})"
                break
        else:
            move = engine.play(board, chess.engine.Limit(time=0.05)).move
        history.append(board.san(move))
        board.push(move)
finally:
    engine.quit()
record = {"game": Path(os.environ["BONSAI_TASK"]).name, "harness": harness.get("id", "seed"), "agent_plays": cfg["color"],
          "moves": [m.uci() for m in board.move_stack], "san": history, "final_fen": board.fen(), "forfeit": end}  # fmt: skip
Path("game-record.json").write_text(json.dumps(record))
# A copy for replays and renders: tasks/chess/_replays/<harness version>/<game>.json
keep = Path(__file__).parent / "_replays" / record["harness"]
keep.mkdir(parents=True, exist_ok=True)
(keep / f"{record['game']}.json").write_text(json.dumps(record, indent=1))
print(end or f"played to move {board.fullmove_number}: {' '.join(history[start:])[-260:]}")
