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


def live_path(version: str, task: str) -> Path:
    """Where one agent writes what it is doing, step by step, so the page can watch it: runs/live/<version>/<task>.jsonl."""
    return ROOT / "runs" / "live" / version / (task.replace("/", "__") + ".jsonl")


def stream_claude(cmd: list[str], cwd: Path, timeout: int, live: Path) -> dict:
    """Run `claude -p` with stream-json output: every step goes to the live log, the final result is returned."""
    import threading

    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    out, tail = None, []
    try:
        with live.open("a") as log:
            for line in proc.stdout:
                tail.append(line)
                del tail[:-20]
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "result":
                    out = ev
                for step in steps(ev):
                    log.write(json.dumps({"t": time.time(), **step}) + "\n")
                    log.flush()
        proc.wait()
    finally:
        timer.cancel()
    if out is None:
        return {"result": "agent timeout" if proc.returncode in (-9, 137) else "".join(tail)[-500:] or proc.stderr.read()[-500:]}
    return out


def steps(ev: dict) -> list[dict]:
    """One stream-json event as short, readable steps: what the agent said, which tool it used, what came back."""
    kind, msg = ev.get("type"), ev.get("message") or {}
    if kind == "result":
        return [{"kind": "result", "text": str(ev.get("result") or ev.get("subtype") or "")[-600:]}]
    if not isinstance(msg, dict):  # some stream events carry "message" as plain text
        return []
    out = []
    for part in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
        if part.get("type") == "text" and kind == "assistant":
            out.append({"kind": "said", "text": part["text"][-600:]})
        elif part.get("type") == "tool_use":
            arg = part.get("input") or {}
            brief = arg.get("file_path") or arg.get("command") or arg.get("pattern") or json.dumps(arg)[:160]
            out.append({"kind": "tool", "tool": part.get("name"), "text": str(brief)[-200:]})
        elif part.get("type") == "tool_result":
            body = part.get("content")
            body = " ".join(b.get("text", "") for b in body if isinstance(b, dict)) if isinstance(body, list) else str(body or "")
            out.append({"kind": "result_of_tool", "text": body[:240]})
    return out


def task_set(key: str) -> dict | None:
    """A connected task set: tasks/<key>/set.json. See tasks/README.md."""
    f = TASKS / key / "set.json"
    return json.loads(f.read_text()) if f.exists() else None


def run_one(name: str, harness: dict, scratch: Path) -> dict:
    # A task is "name" (Python) or "lang/name", for example "cpp/zebra-puzzle".
    lang, _, ex = name.rpartition("/")
    lang = lang or "python"
    work = (scratch / lang / ex).resolve()
    shutil.rmtree(work, ignore_errors=True)
    spec = task_set(lang)
    if spec:
        # A connected set (tasks/<set>/set.json): files named in "hidden" never enter the agent's folder.
        src = TASKS / lang / ex
        if (TASKS / lang / "_shared").exists():
            shutil.copytree(TASKS / lang / "_shared", work)
        shutil.copytree(src, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*spec.get("hidden", [])))
        meta = {"solution": spec.get("solution", []), "test": []}
    else:
        src = BENCH / lang / "exercises/practice" / ex
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("example.py"))
        meta = json.loads((work / ".meta/config.json").read_text())["files"]
        shutil.rmtree(work / ".meta")
    tests_before = {t: hashlib.sha256((work / t).read_bytes()).hexdigest() for t in meta["test"]}
    prompt = f"Implement {', '.join(meta['solution'])} so that {', '.join(meta['test'])} passes."
    if spec:
        prompt = spec["prompt"]
    t0 = time.time()
    live = live_path(harness.get("id", "seed"), name)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps({"t": t0, "kind": "start", "text": prompt[:300]}) + "\n")
    try:
        own = None
        if spec and spec.get("agent"):
            # The set brings its own agent: a shell command that reads the harness and the prompt from the environment.
            (work / ".harness.json").write_text(json.dumps(harness))
            own = {**os.environ, "BONSAI_HARNESS": str(work / ".harness.json"), "BONSAI_PROMPT": prompt, "BONSAI_TASK": str(src),
                   "BONSAI_LIVE": str(live)}  # fmt: skip
        if not own:
            out = stream_claude([
                CLAUDE, "-p", prompt, "--model", harness["model"], "--output-format", "stream-json", "--verbose",
                "--system-prompt", harness["rules"], "--allowedTools", ",".join(harness["tools"]),
                "--max-turns", str(harness["max_turns"]), "--permission-mode", "acceptEdits",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            ], work, (spec or {}).get("timeout", 600), live)  # fmt: skip
        agent = None if not own else subprocess.run(
            ["sh", "-c", spec["agent"].replace("{root}", str(ROOT))],
            cwd=work, env=own, capture_output=True, text=True, timeout=spec.get("timeout", 600),
        )  # fmt: skip
        if agent:
            out = (
                json.loads(agent.stdout)
                if agent.stdout.strip().startswith("{")
                else {"result": (agent.stdout or agent.stderr)[-500:]}
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
        if spec:
            cmd = ["sh", "-c", spec["grade"].replace("{task}", str(src)).replace("{root}", str(ROOT))]
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
        passed, tail = test.returncode == 0, (test.stdout + test.stderr)[-600:]
    except subprocess.TimeoutExpired:
        passed, tail = False, "test timeout"
    with live.open("a") as log:
        log.write(json.dumps({"t": time.time(), "kind": "verdict", "passed": passed, "edited_tests": edited_tests, "text": tail[-300:]}) + "\n")
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
            event=f"{lang}_task" if spec else "polyglot_task",
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
        # The free Raindrop plan allows one custom signal, so pass or fail lives in its sentiment.
        raindrop.track_signal(
            event_id=event_id,
            name="task_outcome",
            signal_type="feedback",
            sentiment="POSITIVE" if passed and not edited_tests else "NEGATIVE",
            comment=("edited the tests. " if edited_tests else "") + tail[-300:],
        )
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
    tasks = a.tasks or sorted(p.name for p in PRACTICE.iterdir())[: a.n]
    # Without --harness, a connected set is scored on its own seed (set.json "seed"), the same one the loop starts from.
    own = (task_set(tasks[0].rpartition("/")[0]) or {}).get("seed", {})
    harness = json.loads(Path(a.harness).read_text()) if a.harness else {**BASE_HARNESS, **own}
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
