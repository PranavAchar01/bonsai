"""The generation driver end to end with no model calls (rsi.run --dry): a Bot player, a stub proposer, real
Stockfish for the opponents, mining and exam scoring (at low node counts so the suite stays quick)."""

import collections
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rsi import reliable
from rsi.player import InfraAbort
from rsi.run import EXIT_FATAL, EXIT_RETRY, PHASES, Pause, Run

ROOT = Path(__file__).resolve().parent.parent.parent
SMALL = {"games.move_cap": 30, "mine.nodes": 5000, "bank.anchor_size": 20, "bootstrap.max_rounds": 1,
         "games.seed_rating_games": 6}


def events(run_dir: Path) -> list[dict]:
    return reliable.read_jsonl(run_dir / "events.jsonl")


def make(tmp, run_id="t", games=6, **over):
    return Run(run_id, root=tmp / "runs", dry=True, games=games, workers=3, replays=tmp / "replays",
               overrides={**SMALL, **over})


def test_dry_loop_every_phase_and_loop_shaped_export(tmp_path):
    r = make(tmp_path)
    assert r.run(generations=2) == 0
    d = tmp_path / "runs" / "t"
    ev = events(d)
    for g in (1, 2):
        ended = {e["phase"] for e in ev if e["kind"] == "phase_end" and e["gen"] == g}
        assert ended == set(PHASES), (g, ended)
    assert ev[-1]["kind"] == "done"
    docs = [json.loads(l) for l in (d / "versions.jsonl").read_text().splitlines()]
    ids = [x["id"] for x in docs]
    assert len(ids) == len(set(ids)) and ids[0] == "g0"
    loop_keys = {"id", "parent", "generation", "change", "shed", "signal", "harness", "kept", "score", "results",
                 "tokens", "diff", "trace_id", "ts"}
    extras = {"run", "elo", "elo_lo", "elo_hi", "games", "target_elo", "exam", "prescreen", "think", "tier", "rolled_back"}
    for x in docs:
        assert loop_keys | extras <= set(x), set(loop_keys | extras) - set(x)
    seed = docs[0]
    assert seed["elo"] is not None and seed["elo_lo"] < seed["elo"] < seed["elo_hi"] and seed["games"] >= 12
    assert 0 <= seed["score"] <= 1 and seed["results"][0]["task"].startswith("chess/")
    kids = [x for x in docs if x["parent"]]
    assert kids and all(x["exam"] or x["prescreen"] for x in kids)
    for x in docs:  # every harness written to versions/<RUN_ID>-<vid>.json
        assert (d / "versions" / f"t-{x['id']}.json").exists()
    # replays are flat <RUN_ID>-<vid>/<family>-<color>-<hash>.json, servable by the viewer regex
    reps = list((tmp_path / "replays").glob("t-g0/*.json"))
    assert reps and all(p.stem.replace("-", "").replace("_", "").isalnum() for p in reps)
    st = json.loads((d / "state.json").read_text())
    assert st["gen"] == 3 and st["phase"] == "gate"
    # a finished run asked again does nothing new
    n = len(events(d))
    assert make(tmp_path).run(generations=2) == 0
    assert [e["kind"] for e in events(d)[n:]] == ["done"]


def test_bootstrap_doubles_when_the_player_always_wins(tmp_path):
    # a greedy depth-1 player (ladder ~1700) against E100 and then 200: scores >= 0.9 and the target doubles
    r = make(tmp_path, games=8, **{"dry.seed_temp": 0.0, "games.move_cap": 60, "bootstrap.max_rounds": 2,
                                   "games.seed_rating_games": 0})
    assert r.run(seed_only=True) == 0
    ev = events(tmp_path / "runs" / "t")
    rounds = [e["target"] for e in ev if e["kind"] == "round"]
    assert rounds[:2] == [100.0, 200.0]
    assert any(e["kind"] == "promote" and e["target"] == 200.0 for e in ev)
    assert not any(e["kind"] == "proposal" for e in ev)  # seed rating never proposes


def test_usage_limit_pauses_without_a_proposer_call(tmp_path):
    assert make(tmp_path).run(seed_only=True) == 0
    calls = []

    def choose(board, harness, ctx):
        raise InfraAbort("usage_limit", "usage limit reached", reset_at=time.time() + 3600, retryable=True)

    def propose(*a, **k):
        calls.append(1)
        raise AssertionError("the proposer must not be called")

    r = Run("t", root=tmp_path / "runs", dry=True, workers=3, replays=tmp_path / "replays", choose=choose,
            propose_fn=propose)
    assert r.run(generations=1) == EXIT_RETRY
    ev = events(tmp_path / "runs" / "t")
    pause = [e for e in ev if e["kind"] == "pause"][-1]
    assert pause["reset_at"] and "usage limit" in pause["reason"]
    assert not calls
    st = json.loads((tmp_path / "runs" / "t" / "state.json").read_text())
    assert st["gen"] == 1 and st["phase"] == "play"  # resumes at play, nothing bought is lost
    assert all(g["status"] == "complete" for g in st["games"] if g["gen"] == 0)


