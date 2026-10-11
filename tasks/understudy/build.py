"""Builds the Understudy problems: batches of the text MuJoCo and the 21 gates printed for completed runs.

Understudy (github.com/PranavAchar01/understudy-replay) turns one sentence into robot training data: Runway films
people doing the task, MediaPipe tracks the hand, 21 physics gates and a MuJoCo replay decide what counts, and
SmolVLA (a VLA) trains on the rest. The last stage is text. This set asks an agent to name each line with one
failure term, so the data step knows what to fix. Bonsai then grows that agent's rules (RSI).

  python tasks/understudy/build.py ../understudy-replay

Each problem folder gets lines.json (what the agent sees) and expected.json (hidden: the gold term per line, taken
from the structured fields behind the text, never from the text).
"""

import json
import shutil
import sys
from pathlib import Path

here = Path(__file__).parent
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo / "src"))
from understudy import rsi  # noqa: E402

for d in here.iterdir():
    if d.is_dir() and not d.name.startswith("_"):
        shutil.rmtree(d)
lines = rsi.corpus(repo)
batches = {}
for x in lines:
    run = x["run"].split(":")[0].replace("runway clips", "gates").replace(" ", "-").replace("(", "").replace(")", "")
    batches.setdefault(run, []).append(x)
n = 0
for run, xs in batches.items():
    for i in range(0, len(xs), 25):
        chunk = xs[i : i + 25]
        name = f"{run}-{i // 25 + 1}".lower()
        d = here / name
        (d / ".docs").mkdir(parents=True)
        (d / ".docs" / "instructions.md").write_text(
            f"Label each line in lines.json with one failure term. Run: {chunk[0]['run']}.\n"
        )
        (d / "lines.json").write_text(json.dumps([{"id": k, "text": x["text"]} for k, x in enumerate(chunk)], indent=1))
        (d / "expected.json").write_text(json.dumps([x["gold"] for x in chunk]))
        n += 1
(here / "_shared").mkdir(exist_ok=True)
(here / "_shared" / "terms.json").write_text(json.dumps(rsi.TERMS, indent=1))
print(f"{n} problems, {len(lines)} lines")
