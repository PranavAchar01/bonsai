"""The Bonsai loop: the agent rewrites its own harness, and only earned changes are kept.

One generation:
  1. run the current best harness on the practice set, traced to Raindrop
  2. digest the failures (what the agent said, what the tests said, any tampering)
  3. a proposer model writes ONE change to the harness aimed at that failure pattern
  4. score the child on the held-out set
  5. keep it only if held-out score rises and no test file was touched; otherwise prune
  6. store the version (kept or pruned) as a document

Layout follows MaxRobinsonTheGreat/fractalsearch (itself adapted from karpathy/autoresearch):
a fixed evaluator the agent cannot edit, a folder of candidates, and an append-only run log.
"""

import argparse
import difflib
import json
import os
import subprocess
import time
from pathlib import Path

from harness import evaluate as runner

ROOT = Path(__file__).parent
VERSIONS = ROOT / "versions"
LOG = ROOT / "runs" / "versions.jsonl"
ALLOWED_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
MAX_TURNS_CAP = 30

PROPOSER = """You improve a coding agent by editing its harness. The harness is JSON:
{{"rules": system prompt, "tools": subset of {tools}, "max_turns": int up to {cap}, "model": fixed}}

Current harness:
{harness}

The person tending this agent asked for: {goal}

It just ran {n} problems and failed {k}. Failures:
{failures}

Changes already tried from this parent and pruned (do not repeat them):
{pruned}

Make exactly ONE change aimed at the most common failure pattern. A bonsai is shaped by cutting:
whatever you add, you must also SHED something of similar size (a rule line, a tool, turns) that
the failures show is not earning its place. The harness should not grow. Do not name specific problems
in the rules: the change is scored on problems you have not seen. Do not change "model".
Reply with JSON only:
{{"change": "one sentence", "shed": "what was removed, a few words", "signal": "the failure pattern in a few words", "harness": {{...}}}}"""


def store(doc: dict) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(doc) + "\n")
    versions = [json.loads(line) for line in LOG.read_text().splitlines()]
    (ROOT / "web" / "lineage.json").write_text(json.dumps({"versions": versions}))
    uri = os.getenv("MONGODB_URI")
    if uri:
        from pymongo import MongoClient

        MongoClient(uri)["bonsai"]["versions"].replace_one(
            {"id": doc["id"]}, doc, upsert=True
        )


def score(harness: dict, tasks: list[str], scratch: Path, workers: int, on_task=None) -> dict:
    """on_task(state, task, row) is called with "queued", "start" and "end" for each problem."""
    from concurrent.futures import ThreadPoolExecutor

    def one(t: str) -> dict:
        if on_task:
            on_task("start", t, None)
        row = runner.run_one(t, harness, scratch)
        if on_task:
            on_task("end", t, row)
        return row

    if on_task:
        for t in tasks:
            on_task("queued", t, None)
    with ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(one, tasks))
    return {
        "score": sum(r["passed"] for r in rows) / len(rows),
        "cheated": any(r["edited_tests"] for r in rows),
        "rows": rows,
    }


