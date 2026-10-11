"""Check every tree's scores against Raindrop's own record of the runs.

  uv run --no-project --with raindrop-query --with python-dotenv python raindrop_audit.py legal health finance

For each version in runs/<set>-versions.jsonl, this pulls that version's held-out attempts from
Raindrop (event <set>_task, property harness_version), keeps the latest attempt per problem made
before the version was recorded, and recounts the pass rate. It also lists the signals Raindrop's
own detectors attached to those attempts, beside our task_outcome. Writes runs/raindrop-audit.json.
Needs RAINDROP_QUERY_KEY in .env.
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from raindrop_query import RaindropQuery

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
rq = RaindropQuery(api_key=os.environ["RAINDROP_QUERY_KEY"])


def events(name: str, since: datetime) -> list:
    out, cursor = [], None
    while True:
        page = rq.events.list(event_name=name, limit=100, cursor=cursor, timestamp={"gte": since})
        out += page.data
        cursor = getattr(page.meta, "cursor", None)
        if not cursor or not getattr(page.meta, "has_more", False):
            return out


def audit(key: str) -> list[dict]:
    versions = [json.loads(line) for line in (ROOT / f"runs/{key}-versions.jsonl").read_text().splitlines() if line.strip()]
    tasks = sorted(p.name for p in (ROOT / "tasks" / key).glob("q*"))
    heldout = {f"{key}/{t}" for i, t in enumerate(tasks) if i % 2 == 1}
    first = datetime.fromtimestamp(min(v["ts"] for v in versions), timezone.utc) - timedelta(hours=1)
    evs = events(f"{key}_task", first)
    rows = []
    for v in versions:
        done = datetime.fromtimestamp(v["ts"], timezone.utc) + timedelta(seconds=5)
        latest = {}
        for e in evs:
            p = e.properties or {}
            if p.get("harness_version") == v["id"] and p.get("task") in heldout and e.timestamp <= done:
                if p["task"] not in latest or e.timestamp > latest[p["task"]].timestamp:
                    latest[p["task"]] = e
        found = list(latest.values())
        passed = sum(1 for e in found if str((e.properties or {}).get("passed")).lower() == "true")
        detected = Counter(s.name for e in found for s in (e.signals or []) if s.name != "task_outcome")
        rows.append({
            "set": key, "id": v["id"], "kept": v["kept"], "our_score": v["score"],
            "raindrop_attempts": len(found), "raindrop_passed": passed,
            "raindrop_score": passed / len(found) if found else None,
            "match": bool(found) and abs(passed / len(found) - v["score"]) < 1e-9 and len(found) == len(heldout),
            "raindrop_detected": dict(detected),
        })  # fmt: skip
    return rows


if __name__ == "__main__":
    report = [r for key in sys.argv[1:] for r in audit(key)]
    (ROOT / "runs" / "raindrop-audit.json").write_text(json.dumps(report, indent=1))
    for r in report:
        rs = f"{r['raindrop_score']:.0%}" if r["raindrop_score"] is not None else "-"
        print(f"{r['set']:8} {r['id']:4} {'kept  ' if r['kept'] else 'pruned'} ours {r['our_score']:.0%}  raindrop {rs} "
              f"({r['raindrop_passed']}/{r['raindrop_attempts']})  {'MATCH' if r['match'] else 'DIFF '}  {r['raindrop_detected'] or ''}")  # fmt: skip