def test_budget_cap_stops_cleanly(tmp_path):
    assert make(tmp_path, **{"max_hours": 0}).run(generations=1) == 0
    ev = events(tmp_path / "runs" / "t")
    assert [e["kind"] for e in ev if e["kind"] in ("budget_stop", "game_end")] == ["budget_stop"]


def test_changed_config_needs_a_new_run_id(tmp_path):
    make(tmp_path)
    f = tmp_path / "runs" / "t" / "run.json"
    cfg = json.loads(f.read_text())
    cfg["max_usd"] = 999
    f.write_text(json.dumps(cfg))
    with pytest.raises(Pause) as e:
        make(tmp_path)
    assert e.value.code == EXIT_FATAL


def test_kill9_mid_exam_then_resume_buys_nothing_twice(tmp_path):
    root, reps = tmp_path / "runs", tmp_path / "replays"
    cmd = [sys.executable, "-m", "rsi.run", "--dry", "--run", "k", "--root", str(root), "--replays", str(reps),
           "--games", "6", "--generations", "1", "--workers", "3"]
    cmd += [x for k, v in SMALL.items() for x in ("--set", f"{k}={json.dumps(v)}")]
    cache = root / "k" / "exam" / "g1a-exam.jsonl"
    p = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, "RSI_DRY_SLOW": "0.05"}, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 240 and p.poll() is None:
        if cache.exists() and len(reliable.read_jsonl(cache)) >= 6:
            break
        time.sleep(0.05)
    assert p.poll() is None, "the run ended before the exam started"
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    before = len(reliable.read_jsonl(cache))
    assert json.loads((root / "k" / "state.json").read_text())["phase"] == "exam"

    rc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300).returncode
    assert rc == 0
    ev = events(root / "k")
    ends = collections.Counter(e["slot"] for e in ev if e["kind"] == "game_end")
    assert ends and max(ends.values()) == 1, "a finished game was played again"
    props = collections.Counter(e["vid"] for e in ev if e["kind"] == "proposal")
    assert props and max(props.values()) == 1, "the proposer was asked again"
    rows = reliable.read_jsonl(cache)
    keys = [(r["pid"], r["arm"]) for r in rows]
    assert len(keys) == len(set(keys)) and len(rows) >= before, "an exam answer was bought twice"
    ids = [json.loads(l)["id"] for l in (root / "k" / "versions.jsonl").read_text().splitlines()]
    assert len(ids) == len(set(ids))
    assert any(e["kind"] == "phase_end" and e["phase"] == "store" and e["gen"] == 1 for e in ev)


def test_real_mode_wiring_with_the_fake_cli(tmp_path):
    """Not --dry: player.choose, the canary gate and propose.propose all go through the (fake) CLI."""
    from rsi.tests.fake_claude import make_cli
    cli = make_cli(tmp_path)
    r = Run("real", root=tmp_path / "runs", games=6, workers=3, replays=tmp_path / "replays",
            overrides={**SMALL, "cli.path": cli, "games.seed_rating_games": 0, "games.move_cap": 15})
    assert r.run(generations=1) == 0
    ev = events(tmp_path / "runs" / "real")
    assert any(e["kind"] == "phase_end" and e["phase"] == "gate" and e["gen"] == 1 for e in ev)
    st = json.loads((tmp_path / "runs" / "real" / "state.json").read_text())
    assert st["gens"]["1"]["gate"]["canary"]["model"]
    assert st["canary_usd"] > 0
    games = [g for g in st["games"] if g["status"] == "complete"]
    assert games and all(g["calls"] > 0 for g in games)
    # the fake proposer returns a typed change: linted, prescreened / examined by fake haiku answers, then decided
    kids = st["gens"]["1"]["children"]
    assert kids and st["proposer_resolved"]
    assert all(c["status"] in ("accepted", "pruned", "killed", "skipped", "inconclusive") for c in kids)
    assert {p["vid"] for p in st["pruned"]} == {c["vid"] for c in kids if c["status"] in ("pruned", "killed")}
