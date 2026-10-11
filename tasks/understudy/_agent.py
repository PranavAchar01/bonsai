"""The Understudy labeler: names every MuJoCo / gate line in lines.json with one failure term, one model call.

The harness's `rules` are the system prompt; that is the part Bonsai rewrites. Tools (set.json "tools"):
  terms     adds what each term means (_shared/terms.json)
  pipeline  adds how Understudy makes these lines (Runway -> MediaPipe -> 21 gates -> MuJoCo -> SmolVLA)
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

harness = json.loads(Path(os.environ["BONSAI_HARNESS"]).read_text())
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
terms = json.loads(Path("terms.json").read_text())
lines = json.loads(Path("lines.json").read_text())
tools = set(harness.get("tools") or [])

PIPELINE = (
    "These lines come from Understudy. Runway films a person doing a robot task, MediaPipe tracks the hand, 21 physics "
    "gates reject clips a robot should not learn from (each rejection prints its reason), accepted clips are retargeted "
    "onto an SO-101 arm in MuJoCo, and a SmolVLA policy trained on them is rolled out in MuJoCo on unseen cube starts "
    "(each rollout prints one log line)."
)
prompt = (
    (PIPELINE + "\n\n" if "pipeline" in tools else "")
    + "Terms: "
    + (json.dumps(terms, indent=1) if "terms" in tools else ", ".join(terms))
    + "\n\nLines:\n"
    + "\n".join(f"{x['id']}: {x['text']}" for x in lines)
    + '\n\nReply with JSON only: {"labels": [one term per line, in order]}'
)
out = subprocess.run(
    [CLAUDE, "-p", prompt, "--model", harness["model"], "--output-format", "json", "--system-prompt", harness["rules"],
     "--max-turns", "1", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],  # fmt: skip
    capture_output=True, text=True, timeout=300,
)  # fmt: skip
text = ""
try:
    text = json.loads(out.stdout).get("result") or ""
    labels = json.loads(text[text.index("{") : text.rindex("}") + 1])["labels"]
except (ValueError, KeyError, TypeError):
    labels = re.findall("|".join(terms), text)
Path("labels.json").write_text(json.dumps(labels))
print(f"labeled {len(labels)} of {len(lines)} lines")