def propose(parent: dict, result: dict, pruned: list[str], model: str, goal: str = "") -> dict:
    fails = [r for r in result["rows"] if not r["passed"] or r["edited_tests"]]
    digest = "\n".join(
        f"- {r['task']} (turns {r['turns']}, edited tests: {r['edited_tests']})\n"
        f"  agent said: {r['agent_said'][-300:]!r}\n  tests said: {r['test_tail'][-300:]!r}"
        for r in fails
    )
    prompt = PROPOSER.format(
        tools=ALLOWED_TOOLS,
        cap=MAX_TURNS_CAP,
        harness=json.dumps(
            {k: parent[k] for k in ("rules", "tools", "max_turns", "model")}, indent=1
        ),
        goal=goal or "a higher score on problems it has not seen",
        n=len(result["rows"]),
        k=len(fails),
        failures=digest or "(none)",
        pruned="\n".join(f"- {p}" for p in pruned) or "(none)",
    )
    out = subprocess.run(
        [runner.CLAUDE, "-p", prompt, "--model", model, "--output-format", "json", "--max-turns", "1",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        capture_output=True, text=True, timeout=300, cwd=ROOT / "runs",
    )  # fmt: skip
    text = json.loads(out.stdout)["result"]
    reply = json.loads(text[text.index("{") : text.rindex("}") + 1])
    child = reply["harness"]
    child["model"] = parent["model"]
    child["tools"] = [t for t in child["tools"] if t in ALLOWED_TOOLS]
    child["max_turns"] = min(int(child["max_turns"]), MAX_TURNS_CAP)
    return {"change": reply["change"], "shed": reply.get("shed", "nothing"), "signal": reply["signal"], "harness": child}


def diff(a: dict, b: dict) -> str:
    keys = ("rules", "tools", "max_turns")
    return "\n".join(
        difflib.unified_diff(
            json.dumps({k: a[k] for k in keys}, indent=1).splitlines(),
            json.dumps({k: b[k] for k in keys}, indent=1).splitlines(),
            "parent", "child", lineterm="",
        )
    )  # fmt: skip


def run(practice, heldout, generations=3, tries=2, workers=3, proposer="sonnet", scratch=None,
        goal="", parent=None, emit=None, stopped=lambda: False) -> None:  # fmt: skip
    """Grow the tree. `parent` is a stored version to branch from; without one the seed is scored first.

    emit(kind, **data) reports what is happening: phase, task, proposed, version.
    """
    emit = emit or (lambda kind, **d: None)
    scratch = Path(scratch)
    VERSIONS.mkdir(exist_ok=True)

    def tasks_cb(vid: str, split: str):
        return lambda state, task, row: emit("task", version=vid, split=split, task=task, state=state, row=row)

    if parent:
        best = {**parent["harness"], "id": parent["id"]}
        best_score, first = parent["score"], parent["generation"] + 1
    else:
        best, first = {**runner.BASE_HARNESS, "id": "g0"}, 1
        emit("phase", text="scoring the seed on held-out problems", version="g0")
        held = score(best, heldout, scratch / "g0-held", workers, tasks_cb("g0", "held-out"))
        best_score = held["score"]
        (VERSIONS / "g0.json").write_text(json.dumps(best, indent=1))
        doc = {"id": "g0", "parent": None, "generation": 0, "change": "seed harness", "signal": None, "harness": dict(runner.BASE_HARNESS),
               "kept": True, "score": best_score, "tokens": len(best["rules"]) // 4, "diff": "",
               "trace_id": None, "ts": time.time()}  # fmt: skip
        store(doc)
        emit("version", doc=doc)
        print(f"g0 seed held-out {best_score:.2f}", flush=True)

    for gen in range(first, first + generations):
        if stopped():
            return
        emit("phase", text=f"running {best['id']} on practice problems to find what fails", version=best["id"])
        practice_run = score(best, practice, scratch / f"g{gen}-practice", workers, tasks_cb(best["id"], "practice"))
        pruned: list[str] = []
        for i in range(tries):
            if stopped():
                return
            vid = f"g{gen}{'abcdef'[i]}"
            emit("phase", text=f"proposer is reading the failures and writing {vid}", version=vid)
            emit("task", version=vid, split="propose", task="one change, one thing shed", state="start", row=None)
            p = propose(best, practice_run, pruned, proposer, goal)
            emit("task", version=vid, split="propose", task="one change, one thing shed", state="end",
                 row={"passed": True, "edited_tests": False, "said": p["change"]})  # fmt: skip
            child = {**p["harness"], "id": vid}
            emit("phase", text=f"scoring {vid} on held-out problems: {p['change']}", version=vid)
            res = score(child, heldout, scratch / f"{vid}-held", workers, tasks_cb(vid, "held-out"))
            kept = res["score"] > best_score and not res["cheated"]
            fail_trace = next(
                (r.get("trace_id") for r in practice_run["rows"] if not r["passed"]), None
            )
            doc = {"id": vid, "parent": best["id"], "generation": gen, "change": p["change"], "shed": p["shed"], "harness": p["harness"],
                   "signal": "cheat caught" if res["cheated"] else p["signal"], "kept": kept,
                   "score": res["score"], "tokens": len(child["rules"]) // 4,
                   "diff": diff(best, child), "trace_id": fail_trace, "ts": time.time()}  # fmt: skip
            store(doc)
            emit("version", doc=doc)
            (VERSIONS / f"{vid}.json").write_text(json.dumps(child, indent=1))
            print(f"{vid} {'KEPT' if kept else 'PRUNED'} {res['score']:.2f} (best {best_score:.2f}) "
                  f"{'CHEAT ' if res['cheated'] else ''}{p['change']}", flush=True)  # fmt: skip
            if kept:
                best, best_score = child, res["score"]
                break
            pruned.append(p["change"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--practice", nargs="+", required=True, help="problems the agent learns from"
    )
    ap.add_argument(
        "--heldout", nargs="+", required=True, help="problems that decide keep or prune"
    )
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--tries", type=int, default=2, help="children per generation")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--proposer", default="sonnet")
    ap.add_argument("--goal", default="", help="what the person wants the agent to get better at")
    ap.add_argument("--scratch", required=True)
    a = ap.parse_args()
    run(a.practice, a.heldout, a.generations, a.tries, a.workers, a.proposer, a.scratch, a.goal)
    if runner.RAINDROP:
        runner.raindrop.flush()
        runner.raindrop.shutdown()


if __name__ == "__main__":
    main()
