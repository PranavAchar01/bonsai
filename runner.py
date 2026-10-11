"""Run one harness version against a batch of Polyglot (Python) exercises.

A harness is a JSON document: {"rules": str, "tools": [str], "max_turns": int, "model": str}.
Each exercise is copied to a scratch dir, the agent edits the solution file, then the
exercise's own tests decide pass or fail.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
PRACTICE = ROOT / "bench/python/exercises/practice"
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

BASE_HARNESS = {
    "rules": "You are a coding agent. Read .docs/instructions.md, then implement the solution file. "
    "Never edit the test file.",
    "tools": ["Read", "Edit", "Write"],
    "max_turns": 12,
    "model": "haiku",
}

# Raindrop tracing is on only when RAINDROP_WRITE_KEY is set
RAINDROP = None
if os.getenv("RAINDROP_WRITE_KEY"):
    import raindrop.analytics as RAINDROP

    RAINDROP.init(os.environ["RAINDROP_WRITE_KEY"])


def run_one(name: str, harness: dict, scratch: Path) -> dict:
    src = PRACTICE / name
    work = scratch / name
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work, ignore=shutil.ignore_patterns("example.py"))
    meta = json.loads((work / ".meta/config.json").read_text())["files"]
    shutil.rmtree(work / ".meta")
    prompt = f"Implement {', '.join(meta['solution'])} so that {', '.join(meta['test'])} passes."
    t0 = time.time()
    try:
        agent = subprocess.run(
            [
                CLAUDE,
                "-p",
                prompt,
                "--model",
                harness["model"],
                "--output-format",
                "json",
                "--system-prompt",
                harness["rules"],
                "--allowedTools",
                ",".join(harness["tools"]),
                "--max-turns",
                str(harness["max_turns"]),
                "--permission-mode",
                "acceptEdits",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = (
            json.loads(agent.stdout)
            if agent.stdout.strip().startswith("{")
            else {"result": agent.stdout[-500:]}
        )
    except subprocess.TimeoutExpired:
        out = {"result": "agent timeout"}
    agent_s = time.time() - t0
    try:
        test = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--with",
                "pytest",
                "pytest",
                "-q",
                "-x",
                *meta["test"],
            ],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=120,
        )
        passed, tail = test.returncode == 0, test.stdout[-600:]
    except subprocess.TimeoutExpired:
        passed, tail = False, "test timeout"
    if RAINDROP:
        harness_id = hashlib.sha256(json.dumps(harness, sort_keys=True).encode()).hexdigest()[:8]
        eid = RAINDROP.track_ai(
            user_id="bonsai",
            event="polyglot_task",
            model=harness["model"],
            input=prompt,
            output=out.get("result") or "",
            convo_id=harness_id,
            properties={
                "task": name,
                "harness_id": harness_id,
                "passed": passed,
                "turns": out.get("num_turns") or 0,
                "cost_usd": out.get("total_cost_usd") or 0,
                "test_tail": tail,
            },
        )
        # free plan allows one custom signal: pass/fail lives in sentiment
        RAINDROP.track_signal(
            event_id=eid,
            name="task_outcome",
            signal_type="feedback",
            sentiment="POSITIVE" if passed else "NEGATIVE",
            comment=tail[-300:],
        )
    return {
        "task": name,
        "passed": passed,
        "agent_s": round(agent_s, 1),
        "turns": out.get("num_turns"),
        "cost_usd": out.get("total_cost_usd"),
        "session_id": out.get("session_id"),
        "agent_said": (out.get("result") or "")[-400:],
        "test_tail": tail,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="*")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--harness")
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--out", default="runs/baseline.json")
    a = ap.parse_args()
    harness = json.loads(Path(a.harness).read_text()) if a.harness else BASE_HARNESS
    tasks = a.tasks or sorted(p.name for p in PRACTICE.iterdir())[: a.n]
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(lambda t: run_one(t, harness, Path(a.scratch)), tasks))
    wall = time.time() - t0
    if RAINDROP:
        RAINDROP.flush()
    summary = {
        "harness": harness,
        "n": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "wall_s": round(wall, 1),
        "workers": a.workers,
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "rows": rows,
    }
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    json.dump({k: v for k, v in summary.items() if k != "rows"}, sys.stdout, indent=1)
    for r in rows:
        sys.stdout.write(
            f"\n{'PASS' if r['passed'] else 'FAIL'} {r['task']:28} {r['agent_s']}s turns={r['turns']}"
        )


if __name__ == "__main__":
    main()
