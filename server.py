"""Local Bonsai app: the page, plus the controls that start and stop a run.

  GET  /api/state  what is running now: the job, every agent, the activity log
  POST /api/grow   {set, goal, generations}  start growing
  POST /api/sets   {name, rules, items: [{q, a}]}  connect a question-and-answer benchmark made on the page
  POST /api/agents {name, model, rules, tools, max_turns}  save an agent made on the page; Grow can start from it
  GET  /api/watch?version=&task=  what one agent is doing right now, step by step (runs/live/)
  POST /api/stop   finish the current step, then stop

Everything else is served from web/. Binds to 127.0.0.1 only.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import loop
from harness import evaluate as runner

ROOT = Path(__file__).parent
WEB = ROOT / "web"
JOB_FILE = ROOT / "runs" / "job.json"
HEAVY_LOCK = Path.home() / "helloworld" / ".heavy-lock"
PORT = int(os.getenv("PORT", "8765"))

lock = threading.Lock()
stop = threading.Event()
state: dict = {"job": None, "agents": [], "log": []}


def problems(lang: str) -> list[str]:
    base = runner.TASKS / lang if runner.task_set(lang) else runner.BENCH / lang / "exercises/practice"
    if not base.exists():
        return []
    names = sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("_"))
    return names if lang == "python" else [f"{lang}/{n}" for n in names]


def port_open(port: int) -> bool:
    import socket

    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def connected() -> list[tuple]:
    """Task sets dropped into tasks/: one folder each, described by set.json (see tasks/README.md)."""
    rows = []
    for f in sorted(runner.TASKS.glob("*/set.json")):
        spec = json.loads(f.read_text())
        port = spec.get("needs_port")
        rows.append((f.parent.name, spec["name"], "Connected agents and tasks", not port or port_open(port), spec.get("needs", "")))
    return rows


def sets_rows() -> list[tuple]:
    poly = "Aider Polyglot (Exercism practice problems)"
    have = lambda tool: shutil.which(tool) is not None  # noqa: E731
    return [
        *connected(),
        ("cpp", "Aider Polyglot: C++", poly, have("c++"), "needs a C++ compiler"),
        ("python", "Aider Polyglot: Python", poly, True, ""),
    ]


def sets() -> list[dict]:
    """What the dropdown offers. A set is only selectable if this machine can grade it."""
    return [{"key": k, "name": n, "group": g, "ready": r, "why": w, "count": len(problems(k)), "view": view_for(k),
             "tools": list((runner.task_set(k) or {}).get("tools", {}).keys()) if (runner.task_set(k) or {}).get("agent") else CLAUDE_TOOLS}
            for k, n, g, r, w in sets_rows()]  # fmt: skip


# Live views: one small page per benchmark that shows the agent at work, drawn in a sandboxed frame.
# Chess, question sets and coding have a hand-built one. Anything else gets one written for it by the
# proposer model into web/views/gen/<set>.html, so the app's own code grows with each new kind of task.
VIEWS = WEB / "views"
VIEW_CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\' '
            "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; img-src data:\">")  # fmt: skip
viewgen: set[str] = set()


def view_for(key: str) -> str:
    made = VIEWS / "gen" / f"{key}.html"
    if made.exists():
        return f"views/gen/{key}.html?v={int(made.stat().st_mtime)}"
    spec = runner.task_set(key) or {}
    if key == "chess":
        return "views/chess.html"
    return "views/qa.html" if "grade_exact" in spec.get("grade", "") else "views/steps.html"


def question(task: str) -> str:
    """The problem as the agent sees it, for the live view."""
    lang, _, name = task.rpartition("/")
    for base in (runner.TASKS / task, runner.BENCH / (lang or "python") / "exercises/practice" / name):
        f = base / ".docs" / "instructions.md"
        if f.exists():
            return f.read_text()[:3000]
    return ""


def sample_lines(key: str) -> list[dict]:
    """A real live log from this set, if any run has produced one, so a written view fits real data."""
    live = ROOT / "runs" / "live"
    dirs = sorted((d for d in live.iterdir() if d.is_dir()), key=lambda d: -d.stat().st_mtime) if live.exists() else []
    for d in dirs:
        for name in problems(key)[:40]:
            f = runner.live_path(d.name, name)
            if f.exists():
                return [json.loads(x) for x in f.read_text().splitlines()[:30] if x.strip()]
    return []


def write_view(key: str, hint: str) -> None:
    try:
        spec, names = runner.task_set(key) or {"name": key}, problems(key)
        prompt = f"""Write one self-contained HTML page: a small live view (about 340 x 290 px) that shows an AI agent
