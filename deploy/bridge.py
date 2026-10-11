"""Connect the local Bonsai app to the hosted page on Vercel, through MongoDB Atlas.

Run it next to server.py on the machine that does the runs:

  uv run --no-project --with pymongo --with python-dotenv python deploy/bridge.py

Twice a second it copies the app's live state (/api/state), web/lineage.json and runs/*-versions.jsonl
into bonsai.live, and carries out requests the hosted page left in bonsai.rpc (Grow, Stop, watching an
agent, saved games) against the local app. Requests older than 10 s are ignored, so a Grow pressed while
this was off never fires later. Needs MONGODB_URI in .env.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
APP = f"http://127.0.0.1:{os.getenv('PORT', '8765')}"
db = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=30000)["bonsai"]
live, rpc = db["live"], db["rpc"]
sent: dict[str, str] = {}


def call(method: str, path: str, body: str | None = None) -> tuple[int, str, str]:
    req = urllib.request.Request(APP + path, data=body.encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"} if body is not None else {})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.headers.get("Content-Type", "application/json"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e.read().decode()


def mirror(key: str, body: str, ctype: str, heartbeat: bool = False) -> None:
    digest = hashlib.sha256(body.encode()).hexdigest()
    if sent.get(key) != digest or heartbeat:
        live.replace_one({"_id": key}, {"_id": key, "body": body, "type": ctype, "ts": time.time() * 1000}, upsert=True)
        sent[key] = digest


def tick() -> None:
    try:
        code, ctype, body = call("GET", "/api/state")
        if code == 200:
            mirror("/api/state", body, ctype, heartbeat=True)
    except OSError:
        pass  # the app is not running: the hosted page shows the last saved tree
    files = [ROOT / "web" / "lineage.json", *sorted((ROOT / "runs").glob("*-versions.jsonl"))]
    for f in files:
        if f.exists():
            key = "/lineage.json" if f.name == "lineage.json" else f"/runs/{f.name}"
            mirror(key, f.read_text(), "application/json" if f.suffix == ".json" else "text/plain")
    while job := rpc.find_one_and_update(
        {"status": "pending", "ts": {"$gt": time.time() * 1000 - 10_000}}, {"$set": {"status": "taken"}}, sort=[("ts", 1)]
    ):
        try:
            code, ctype, body = call(job["method"], job["path"], job.get("body"))
        except OSError as e:
            code, ctype, body = 503, "application/json", json.dumps({"error": f"the local app is not running: {e}"})
        rpc.update_one({"_id": job["_id"]}, {"$set": {"status": "done", "code": code, "type": ctype, "body": body}})
        print(f"{job['method']} {job['path']} -> {code}", flush=True)


if __name__ == "__main__":
    print(f"bridge: {APP} <-> Atlas bonsai.live / bonsai.rpc", flush=True)
    while True:
        t = time.time()
        try:
            tick()
        except Exception as e:  # keep bridging through a dropped connection
            print(f"bridge: {type(e).__name__}: {str(e)[:200]}", flush=True)
            time.sleep(2)
        time.sleep(max(0.0, 0.5 - (time.time() - t)))
