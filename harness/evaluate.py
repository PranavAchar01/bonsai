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

ROOT = Path(__file__).parent.parent
PRACTICE = ROOT / "bench/python/exercises/practice"
BENCH = ROOT / "bench"
TASKS = ROOT / "tasks"
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

try:  # Raindrop is optional: without the SDK or the key the runner still grades
    import raindrop.analytics as raindrop
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    RAINDROP = bool(os.getenv("RAINDROP_WRITE_KEY"))
    if RAINDROP:
        raindrop.init(os.getenv("RAINDROP_WRITE_KEY"))
except ImportError:
    RAINDROP = False

BASE_HARNESS = {
    "rules": "You are a coding agent. Read .docs/instructions.md, then implement the solution file. "
    "Never edit the test file.",
    "tools": ["Read", "Edit", "Write"],
    "max_turns": 12,
    "model": "haiku",
}


def run_one(name: str, harness: dict, scratch: Path) -> dict:
    # A task is "name" (Python) or "lang/name", for example "cpp/zebra-puzzle".
    lang, _, ex = name.rpartition("/")
    lang = lang or "python"
    work = scratch / lang / ex
    shutil.rmtree(work, ignore_errors=True)
    if lang == "mongo":
        # Our own question set (tasks/mongo): the answer file never enters the agent's folder.
        src = TASKS / "mongo" / ex
        shutil.copytree(TASKS / "mongo" / "_shared", work)
        shutil.copytree(src, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns("expected.json"))
        meta = {"solution": ["pipeline.json"], "test": []}
    else:
        src = BENCH / lang / "exercises/practice" / ex
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("example.py"))
        meta = json.loads((work / ".meta/config.json").read_text())["files"]
        shutil.rmtree(work / ".meta")
    tests_before = {t: hashlib.sha256((work / t).read_bytes()).hexdigest() for t in meta["test"]}
    prompt = f"Implement {', '.join(meta['solution'])} so that {', '.join(meta['test'])} passes."
    if lang == "mongo":
        prompt = "Write pipeline.json so that it answers the question in .docs/instructions.md."
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
    # Cheat guard: the tests that grade are always the originals, and touching them is recorded.
    edited_tests = any(
        not (work / t).exists() or hashlib.sha256((work / t).read_bytes()).hexdigest() != h
        for t, h in tests_before.items()
    )
    for t in meta["test"]:
        shutil.copy(src / t, work / t)
    try:
        if lang == "mongo":
            cmd = ["uv", "run", "--no-project", "--with", "pymongo", "python", str(ROOT / "harness/grade_mongo.py"),
                   str(work / "pipeline.json"), str(src / "expected.json")]  # fmt: skip
        elif lang == "cpp":
            sources = [f for f in meta["solution"] if f.endswith(".cpp")]
            cmd = ["sh", "-c", "c++ -std=c++17 -DEXERCISM_RUN_ALL_TESTS -I. -o .graded "
                   + " ".join([*sources, *meta["test"], "test/tests-main.cpp"]) + " 2>&1 && ./.graded"]  # fmt: skip
        else:
            cmd = ["uv", "run", "--no-project", "--with", "pytest", "pytest", "-q", "-x", *meta["test"]]
        test = subprocess.run(
            cmd,
            cwd=work,
            capture_output=True,
            text=True,
            timeout=300,
        )
        passed, tail = test.returncode == 0, test.stdout[-600:]
    except subprocess.TimeoutExpired:
        passed, tail = False, "test timeout"
    row = {
        "task": name,
        "passed": passed,
        "edited_tests": edited_tests,
        "agent_s": round(agent_s, 1),
        "turns": out.get("num_turns"),
        "cost_usd": out.get("total_cost_usd"),
        "session_id": out.get("session_id"),
        "agent_said": (out.get("result") or "")[-400:],
        "test_tail": tail,
    }
    if RAINDROP:
        version = harness.get("id", "seed")
        event_id = raindrop.track_ai(
            user_id=f"harness-{version}",
            event="polyglot_task",
            model=harness["model"],
            input=prompt,
            output=out.get("result") or "(no final message)",
            convo_id=out.get("session_id"),
            properties={
                "harness_version": version,
                "task": name,
                "passed": passed,
                "edited_tests": edited_tests,
                "turns": out.get("num_turns"),
                "hit_turn_limit": out.get("subtype") == "error_max_turns",
                "rules": harness["rules"],
                "test_tail": tail,
            },
        )
        row["trace_id"] = event_id
        raindrop.track_signal(
            event_id=event_id,
            name="tests_passed" if passed else "tests_failed",
            sentiment="POSITIVE" if passed else "NEGATIVE",
        )
        if edited_tests:
            raindrop.track_signal(event_id=event_id, name="edited_tests", sentiment="NEGATIVE")
    return row


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
    summary = {
        "harness": harness,
        "n": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "wall_s": round(wall, 1),
        "workers": a.workers,
        "raindrop": RAINDROP,
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "rows": rows,
    }
    if RAINDROP:
        raindrop.flush()
        raindrop.shutdown()
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