working on one problem from the benchmark "{spec.get("name", key)}". It must make what the agent is doing visible at a
glance and fit THIS benchmark, not be a generic log. {("The user asked: " + hint) if hint else ""}

How it gets data: the host posts a message about once a second; the page redraws from it every time:
  {{lines: [...], question: "the problem text", agent: {{task, version, status, model}}, set: {{key, name}}, colors: {{paper, ink, faint, seal, moss}}}}
agent.status is "working", "passed", "failed" or "cheat". lines is the agent's log so far, oldest first. Each line has
t (seconds) and kind: "start", "tool" (tool, text), "said" (text), "result_of_tool" (text), "result" (text),
"verdict" (passed, edited_tests, text). Other kinds may appear; ignore what you do not know.

The task: {json.dumps({k: spec.get(k) for k in ("prompt", "solution", "tools")})}
A sample problem: {question(names[0])[:1200] if names else "(none yet)"}
Sample lines from a real run: {json.dumps(sample_lines(key))[:3000] or "(none yet)"}

Rules: no network, no external scripts, no images, no libraries, no storage. Inline CSS and one inline script.
Escape every string before putting it in innerHTML. Under 250 lines. Paper and ink look, no cards, no gradients,
no emoji: use the CSS variables below and the fonts "DotGothic16" (display) and "IBM Plex Mono" (text).
Here is the hand-built view for question sets, as the pattern to follow:

{(VIEWS / "qa.html").read_text()}

