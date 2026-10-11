"""rsi.supervise against a scripted child (crash, stall, pause, fatal), plus the doctor's offline checks."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from rsi import doctor, reliable
from rsi.supervise import pause_wait
from rsi.tests.fake_claude import make_cli

ROOT = Path(__file__).resolve().parent.parent.parent

CHILD = r'''
import json, os, subprocess, sys, time
d, marker = sys.argv[1], sys.argv[2]
modes = open(os.path.join(d, "modes")).read().split()
n = len(open(os.path.join(d, "starts")).read().split()) if os.path.exists(os.path.join(d, "starts")) else 0
open(os.path.join(d, "starts"), "a").write(f"{os.getpid()}\n")
mode = modes[min(n, len(modes) - 1)]
ev = os.path.join(d, "run", "events.jsonl")
def event(**k):
    open(ev, "a").write(json.dumps({"t": time.time(), **k}) + "\n")
event(kind="child_start", mode=mode)
if mode == "crash":
    sys.exit(1)
if mode == "fatal":
    sys.exit(77)
if mode.startswith("pause"):
    event(kind="pause", reason="usage limit", reset_at=time.time() + float(mode.split(":")[1]))
    sys.exit(75)
if mode.startswith("sleep"):
    time.sleep(float(mode.split(":")[1]))
if mode == "stall":
    # a grandchild in its own session, like a claude call: the supervisor must still kill it
    subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(600)  # {marker}"], start_new_session=True)
    time.sleep(600)
sys.exit(0)
'''


def supervise(tmp: Path, modes: str, *extra, timeout: float = 60):
    (tmp / "modes").write_text(modes)
    (tmp / "run").mkdir(exist_ok=True)
    script = tmp / "child.py"
    script.write_text(CHILD)
    marker = f"MARK_{uuid.uuid4().hex}"
    cmd = [sys.executable, "-m", "rsi.supervise", "--run", "run", "--root", str(tmp), "--lock", str(tmp / "heavy.lock"),
           "--stall", "2", "--poll", "0.2", "--backoff", "0.2,0.2,0.2", "--pause", "0.3", "--reset-margin", "0",
           *extra, "--", sys.executable, str(script), str(tmp), marker]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    log = reliable.read_jsonl(tmp / "run" / "supervise.jsonl")
    return p, [e["kind"] for e in log], log, marker


def alive(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


def test_crash_is_restarted_with_backoff(tmp_path):
    p, kinds, log, _ = supervise(tmp_path, "crash crash ok")
    assert p.returncode == 0, p.stderr
    assert kinds == ["start", "restart_wait", "start", "restart_wait", "start", "done"]
    assert [e["rc"] for e in log if e["kind"] == "restart_wait"] == [1, 1]


def test_stall_kills_the_whole_tree_then_restarts(tmp_path):
    p, kinds, log, marker = supervise(tmp_path, "stall ok")
    assert p.returncode == 0, p.stderr
    assert kinds == ["start", "stall", "restart_wait", "start", "done"]
    time.sleep(0.5)
    assert not alive(marker), "the grandchild in its own session survived the stall kill"
    first = int((tmp_path / "starts").read_text().split()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(first, 0)


def test_exit_75_sleeps_until_the_reset_then_retries(tmp_path):
    t0 = time.time()
    p, kinds, log, _ = supervise(tmp_path, "pause:1.5 ok")
    assert p.returncode == 0, p.stderr
    assert kinds == ["start", "pause", "start", "done"]
    pause = next(e for e in log if e["kind"] == "pause")
    assert 1.0 < pause["wait_s"] <= 1.6 and pause["reason"] == "usage limit"
    assert time.time() - t0 >= 1.4


def test_fatal_exit_stops(tmp_path):
    p, kinds, _, _ = supervise(tmp_path, "fatal ok")
    assert p.returncode == 77
    assert kinds == ["start", "fatal"]


def test_second_instance_is_refused(tmp_path):
    lock = reliable.RunLock(tmp_path / "heavy.lock", "test holder")
    assert lock.acquire()
    try:
        p, kinds, _, _ = supervise(tmp_path, "ok")
    finally:
        lock.release()
    assert p.returncode == 2 and kinds == ["refused"]
    assert not (tmp_path / "starts").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS only")
def test_caffeinate_lives_only_as_long_as_the_supervisor(tmp_path):
    (tmp_path / "modes").write_text("sleep:3 ok")
    (tmp_path / "run").mkdir()
    (tmp_path / "child.py").write_text(CHILD)
    cmd = [sys.executable, "-m", "rsi.supervise", "--run", "run", "--root", str(tmp_path), "--lock",
           str(tmp_path / "heavy.lock"), "--poll", "0.2", "--", sys.executable, str(tmp_path / "child.py"),
           str(tmp_path), "x"]
    sup = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pat = f"caffeinate -dimsu -w {sup.pid}"
    seen = False
    for _ in range(40):
        time.sleep(0.1)
        if subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0:
            seen = True
            break
    assert seen
    assert sup.wait(30) == 0
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode != 0


def test_pause_wait_falls_back_without_a_near_reset():
    now = 1000.0
    assert pause_wait({"reset_at": now + 100}, 600, now, margin=60) == 160
    assert pause_wait({"reset_at": now + 13 * 3600}, 600, now) == 600
    assert pause_wait(None, 600, now) == 600
    assert pause_wait({"reset_at": now - 5}, 600, now) == 600


def test_doctor_canary_and_cli_checks_against_the_fake(tmp_path):
    cli = make_cli(tmp_path)
    ok = doctor.canary("haiku", cli, timeout=30)
    assert ok["ok"] and ok["model"] == "claude-haiku-4-5-20251001"
    modes = tmp_path / "modes"
    modes.write_text("auth")
    bad = doctor.canary("haiku", cli, timeout=30, extra_env={"FAKE_CLAUDE_MODES": str(modes)})
    assert not bad["ok"] and bad["kind"] == "auth"
    rows = doctor.cli_check({"cli": {"path": str(tmp_path / "nope"), "version": "2.1.236"}})
    assert rows[0][0] == "FAIL"
    sf_ok, detail = doctor.stockfish_check()
    assert sf_ok, detail
    busy, why = doctor.server_busy("http://127.0.0.1:9/none")
    assert isinstance(busy, bool) and why
