"""Pass when at least 90% of the lines carry the gold term. Prints the misses so the proposer can read them."""

import json
import sys
from pathlib import Path

got = json.loads(Path(sys.argv[1]).read_text()) if Path(sys.argv[1]).exists() else []
gold = json.loads(Path(sys.argv[2]).read_text())
lines = json.loads(Path("lines.json").read_text())
ok = sum(g == (got[i] if i < len(got) else None) for i, g in enumerate(gold))
for i, g in enumerate(gold):
    if (got[i] if i < len(got) else None) != g:
        print(f"{lines[i]['text']!r}: said {got[i] if i < len(got) else 'nothing'}, is {g}")
print(f"{ok}/{len(gold)} right")
sys.exit(0 if ok >= 0.9 * len(gold) else 1)