Reply with the HTML only, starting with <!doctype html>."""
        out = subprocess.run([runner.CLAUDE, "-p", prompt, "--model", "claude-opus-5-5", "--output-format", "json", "--max-turns", "1"],
                             capture_output=True, text=True, timeout=420)  # fmt: skip
        text = json.loads(out.stdout)["result"]
        low = text.lower()
        html = text[low.index("<!doctype") : low.rindex("</html>") + 7]
        # The page is model output: it runs in a sandboxed frame, and it may not load or call anything.
        if "addEventListener" not in html or len(html) > 60_000 or re.search(r"<script[^>]+src=|<iframe|<object|<embed", html, re.I):
            raise ValueError("the reply was not a usable view")
        html = re.sub(r"<meta[^>]+Content-Security-Policy[^>]*>", "", html, flags=re.I)
        html = re.sub(r"<head[^>]*>", lambda m: m[0] + VIEW_CSP, html, count=1, flags=re.I)
        if VIEW_CSP not in html:
            raise ValueError("the reply had no head")
        (VIEWS / "gen").mkdir(parents=True, exist_ok=True)
        (VIEWS / "gen" / f"{key}.html").write_text(html)
        say(f"live view written for {spec.get('name', key)}: web/views/gen/{key}.html, {html.count(chr(10)) + 1} lines")
    except Exception as e:  # the hand-built view stays in place
        say(f"live view not written for {key}: {type(e).__name__}: {str(e)[-160:]}")
    finally:
        viewgen.discard(key)


def generate_view(body: dict) -> tuple[int, dict]:
    key, hint = str(body.get("set") or ""), " ".join(str(body.get("hint") or "").split())[:300]
    if key not in {k for k, *_ in sets_rows()} or not re.fullmatch(r"[\w-]+", key):
        return 400, {"error": "no such benchmark"}
    if key in viewgen:
        return 409, {"error": "a view is already being written for it"}
    viewgen.add(key)
    say(f"writing a live view for {key}" + (f": {hint}" if hint else ""))
    threading.Thread(target=write_view, args=(key, hint), daemon=True).start()
    return 200, {"ok": True}


# Agents made on the page: a name and a starting harness. Grow can plant one as the seed of a new tree.
AGENTS = ROOT / "agents"
CLAUDE_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
MODELS = ["haiku", "sonnet", "opus"]


def saved_agents() -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted(AGENTS.glob("*.json"))] if AGENTS.exists() else []


def save_agent(body: dict) -> tuple[int, dict]:
    name = " ".join(str(body.get("name") or "").split())[:40]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        return 400, {"error": "give it a name"}
    model = body.get("model") if body.get("model") in MODELS else "haiku"
    tools = [t for t in body.get("tools") or [] if isinstance(t, str) and re.fullmatch(r"\w+", t)][:12]
    agent = {"key": slug, "name": name, "model": model, "rules": str(body.get("rules") or "").strip()[:2000],
             "tools": tools, "max_turns": max(1, min(int(body.get("max_turns") or 12), 30))}  # fmt: skip
    if not agent["rules"]:
        return 400, {"error": "give it starting rules"}
    AGENTS.mkdir(exist_ok=True)
    (AGENTS / f"{slug}.json").write_text(json.dumps(agent, indent=1))
    say(f"agent saved: {name} ({model}, {len(tools)} tools, {agent['max_turns']} turns)")
    return 200, agent


def watch(query: str) -> tuple[int, dict]:
    """The live log one agent writes while it works: runs/live/<version>/<task>.jsonl."""
    from urllib.parse import parse_qs
    q = {k: v[0] for k, v in parse_qs(query).items()}
    version, task = q.get("version", ""), q.get("task", "")
    if not re.fullmatch(r"[\w-]+", version) or not re.fullmatch(r"[\w./-]+", task) or ".." in task:
        return 400, {"error": "bad agent"}
    f = runner.live_path(version, task)
    lines = [json.loads(x) for x in f.read_text().splitlines()[-300:] if x.strip()] if f.exists() else []
    return 200, {"lines": lines, "question": question(task)}


def create_set(body: dict) -> tuple[int, dict]:
    """A benchmark made on the page: questions with one right answer each, graded by harness/grade_exact.py."""
    import re

    name = " ".join(str(body.get("name") or "").split())[:60]
    items = [
        (str(i.get("q") or "").strip(), " ".join(str(i.get("a") or "").split()))
        for i in (body.get("items") or [])
        if isinstance(i, dict)
    ]
    items = [(q[:4000], a[:200]) for q, a in items if q and a][:200]
    if not name:
        return 400, {"error": "give the benchmark a name"}
    if len(items) < 4:
        return 400, {"error": "a benchmark needs at least 4 questions, each with an answer"}
    key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "set"
    if key in {k for k, *_ in sets_rows()} or (runner.TASKS / key).exists():
        key = f"{key}-{int(time.time()) % 100000}"
    rules = " ".join(str(body.get("rules") or "").split())[:1000] or "Answer the question in .docs/instructions.md."
    base = runner.TASKS / key
    for n, (q, a) in enumerate(items, 1):
        d = base / f"q{n:03d}"
        (d / ".docs").mkdir(parents=True)
        (d / ".docs" / "instructions.md").write_text(q + "\n\nWrite only your final answer to answer.txt, nothing else.\n")
        (d / "expected.json").write_text(json.dumps({"answer": a}))
    spec = {"name": name, "prompt": "Answer the question in .docs/instructions.md and write only the answer to answer.txt.",
            "solution": ["answer.txt"], "hidden": ["expected.json"],
            "grade": "python3 {root}/harness/grade_exact.py answer.txt {task}/expected.json",
            "seed": {"rules": rules, "tools": ["Read", "Write"], "max_turns": 4}}  # fmt: skip
    (base / "set.json").write_text(json.dumps(spec, indent=1))
    say(f"new benchmark connected: {name}, {len(items)} questions")
    generate_view({"set": key})
    return 200, {"ok": True, "key": key, "count": len(items)}


def say(text: str) -> None:
    with lock:
        state["log"].append({"ts": time.time(), "text": text})
        del state["log"][:-200]


def emit(kind: str, **d) -> None:
    if kind == "phase":
        with lock:
            state["job"]["phase"] = d["text"]
        say(d["text"])
    elif kind == "version":
        doc = d["doc"]
        verdict = "seed" if not doc["parent"] else "kept" if doc["kept"] else "pruned"
        say(f"{doc['id']} {verdict} at {doc['score']:.0%} held-out. {doc['change']}")
    elif kind == "task":
        role = "proposer" if d["split"] == "propose" else "solver"
        key = (d["version"], d["split"], d["task"])
        with lock:
            agent = next(
                (
                    a
                    for a in state["agents"]
                    if a["key"] == key and a["status"] in ("queued", "working")
                ),
                None,
            )
            if d["state"] == "queued" or agent is None:
                agent = {"key": key, "role": role, "version": d["version"], "split": d["split"], "task": d["task"],
                         "model": state["job"]["proposer"] if role == "proposer" else "haiku",
                         "status": "queued", "queued": time.time()}  # fmt: skip
                state["agents"].append(agent)
                del state["agents"][:-300]
            if d["state"] == "start":
                agent.update(status="working", started=time.time())
            elif d["state"] == "end":
                row = d["row"]
                agent.update(
                    status="cheat" if row.get("edited_tests") else "passed" if row["passed"] else "failed",
                    ended=time.time(), turns=row.get("turns"), trace=row.get("trace_id"), said=(row.get("said") or row.get("agent_said") or "")[-220:],
                )  # fmt: skip


def take_heavy_lock() -> None:
    """One heavy job at a time on this machine: wait for the lock, clear it if its owner is gone."""
    HEAVY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        try:
            HEAVY_LOCK.mkdir()
            (HEAVY_LOCK / "owner").write_text(str(os.getpid()))
            return
        except FileExistsError:
            try:
                os.kill(int((HEAVY_LOCK / "owner").read_text()), 0)
                with lock:
                    state["job"]["phase"] = (
                        "waiting for another heavy job on this machine to finish"
                    )
                time.sleep(5)
            except (OSError, ValueError):
                shutil.rmtree(HEAVY_LOCK, ignore_errors=True)


def job(cfg: dict) -> None:
    held_lock = False
    try:
        take_heavy_lock()
        held_lock = not stop.is_set()
        if held_lock:
            loop.run(cfg["practice"], cfg["heldout"], cfg["generations"], 2, cfg["workers"], cfg["proposer"],
                     cfg["scratch"], cfg["goal"], cfg["parent"], emit, stop.is_set, start=cfg.get("start"))  # fmt: skip
        with lock:
            state["job"].update(
                status="stopped" if stop.is_set() else "done",
                phase="",
                ended=time.time(),
            )
        say("stopped" if stop.is_set() else "run finished")
        if held_lock and loop.report(state["job"].get("set", "")):
            say("context file and change list written: web/out/latest/context.md, CHANGES.md")
    except (
        Exception
    ) as e:  # shown on the page: a run that dies silently looks like a hang
        with lock:
            state["job"].update(
                status="error",
                phase="",
                error=f"{type(e).__name__}: {e}"[:300],
                ended=time.time(),
            )
        say(f"run failed: {type(e).__name__}: {e}"[:300])
    finally:
        if held_lock:
            shutil.rmtree(HEAVY_LOCK, ignore_errors=True)
        if runner.RAINDROP:
            runner.raindrop.flush()


def grow(body: dict) -> tuple[int, dict]:
    with lock:
        if state["job"] and state["job"]["status"] == "running":
            return 409, {"error": "a run is already going"}
    lang = body.get("set")
    chosen = next((x for x in sets() if x["key"] == lang), None)
    if not chosen or not chosen["ready"]:
        return 400, {"error": "that set cannot be graded on this machine yet"}
    names = problems(lang)
    count = max(4, min(int(body.get("count") or len(names)), len(names)))
    names = names[:count]
    practice, heldout = names[0::2], names[1::2]
    generations = max(1, min(int(body.get("generations") or 2), 5))
    goal = " ".join(str(body.get("goal") or "").split())[:300]

    # A score only means something against the same held-out problems, so a new split starts a new tree.
    # An agent made on the page plants a new seed, so it starts its own tree too.
    start = next((a for a in saved_agents() if a["key"] == body.get("agent")), None)
    split = {"lang": lang, "practice": practice, "heldout": heldout, "agent": start["key"] if start else None}
    previous = json.loads(JOB_FILE.read_text()) if JOB_FILE.exists() else None
    versions = (
        [json.loads(line) for line in loop.LOG.read_text().splitlines()]
        if loop.LOG.exists()
        else []
    )
    if versions and previous != split:
        archive = loop.LOG.with_name(f"versions-{int(time.time())}.jsonl")
        loop.LOG.rename(archive)
        (WEB / "lineage.json").unlink(missing_ok=True)
        versions = []
    parent = None
    if versions:
        wanted = body.get("parent")
        kept = [v for v in versions if v["kept"]]
        parent = next(
            (v for v in versions if v["id"] == wanted and v.get("harness")), kept[-1]
        )
        parent = {**parent, "generation": max(v["generation"] for v in versions)}
    JOB_FILE.parent.mkdir(exist_ok=True)
    JOB_FILE.write_text(json.dumps(split))

    stop.clear()
    cfg = {**split, "start": start, "goal": goal, "generations": generations, "workers": 4, "proposer": "claude-opus-5-5", "parent": parent,
           "scratch": str(Path(os.getenv("BONSAI_SCRATCH", ROOT / "runs" / "scratch")))}  # fmt: skip
    with lock:
        state["agents"].clear()
        state["job"] = {"status": "running", "goal": goal, "set": chosen["name"], "practice": len(practice),
                        "heldout": len(heldout), "generations": generations, "proposer": cfg["proposer"],
                        "from": parent["id"] if parent else "seed", "phase": "starting", "started": time.time()}  # fmt: skip
    say(f"asked to improve on {chosen['name']}{': ' + goal if goal else ''} ({len(practice)} practice, {len(heldout)} held-out, "
        f"{generations} generations, from {parent['id'] if parent else start['name'] if start else 'the seed'})")  # fmt: skip
    threading.Thread(target=job, args=(cfg,), daemon=True).start()
    return 200, {"ok": True}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(WEB), **k)

    def log_message(self, *a):
        pass

    def send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        # Saved games, read-only: /replays/<set>/<version>/<game>.json from tasks/<set>/_replays/.
        m = re.fullmatch(r"/replays/([\w-]+)/([\w-]+)/([\w-]+)\.json", path)
        if m:
            f = runner.TASKS / m[1] / "_replays" / m[2] / f"{m[3]}.json"
            return self.send_json(200, json.loads(f.read_text())) if f.exists() else self.send_json(404, {"error": "no replay"})
        if path == "/api/watch":
            return self.send_json(*watch(self.path.partition("?")[2]))
        if path != "/api/state":
            return super().do_GET()
        with lock:
            agents = [
                {k: v for k, v in a.items() if k != "key"} for a in state["agents"]
            ]
            body = {"job": state["job"], "agents": agents, "log": state["log"][-80:], "now": time.time(),
                    "sets": sets(), "studio": saved_agents(), "viewgen": sorted(viewgen),
                    "raindrop": runner.RAINDROP}  # fmt: skip
        self.send_json(200, body)

    def do_POST(self):
        # Only this page may start runs: a run spends model credit.
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host.split(":")[0] not in ("localhost", "127.0.0.1") or (
            origin and origin.split("//")[-1] != host
        ):
            return self.send_json(403, {"error": "local page only"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_json(415, {"error": "json only"})
        try:
            body = json.loads(
                self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2_000_000 if self.path == "/api/sets" else 4096))
                or b"{}"
            )
        except ValueError:
            return self.send_json(400, {"error": "bad json"})
        if self.path == "/api/grow":
            try:
                return self.send_json(*grow(body))
            except (TypeError, ValueError):
                return self.send_json(400, {"error": "bad request"})
        if self.path == "/api/sets":
            try:
                return self.send_json(*create_set(body))
            except (TypeError, ValueError, OSError) as e:
                return self.send_json(400, {"error": f"could not create it: {e}"[:200]})
        if self.path == "/api/views":
            return self.send_json(*generate_view(body))
        if self.path == "/api/agents":
            try:
                return self.send_json(*save_agent(body))
            except (TypeError, ValueError, OSError) as e:
                return self.send_json(400, {"error": f"could not save it: {e}"[:200]})
        if self.path == "/api/stop":
            stop.set()
            say("stop asked: finishing the current step")
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"Bonsai on http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
