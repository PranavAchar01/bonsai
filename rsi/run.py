"""The RSI generation driver: play the champion on the ladder, mine its mistakes, propose, exam, keep or prune.

  python -m rsi.run --run ID [--dry] [--generations N] [--games N] [--workers N] [--seed-only]

Everything lives in runs/rsi/<ID>/:
  run.json        the frozen config (rsi/run.json + overrides), hashed into state.json; a change needs a new ID
  state.json      phase checkpoint (atomic): generation, phase, champion, rating state, children, pruned list
  events.jsonl    one line per thing that happened (phase_start/phase_end for every phase, games, proposals, ...)
  versions.jsonl  loop.py-shaped docs (+ elo, CI, games, exam, ...): point BONSAI_LOG at it to view on the page
  versions/<ID>-<vid>.json   every harness, written before it is scored
  games/<vid>/<slot>.json    game records (rsi.game); replays in tasks/chess/_replays/<ID>-<vid>/
  bank.jsonl, train_bank.jsonl, anchor.jsonl, evalcache.json, digests/, exam/ (answer caches)

Generation 0 rates the seed: bootstrap doubling from E100 (100, 200, 400, ...) while it scores >= 0.9 over >= 8
games, then N games at the estimate. No proposals. Each later generation runs the phases
  gate -> play -> rate -> mine -> propose -> prescreen -> exam -> store
and checkpoints after each one. LLM work is never bought twice: finished games are read back from their records,
proposals are saved the moment they return, exam answers are appended to exam/*.jsonl as they arrive.

Exit codes: 0 done (or a budget cap hit), 75 pause and retry later (gate failed, usage limit, too many void
games), 77 stop (auth failure, config changed). rsi.supervise acts on them.

--dry: no model calls at all. The player is a ladder Bot whose strength is the harness's "temp" knob, and the
proposer is a stub that lowers temp (first child) or raises it (second child), so the whole loop runs offline.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import math
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import chess

from rsi import bank as B
from rsi import exam as X
from rsi import game as G
from rsi import mine as M
from rsi import perceive, rating as R, reliable
from rsi.ladder import LADDER_JSON, STOCKFISH, Bot
from rsi.supervise import ancestors

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs" / "rsi"
EXIT_RETRY, EXIT_FATAL = 75, 77
PHASES = ("gate", "play", "rate", "mine", "propose", "prescreen", "exam", "store")
SEED_PHASES = ("gate", "play", "rate", "mine", "exam", "store")
DRY = {"seed_temp": 4.0, "lower": 0.6, "raise": 1.5, "nodes": 20000, "anchor_size": 40, "max_bootstrap_rounds": 4}


class Pause(Exception):
    """Stop this process with an exit code (75 = retry later, 77 = fatal); the event log says why."""

    def __init__(self, reason: str, code: int = EXIT_RETRY, reset_at: float | None = None):
        super().__init__(reason)
        self.reason, self.code, self.reset_at = reason, code, reset_at


class BudgetHit(Exception):
    pass


def sha1(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


def phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


# ------------------------------------------------------------------------------------------------
# Dry mode: a Bot player with a temperature knob and a stub proposer (no model calls)
# ------------------------------------------------------------------------------------------------

class DryPlayer:
    """choose(board, harness, ctx) with a depth-1 softmax Bot at harness["temp"]; one Bot per thread and temp."""

    def __init__(self, engine_path: str = STOCKFISH):
        self.engine_path, self.free, self.all, self.lock = engine_path, {}, [], threading.Lock()

    def take(self, temp: float) -> Bot:
        with self.lock:
            if self.free.get(temp):
                return self.free[temp].pop()
        spec = {"kind": "policy", "temp": temp, "depth": 1} if temp > 0 else {"kind": "policy", "depth": 1}
        b = Bot(spec, seed="dry", engine_path=self.engine_path)
        with self.lock:
            self.all.append(b)
        return b

    def give(self, temp: float, b: Bot) -> None:
        with self.lock:
            self.free.setdefault(temp, []).append(b)

    def __call__(self, board: chess.Board, harness: dict, ctx: dict | None = None) -> dict:
        ctx = ctx or {}
        slow = float(os.environ.get("RSI_DRY_SLOW") or 0)
        if slow and ctx.get("purpose") in ("exam", "prescreen"):
            time.sleep(slow)
        temp = float(harness.get("temp", DRY["seed_temp"]))
        b = self.take(temp)
        try:
            b.seed = f"{ctx.get('game') or ctx.get('pos_id')}|{ctx.get('arm', '')}"
            mv = b.move(board)
        finally:
            self.give(temp, b)
        return {"move": mv, "uci": mv.uci(), "reason": "dry bot", "attempts": [{"kind": "ok", "move": mv.uci()}],
                "calls": 1, "cost": 0.0, "infra": False, "think_tokens": 0, "latency": 0.0, "model": "dry-bot"}

    def close(self) -> None:
        for b in self.all:
            b.close()


def stub_propose(parent: dict, digest_text: str, *, gen: int, stats=None, pruned=(), ladder: str = "",
                 index: int = 0, **_) -> dict:
    """Dry proposer: child 0 lowers temp (stronger), child 1 raises it (weaker). Same return shape as propose()."""
    t = float(parent.get("temp", DRY["seed_temp"]))
    new = round(t * (DRY["lower"] if index % 2 == 0 else DRY["raise"]), 3)
    taken = {p.get("sig") for p in pruned}
    while f"temp:{new}" in taken:
        new = round(new * 0.9, 3)
    change = {"field": "temp", "op": "set", "temp": new, "rationale": "dry run: change the bot temperature",
              "expected_effect": {"motif": "mean_loss", "from": 0, "to": 0}}
    child = copy.deepcopy(parent)
    child.pop("id", None)
    child.update({"temp": new, "parent": parent.get("id"), "change": f"temp {t:g} -> {new:g}"})
    return {"status": "ok", "change": change, "child": child, "sig": f"temp:{new}", "describe": f"temp {t:g} -> {new:g}",
            "removal_only": False, "cost": 0.0, "model": "dry-stub", "attempts": 1}


# ------------------------------------------------------------------------------------------------
# The run
# ------------------------------------------------------------------------------------------------

class Run:
    def __init__(self, run_id: str, *, root: Path | str = RUNS, dry: bool = False, games: int | None = None,
                 workers: int | None = None, cfg_path: Path | str = reliable.RUN_JSON, replays: Path | str | None = None,
                 overrides: dict | None = None, choose=None, propose_fn=None, engine_path: str = STOCKFISH):
        self.id, self.dir = run_id, Path(root) / run_id
        self.gen, self.phase = None, None
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path, self.events_path = self.dir / "state.json", self.dir / "events.jsonl"
        self.versions_path = self.dir / "versions.jsonl"
        self.replays = G.REPLAYS if replays is None else replays
        self.engine_path = engine_path
        self.cfg = self._config(cfg_path, dry, games, overrides)
        self.dry = bool(self.cfg.get("dry"))
        self.state = self._state()
        self.workers_opt = workers
        self.workers = workers or self.cfg.get("workers", 3)
        self._dry_player = None
        if choose is None and self.dry:
            self._dry_player = choose = DryPlayer(engine_path)
        self.choose = choose or self._real_choose
        self.propose_fn = propose_fn or (stub_propose if self.dry else self._real_propose)
        self._scorer = None

    # ---------------- config, state, events

    def _config(self, cfg_path, dry, games, overrides) -> dict:
        frozen = self.dir / "run.json"
        if frozen.exists():
            cfg = json.loads(frozen.read_text())
            if games and games != cfg["games"]["per_gen"]:
                print(f"run.py: --games {games} ignored, this run is frozen at {cfg['games']['per_gen']}", file=sys.stderr)
            return cfg
        cfg = reliable.run_config(cfg_path)
        cfg["run_id"] = self.id
        cfg["ladder_sha"] = hashlib.sha1(Path(LADDER_JSON).read_bytes()).hexdigest()
        if games:
            cfg["games"]["per_gen"] = games
            cfg["games"]["held"] = round(games / 3)
            cfg["games"]["train"] = games - cfg["games"]["held"]
        if dry:
            cfg["dry"] = dict(DRY)
            cfg["mine"]["nodes"] = DRY["nodes"]
            cfg["bank"]["anchor_size"] = DRY["anchor_size"]
            cfg["bootstrap"]["max_rounds"] = DRY["max_bootstrap_rounds"]
            cfg["games"]["seed_rating_games"] = min(cfg["games"]["seed_rating_games"], 2 * cfg["games"]["per_gen"])
            cfg["player"] = {"model": "dry-bot", "alias": "dry-bot"}
            cfg["proposer"] = {**cfg["proposer"], "model": "dry-stub"}
        for path, value in (overrides or {}).items():  # {"games.move_cap": 30, ...}
            node = cfg
            *head, last = path.split(".")
            for k in head:
                node = node.setdefault(k, {})
            node[last] = value
        reliable.atomic_write_json(frozen, cfg, indent=1)
        return cfg

    def _state(self) -> dict:
        sha = hashlib.sha1((self.dir / "run.json").read_bytes()).hexdigest()
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text())
            if st.get("cfg_sha") != sha:
                raise Pause(f"runs/rsi/{self.id}/run.json changed since the run started: use a new RUN_ID", EXIT_FATAL)
            return st
        st = {"run": self.id, "cfg_sha": sha, "created": time.time(), "started": time.time(), "gen": 0, "phase": "gate",
              "champion": "g0", "harnesses": {"g0": self._seed_harness()}, "rating": None, "games": [], "gens": {},
              "pruned": [], "requeue": [], "docs": {}, "order": [], "motif_history": [], "swapped_at": None,
              "proposer_resolved": None, "canary_usd": 0.0}
        reliable.atomic_write_json(self.state_path, st, indent=1)
        self.event("run_created", dry=bool(self.cfg.get("dry")), cfg_sha=sha)
        return st

    def _seed_harness(self) -> dict:
        if self.cfg.get("dry"):
            return {"id": "g0", "parent": None, "model": "dry-bot", "rules": "(dry run: a ladder Bot plays)",
                    "lessons": [], "tools": [], "think": 0, "temp": self.cfg["dry"]["seed_temp"]}
        from rsi import player
        return player.seed_harness(self.cfg)

    def save(self) -> None:
        reliable.atomic_write_json(self.state_path, self.state, indent=1)

    def event(self, kind: str, **d) -> None:
        reliable.append_jsonl(self.events_path, {"t": round(time.time(), 3), "run": self.id, "gen": self.gen,
                                                 "phase": self.phase, "kind": kind, **d})

    def gs(self, g: int) -> dict:
        return self.state["gens"].setdefault(str(g), {})

    # ---------------- helpers

    @property
    def champion(self) -> dict:
        return self.state["harnesses"][self.state["champion"]]

    def lineage(self, vid: str | None = None) -> list[str]:
        out, v = [], vid or self.state["champion"]
        while v:
            out.append(v)
            v = self.state["harnesses"].get(v, {}).get("parent")
        return out

    def history(self, vid: str | None = None) -> list[dict]:
        """Complete games of the champion and its kept ancestors, oldest first (the rating's input)."""
        keep = set(self.lineage(vid))
        return [g for g in self.state["games"] if g["vid"] in keep and g["status"] == "complete"]

    def estimate(self, vid: str | None = None) -> dict:
        return R.estimate(self.history(vid), self.cfg["games"]["rating_window"])

    @property
    def scorer(self) -> B.Scorer:
        if self._scorer is None:
            self._scorer = B.Scorer(self.dir / "evalcache.json", nodes=self.cfg["mine"]["nodes"])
        return self._scorer

    def spend(self) -> float:
        """Dollars spent so far, recomputed from files (games, exam answers) plus proposals in state."""
        usd = self.state.get("canary_usd", 0.0)
        for f in (self.dir / "games").glob("*/*.json"):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if f.name.endswith(".state.json"):
                if not f.with_name(f.name.replace(".state.json", ".json")).exists():
                    usd += sum(p.get("cost") or 0 for p in rec.get("per_move") or [])
            else:
                usd += rec.get("cost_usd") or 0
        for f in (self.dir / "exam").glob("*.jsonl"):
            usd += sum((r.get("ans") or {}).get("cost") or 0 for r in reliable.read_jsonl(f))
        for gs in self.state["gens"].values():
            usd += sum(c.get("cost") or 0 for c in gs.get("children", []))
        return round(usd, 4)

    def check_budget(self) -> None:
        usd, hours = self.spend(), (time.time() - self.state["started"]) / 3600
        if usd >= self.cfg.get("max_usd", 1e9) or hours >= self.cfg.get("max_hours", 1e9):
            raise BudgetHit(f"budget reached: ${usd:.2f} of {self.cfg.get('max_usd')}, "
                            f"{hours:.2f} h of {self.cfg.get('max_hours')}")

    def write_version(self, harness: dict) -> None:
        reliable.atomic_write_json(self.dir / "versions" / f"{self.id}-{harness['id']}.json", harness, indent=1)

    # ---------------- real player / proposer

    def _infra_log(self, ev: dict) -> None:
        self.event("call_infra", **{k: v for k, v in ev.items() if k != "kind"})

    def _real_choose(self, board, harness, ctx):
        from rsi import player
        return player.choose(board, harness, ctx, cli=self.cfg["cli"]["path"], log=self._infra_log)

    def _real_propose(self, parent, digest_text, *, gen, stats=None, pruned=(), ladder="", index=0):
        from rsi import propose
        return propose.propose(parent, digest_text, gen=gen, stats=stats, pruned=pruned, ladder=ladder,
                               model=self.cfg["proposer"]["model"], log=self._infra_log)

    # ---------------- phase: gate

    def gate(self, g: int) -> None:
        """Doctor-lite: Stockfish answers, one tiny model call works, budget not hit. Failure -> exit 75."""
        from rsi import doctor
        self.check_budget()
        ok, detail = doctor.stockfish_check(self.engine_path)
        if not ok:
            raise Pause(f"stockfish: {detail}")
        info = {"stockfish": detail}
        if not self.dry:
            c = doctor.canary(self.cfg["player"]["model"], self.cfg["cli"]["path"])
            self.state["canary_usd"] = round(self.state.get("canary_usd", 0.0) + (c.get("cost") or 0.0), 5)
            if not c["ok"]:
                raise Pause(f"canary: {c['error']}", EXIT_FATAL if c["kind"] == "auth" else EXIT_RETRY, c.get("reset_at"))
            info["canary"] = {"model": c["model"], "wall_s": c["wall_s"]}
            if self.workers_opt is None:
                self.workers, why = doctor.workers_for(self.cfg)
                info["server"] = why
        info["workers"] = self.workers
        info["spend_usd"] = self.spend()
        self.gs(g)["gate"] = info

    # ---------------- phase: play

    def make_slots(self, g: int, targets: list[float], centre: float, tag: str = "") -> list[dict]:
        """Slots for one batch: every third game on a held family, both colours per family, seeded per slot."""
        train, held = G.families("train"), G.families("held")
        slots, k = [], {"train": 0, "held": 0}
        for i, t in enumerate(targets):
            split = "held" if i % 3 == 2 else "train"
            fams, j = (held if split == "held" else train), k[split]
            k[split] += 1
            sid = f"g{g}{tag}-s{i:02d}"
            slots.append({"id": sid, "seed": sha1(self.id, g, sid)[:10], "gen": g, "split": split,
                          "family": fams[(j // 2 + 5 * g) % len(fams)], "color": "white" if j % 2 == 0 else "black",
                          "target": centre, "elo": round(t), "opp": R.rung_for_elo(t)})
        return slots

    def _game_log(self, ev: dict) -> None:
        if ev.get("kind") == "move" and ev.get("ply", 0) % 20:
            return  # a heartbeat every 20 plies keeps the stall watchdog fed without flooding the log
        if ev.get("kind") in ("move", "resume"):
            self.event("ply" if ev["kind"] == "move" else "game_resume", game=ev.get("game"), ply=ev.get("ply"))

    def play(self, g: int, slots: list[dict], harness: dict) -> list[dict]:
        """Play every slot (resuming checkpoints, skipping finished ones). Infra games are replayed with the same
        seed up to infra_replays times, then dropped. Returns the records of the slots that ended."""
        out = self.dir / "games" / harness["id"]
        replays = self.cfg["games"]["infra_replays"]
        halt = threading.Event()

        def one(slot):
            done = out / f"{G.safe(slot['id'])}.json"
            if done.exists():
                try:
                    rec = json.loads(done.read_text())
                    if rec.get("status") == "complete":
                        return rec
                except ValueError:
                    pass
            rec = None
            for attempt in range(1 + replays):
                if halt.is_set():
                    break
                rec = G.play_game(harness, slot, out, self.id, choose=self.choose, replays_dir=self.replays,
                                  max_moves=self.cfg["games"]["move_cap"], time_cap=self.cfg["games"]["time_cap_s"],
                                  engine_path=self.engine_path, log=self._game_log)
                if rec["status"] == "complete":
                    break
                self.event("game_infra", slot=slot["id"], attempt=attempt + 1, infra_kind=rec.get("infra_kind"),
                           detail=(rec.get("infra") or "")[:200], reset_at=rec.get("reset_at"))
                if rec.get("infra_kind") in ("usage_limit", "auth", "stopped"):
                    halt.set()
                    break
            if rec is not None:
                self.event("game_end", slot=slot["id"], vid=harness["id"], status=rec["status"], score=rec["score"],
                           how=rec["how"], opp_elo=rec["opp_elo"], plies=rec["plies"], cost=rec["cost_usd"])
            return rec

        recs = []
        with ThreadPoolExecutor(max(1, self.workers)) as ex:
            pending = {ex.submit(one, s) for s in slots}
            while pending:
                finished, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in finished:
                    if f.result() is not None:
                        recs.append(f.result())
        order = {s["id"]: i for i, s in enumerate(slots)}
        recs.sort(key=lambda r: order.get(r["slot"], 0))
        self._record_games(harness["id"], recs)
        bad = [r for r in recs if r["status"] != "complete"]
        auth = next((r for r in bad if r.get("infra_kind") == "auth"), None)
        if auth:
            raise Pause(f"auth failure: {auth.get('infra')}", EXIT_FATAL)
        limit = next((r for r in bad if r.get("infra_kind") == "usage_limit"), None)
        if limit:
            raise Pause(f"usage limit: {limit.get('infra')}", EXIT_RETRY, limit.get("reset_at"))
        void = len(slots) - sum(r["status"] == "complete" for r in recs)
        streak = run = 0
        for s in slots:
            r = next((x for x in recs if x["slot"] == s["id"]), None)
            run = run + 1 if r is None or r["status"] != "complete" else 0
            streak = max(streak, run)
        if void > self.cfg["games"]["void_frac_max"] * len(slots) or streak >= self.cfg["games"]["infra_streak_max"]:
            raise Pause(f"{void}/{len(slots)} games void (longest infra streak {streak}): no proposer call")
        return [r for r in recs if r["status"] == "complete"]

    def _record_games(self, vid: str, recs: list[dict]) -> None:
        have = {(x["vid"], x["slot"]) for x in self.state["games"]}
        for r in recs:
            row = {"vid": vid, "slot": r["slot"], "gen": r["gen"], "status": r["status"], "score": r["score"],
                   "opp_elo": r["opp_elo"], "target": r["target"], "split": r["split"], "game": r["game"],
                   "cost": r["cost_usd"], "calls": r["calls"], "how": r["how"], "plies": r["plies"]}
            if (vid, r["slot"]) in have:
                self.state["games"] = [row if (x["vid"], x["slot"]) == (vid, r["slot"]) else x
                                       for x in self.state["games"]]
            else:
                self.state["games"].append(row)

    def records(self, vid: str, g: int) -> list[dict]:
        out = []
        for row in self.state["games"]:
            if row["vid"] == vid and row["gen"] == g and row["status"] == "complete":
                out.append(json.loads((self.dir / "games" / vid / f"{G.safe(row['slot'])}.json").read_text()))
        return out

    def play_seed(self) -> None:
        """Generation 0: bootstrap rounds at doubling targets from E100, then seed_rating_games at the estimate."""
        gs, n, h = self.gs(0), self.cfg["games"]["per_gen"], self.champion
        rounds = gs.setdefault("rounds", [])
        max_rounds = self.cfg["bootstrap"].get("max_rounds", 8)
        while True:
            if rounds and not rounds[-1].get("done"):
                rd = rounds[-1]
                self.play(0, rd["slots"], h)
                rd["done"] = True
                self.save()
                continue
            nt = R.next_target(self.history(), self.state["rating"], n, self.cfg["games"]["rating_window"])
            if nt["bootstrap"] and sum(r["bootstrap"] for r in rounds) >= max_rounds:
                est = nt["estimate"]
                nt = {**nt, "bootstrap": False, "event": "bootstrap_capped",
                      "target": float(est["elo"] if est["elo"] is not None else nt["target"])}
                nt["slots"] = R.slot_targets(nt["target"], n)
            if nt["event"]:
                self.event(nt["event"], target=nt["target"], elo=nt["estimate"]["elo"], lo=nt["estimate"]["lo"],
                           hi=nt["estimate"]["hi"], n=nt["estimate"]["n"])
            self.state["rating"] = {"target": nt["target"], "bootstrap": nt["bootstrap"]}
            rated = {s["id"] for r in rounds if not r["bootstrap"] for s in r["slots"]}
            after = sum(1 for x in self.state["games"] if x["gen"] == 0 and x["slot"] in rated and x["status"] == "complete")
            if not nt["bootstrap"] and after >= self.cfg["games"]["seed_rating_games"]:
                self.save()
                return
            k = len(rounds)
            rounds.append({"round": k, "target": nt["target"], "bootstrap": nt["bootstrap"],
                           "slots": self.make_slots(0, nt["slots"], nt["target"], tag=f"r{k}")})
            self.event("round", round=k, target=nt["target"], bootstrap=nt["bootstrap"], games=n)
            self.save()

    def play_gen(self, g: int) -> None:
        gs = self.gs(g)
        if "slots" not in gs:
            n = self.cfg["games"]["per_gen"]
            nt = R.next_target(self.history(), self.state["rating"], n, self.cfg["games"]["rating_window"])
            if nt["event"]:
                self.event(nt["event"], target=nt["target"])
            self.state["rating"] = {"target": nt["target"], "bootstrap": nt["bootstrap"]}
            gs["target"] = nt["target"]
            gs["slots"] = self.make_slots(g, nt["slots"], nt["target"])
            gs["player"] = self.state["champion"]
            self.save()
        self.play(g, gs["slots"], self.state["harnesses"][gs["player"]])

    # ---------------- phase: rate

    def rate(self, g: int) -> None:
        gs, est = self.gs(g), self.estimate()
        miles = R.milestones(self.history(), window=self.cfg["games"]["rating_window"],
                             min_games=self.cfg["milestone"]["min_games_at_or_above"])
        new = [m for m in miles if m not in self.state.get("milestones", [])]
        self.state["milestones"] = sorted(set(self.state.get("milestones", [])) | set(miles))
        for m in new:
            self.event("milestone", rung=m, elo=est["elo"], lo=est["lo"])
        gs["rating"] = {"elo": est["elo"], "lo": est["lo"], "hi": est["hi"], "n": est["n"], "score": est.get("score"),
                        "target": (self.state["rating"] or {}).get("target"), "milestones": miles}
        self.event("rating", **gs["rating"])
        if g >= 1 and self.state.get("swapped_at") == g - 1:
            self.rollback_check(g)

    def rollback_check(self, g: int) -> None:
        """After a swap: P(new champion worse than its parent on ladder games) >= p_worse -> restore the parent."""
        c = self.state["champion"]
        p = self.state["harnesses"][c].get("parent")
        cg = [x for x in self.state["games"] if x["vid"] == c and x["status"] == "complete"]
        pg = [x for x in self.history(p) if x["vid"] != c][-self.cfg["games"]["rating_window"]:]
        ec, ep = R.estimate(cg), R.estimate(pg)
        if ec["elo"] is None or ep["elo"] is None or ec["lo"] is None or ep["lo"] is None:
            return
        se = math.hypot((ec["hi"] - ec["lo"]) / 3.92, (ep["hi"] - ep["lo"]) / 3.92) or 1.0
        p_worse = phi((ep["elo"] - ec["elo"]) / se)
        self.gs(g)["rollback_check"] = {"child": c, "parent": p, "child_elo": ec["elo"], "parent_elo": ep["elo"],
                                        "p_worse": round(p_worse, 3)}
        if p_worse >= self.cfg["rollback"]["p_worse"]:
            h = self.state["harnesses"][c]
            self.state["pruned"].append({"change": h.get("change"), "sig": h.get("sig"), "parent": p,
                                         "exam": {"n": len(cg), "gain": ec["elo"] - ep["elo"], "p": round(1 - p_worse, 3)},
                                         "stage": "rollback"})
            self.state["champion"], self.state["swapped_at"] = p, None
            doc = self.state["docs"].get(c)
            if doc:
                doc.update({"kept": False, "rolled_back": True, "signal": f"rolled back: P(worse) {p_worse:.2f}"})
            self.gs(g)["rolled_back"] = c
            self.event("rollback", child=c, parent=p, p_worse=round(p_worse, 3))

    # ---------------- phase: mine

    def mine(self, g: int) -> None:
        gs, vid = self.gs(g), self.gs(g).get("player", "g0")
        recs = self.records(vid, g)
        train = [r for r in recs if r.get("split") == "train"]
        held = [r for r in recs if r.get("split") == "held"]
        engine = M.Engine(self.cfg["mine"]["nodes"], path=self.engine_path)
        try:
            analysed = []
            for r in train:
                analysed.append(M.evaluate_game(r, engine))
                self.event("mined", game=r["game"], moves=len(analysed[-1]["moves"]))
            self._train_bank(analysed, g)
            added = B.add_held_games(held, self.dir / "bank.jsonl", g, scorer=self.scorer, engine=engine)
        finally:
            engine.close()
        hist = self.state["motif_history"]
        text, stats = M.digest(analysed, self.champion, history=hist, pruned=self.state["pruned"][-12:],
                               knobs=self.knobs(recs), title=f"{self.id} generation {g}") if analysed else ("", {})
        (self.dir / "digests").mkdir(exist_ok=True)
        (self.dir / "digests" / f"g{g}.md").write_text(text)
        if stats:
            self.state["motif_history"] = [{"gen": g, "motifs": stats["motifs"]}] + [h for h in hist if h["gen"] != g][:4]
        gs["mine"] = {"train_games": len(analysed), "held_games": len(held), "bank_added": added,
                      "stats": {k: stats.get(k) for k in ("agent_moves", "mean_loss", "blunders", "mistakes",
                                                           "rejected_per100", "digest_tokens_est")} if stats else {},
                      "motifs": stats.get("motifs", {}) if stats else {}}
        anchor = self.dir / "anchor.jsonl"
        size = self.cfg["bank"]["anchor_size"]
        if not anchor.exists() and len(reliable.read_jsonl(self.dir / "bank.jsonl")) >= size:
            B.freeze_anchor(self.dir / "bank.jsonl", anchor, size)
            self.event("anchor_frozen", n=size)
        self.scorer.save()
        self.event("digest", tokens=(stats or {}).get("digest_tokens_est"), bank_added=added)

    def _train_bank(self, analysed: list[dict], g: int) -> None:
        path = self.dir / "train_bank.jsonl"
        seen = {p["id"] for p in reliable.read_jsonl(path)}
        for a in analysed:
            for m in a["moves"]:
                b = chess.Board(m["fen"])
                pov = 1 if b.turn == chess.WHITE else -1
                self.scorer.cache.setdefault(m["fen"], pov * m["cp_before"])
                pid = B.pos_id(m["fen"])
                if m["loss"] >= B.MIN_LOSS and pid not in seen:
                    seen.add(pid)
                    reliable.append_jsonl(path, {"id": pid, "fen": m["fen"], "history": m["history"], "mine": a["color"],
                                                 "src": f"{a['game']}:{m['ply']}", "family": a.get("family"),
                                                 "opp_elo": a.get("opp_elo"), "gen": g, "game_loss": m["loss"],
                                                 "best": m["best_uci"], "motifs": m["motifs"]})
        self.scorer.dirty += 1

    def knobs(self, recs: list[dict]) -> dict:
        moves = [p for r in recs for p in r.get("per_move") or []]
        if not moves:
            return {}
        think = self.champion.get("think", 0)
        wall = sum(p.get("wall_s") or 0 for p in moves) / len(moves)
        cost = sum(p.get("cost") or 0 for p in moves) / len(moves)
        return {f"think {think} (current)": f"{wall:.1f} s and ${cost:.4f} per move"}

    def ladder_text(self) -> str:
        e = self.estimate()
        return (f"Champion {self.state['champion']}: {e['elo']} ({e['lo']}..{e['hi']}, 95% CI) over {e['n']} games; "
                f"opponents now near {round((self.state['rating'] or {}).get('target') or 0)}. "
                f"Scale: {self.cfg.get('scale', '')}")

    # ---------------- phase: propose

    def propose(self, g: int) -> None:
        gs, champ = self.gs(g), self.champion
        if gs.get("rolled_back"):
            self.event("skip", why="rolled back this generation: the parent plays again first")
            return
        kids = gs.setdefault("children", [])
        have = {c["vid"] for c in kids}
        for c in list(self.state["requeue"]):  # inconclusive last time: re-examined without a new proposer call
            if c["harness"].get("parent") == champ["id"] and c["vid"] not in have:
                kids.append({**c, "requeued": c.get("requeued", 0) + 1, "status": "ok"})
                have.add(c["vid"])
                self.event("requeued", vid=c["vid"])
        self.state["requeue"] = []
        self.save()
        digest_text = (self.dir / "digests" / f"g{g}.md").read_text() if (self.dir / "digests" / f"g{g}.md").exists() else ""
        stats = {"motifs": gs.get("mine", {}).get("motifs", {})}
        want = self.cfg["proposer"]["children_per_gen"] - sum(1 for c in kids if c.get("requeued"))
        for i in range(max(0, want)):
            vid = f"g{g}{'abcdefgh'[i]}"
            if vid in have:
                continue
            res = self.propose_fn(champ, digest_text, gen=g, stats=stats, pruned=self.state["pruned"],
                                  ladder=self.ladder_text(), index=i)
            entry = {"vid": vid, "status": res["status"], "cost": res.get("cost") or 0.0, "model": res.get("model"),
                     "change": res.get("change"), "describe": res.get("describe"), "sig": res.get("sig"),
                     "removal_only": bool(res.get("removal_only")), "lint": res.get("lint"), "error": res.get("error")}
            if res["status"] == "ok":
                child = {**res["child"], "id": vid, "sig": res["sig"]}
                entry["harness"] = child
                self.state["harnesses"][vid] = child
                self.write_version(child)
                if res.get("model") and not self.state.get("proposer_resolved"):
                    self.state["proposer_resolved"] = res["model"]
            kids.append(entry)
            self.save()
            self.event("proposal", vid=vid, status=res["status"], describe=res.get("describe"), cost=entry["cost"],
                       lint=res.get("lint"))
            if res["status"] == "infra":
                break  # retried next generation; never stored as pruned
        every = self.cfg["every_n_gens"]["shed"]
        if g % every == 0 and not any(c.get("shed") for c in kids):
            self.add_shed(g, champ, kids)

    def add_shed(self, g: int, champ: dict, kids: list[dict]) -> None:
        """Every few generations: try removing the lesson whose target motif improved least since it was added."""
        lessons = champ.get("lessons") or []
        if not lessons:
            return
        motifs = self.gs(g).get("mine", {}).get("motifs", {})
        worst = max(lessons, key=lambda l: (motifs.get(l.get("motif")) or {}).get("per100", 0.0) - (l.get("rate_at_add") or 0))
        from rsi import propose
        change = {"field": "lesson", "op": "retire", "lesson_id": worst["id"]}
        vid = f"g{g}s"
        child = {**propose.apply(champ, change, g), "id": vid, "sig": propose.signature(change)}
        self.state["harnesses"][vid] = child
        self.write_version(child)
        kids.append({"vid": vid, "status": "ok", "cost": 0.0, "model": None, "change": change, "shed": True,
                     "describe": propose.describe(change), "sig": child["sig"], "removal_only": True, "harness": child})
        self.save()
        self.event("proposal", vid=vid, status="ok", describe=propose.describe(change), shed=True)

    # ---------------- phases: prescreen, exam

    def _cache(self, name: str):
        path = self.dir / "exam" / f"{name}.jsonl"
        cache = {(r["pid"], r["arm"]): r["ans"] for r in reliable.read_jsonl(path)}
        count = [len(cache)]

        def on_answer(pid, arm, ans):
            reliable.append_jsonl(path, {"pid": pid, "arm": arm, "ans": ans})
            count[0] += 1
            if count[0] % 10 == 0:
                self.event("answers", exam=name, n=count[0])
        return cache, on_answer

    def prescreen_positions(self, child: dict, n: int, seed: str) -> list[dict]:
        pool = reliable.read_jsonl(self.dir / "train_bank.jsonl")
        ch = child.get("change") or {}
        motif = ch.get("motif") or (ch.get("expected_effect") or {}).get("motif")
        rng = random.Random(seed)
        rng.shuffle(pool)
        tagged = [p for p in pool if motif and motif in (p.get("motifs") or [])] if ch.get("field") != "think" else []
        ids = {p["id"] for p in tagged}
        rest = sorted((p for p in pool if p["id"] not in ids), key=lambda p: -(p.get("game_loss") or 0))
        return (tagged + rest)[:n]

    def prescreen(self, g: int) -> None:
        n = self.cfg["prescreen"]["positions"]
        for c in self.gs(g).get("children", []):
            if c["status"] != "ok" or "prescreen" in c or c.get("removal_only"):
                continue
            pos = self.prescreen_positions(c, n, f"{self.id}|{c['vid']}|pre")
            if len(pos) < 4:
                c["prescreen"] = {"skipped": f"only {len(pos)} train-bank positions"}
                self.save()
                continue
            cache, on_answer = self._cache(f"{c['vid']}-prescreen")
            res = X.prescreen(pos, self.champion, c["harness"], self.choose, self.scorer, workers=self.workers,
                              seed=f"{self.id}|{c['vid']}", cache=cache, on_answer=on_answer)
            reliable.atomic_write_json(self.dir / "exam" / f"{c['vid']}-prescreen.result.json", res, indent=1)
            c["prescreen"] = {k: res[k] for k in ("kill", "p", "gain", "n", "void", "unusable_delta")}
            if res["kill"]:
                c["status"] = "killed"
                self.prune(c, res, "prescreen")
            self.save()
            self.event("prescreen", vid=c["vid"], **c["prescreen"])

    def prune(self, c: dict, res: dict, stage: str) -> None:
        self.state["pruned"].append({"change": c.get("describe"), "sig": c.get("sig"), "parent": c["harness"].get("parent"),
                                     "exam": {"n": res.get("n"), "gain": res.get("gain"), "p": res.get("p")},
                                     "stage": stage, "vid": c["vid"]})

    def exam_positions(self, seed: str) -> list[dict]:
        bank = reliable.read_jsonl(self.dir / "bank.jsonl")
        anchor = {p["id"] for p in reliable.read_jsonl(self.dir / "anchor.jsonl")}
        n = X.LOOKS[-1][0]
        fresh = [p for p in bank if p["id"] not in anchor]
        pool = fresh if len(fresh) >= X.LOOKS[0][0] else bank
        return B.select(pool, self.estimate()["elo"], n, seed=seed)

    def exam(self, g: int) -> None:
        gs = self.gs(g)
        kids = [c for c in gs.get("children", []) if c["status"] == "ok"]
        kids.sort(key=lambda c: (c.get("removal_only"), -((c.get("prescreen") or {}).get("gain") or 0)))
        for c in kids:
            if gs.get("winner"):
                if "exam" not in c:
                    c["status"] = "skipped"
                continue
            if "exam" not in c:
                pos = self.exam_positions(f"{self.id}|{c['vid']}|exam")
                cache, on_answer = self._cache(f"{c['vid']}-exam")
                self.event("exam_start", vid=c["vid"], positions=len(pos), shed=c.get("removal_only"))
                res = X.run_exam(pos, self.champion, c["harness"], self.choose, self.scorer,
                                 shed=bool(c.get("removal_only")), workers=self.workers, seed=f"{self.id}|{c['vid']}",
                                 cache=cache, on_answer=on_answer) if pos else \
                    {"decision": "inconclusive", "accept": False, "n": 0, "gain": 0.0, "p": 0.5, "void": 0, "looks": []}
                reliable.atomic_write_json(self.dir / "exam" / f"{c['vid']}-exam.result.json", res, indent=1)
                c["exam"] = {k: res.get(k) for k in ("decision", "n", "gain", "p", "void", "ci90", "looks", "calls",
                                                     "cost", "a", "b")}
                self.save()
                self.event("exam", vid=c["vid"], decision=res["decision"], n=res["n"], gain=res["gain"], p=res["p"])
            d = c["exam"]["decision"]
            if d == "accept":
                gs["winner"] = c["vid"]
                c["status"] = "accepted"
            elif d == "reject":
                c["status"] = "pruned"
                self.prune(c, c["exam"], "exam")
            else:
                c["status"] = "inconclusive"
                if c.get("requeued", 0) < 1:
                    self.state["requeue"].append({k: v for k, v in c.items() if k not in ("exam", "prescreen")})
            self.save()
        self.anchor_exam(g)

    def anchor_exam(self, g: int) -> None:
        """Every few generations (and once as soon as the anchor exists): the champion's mean loss on the frozen
        anchor positions, the progress curve's second line."""
        anchor = reliable.read_jsonl(self.dir / "anchor.jsonl")
        done = [x for x in self.state["gens"].values() if x.get("anchor")]
        if not anchor or (done and g % self.cfg["every_n_gens"]["anchor_exam"]):
            return
        vid = self.state["champion"]
        cache, on_answer = self._cache(f"anchor-{vid}")
        h = self.champion
        todo = [p for p in anchor if (p["id"], "a") not in cache]

        def one(p):
            ans = X.answer(p, h, self.choose, "a", "anchor")
            cache[(p["id"], "a")] = ans
            on_answer(p["id"], "a", ans)
        with ThreadPoolExecutor(max(1, self.workers)) as ex:
            list(ex.map(one, todo))
        losses = [self.scorer(p, cache[(p["id"], "a")]["move"]) for p in anchor if not cache[(p["id"], "a")]["infra"]]
        self.gs(g)["anchor"] = {"vid": vid, "n": len(losses), "void": len(anchor) - len(losses),
                                "mean_loss": round(sum(losses) / max(1, len(losses)), 2)}
        self.scorer.save()
        self.event("anchor_exam", **self.gs(g)["anchor"])

    # ---------------- phase: store

    def doc(self, vid: str, g: int, **extra) -> dict:
        h = self.state["harnesses"][vid]
        par = self.state["harnesses"].get(h.get("parent") or "")
        view = lambda x: {k: v for k, v in (x or {}).items() if k not in ("id", "parent", "change", "sig")}
        diff = "\n".join(difflib.unified_diff(json.dumps(view(par), indent=1).splitlines(),
                                              json.dumps(view(h), indent=1).splitlines(), h.get("parent") or "",
                                              vid, lineterm="")) if par else ""
        text = (h.get("rules") or "") + "".join(l.get("text", "") for l in h.get("lessons") or [])
        old = self.state["docs"].get(vid, {})
        d = {"id": vid, "parent": h.get("parent"), "generation": g, "change": h.get("change") or "seed harness",
             "shed": None, "signal": None, "harness": view(h), "kept": True, "score": None, "results": [],
             "tokens": len(text) // 4, "diff": diff, "trace_id": None, "ts": time.time(), "run": self.id,
             "elo": None, "elo_lo": None, "elo_hi": None, "games": 0, "target_elo": None, "exam": None,
             "prescreen": None, "think": h.get("think", 0), "tier": perceive.tier(h.get("tools") or []),
             "rolled_back": False, "model": h.get("model"), "file": f"versions/{self.id}-{vid}.json"}
        d.update({k: v for k, v in old.items() if k not in ("harness", "diff", "tokens")})
        d.update(extra)
        self.state["docs"][vid] = d
        if vid not in self.state["order"]:
            self.state["order"].append(vid)
        return d

    def champion_doc(self, vid: str, g: int, **extra) -> dict:
        mine = [x for x in self.state["games"] if x["vid"] == vid and x["status"] == "complete"]
        est = self.estimate(vid)
        results = [{"task": f"chess/{x['game']}", "passed": x["score"] == 1, "cheat": False, "score": x["score"],
                    "opp_elo": x["opp_elo"], "replay": f"{G.safe(self.id)}-{G.safe(vid)}/{x['game']}"} for x in mine[-24:]]
        return self.doc(vid, g, score=round(sum(x["score"] for x in mine) / len(mine), 3) if mine else None,
                        results=results, games=len(mine), elo=est["elo"], elo_lo=est["lo"], elo_hi=est["hi"],
                        target_elo=(self.state["rating"] or {}).get("target"),
                        cost_usd=round(sum(x["cost"] or 0 for x in mine), 4), **extra)

    def store(self, g: int) -> None:
        gs = self.gs(g)
        cur = gs.get("player", self.state["champion"])
        anchor = (gs.get("anchor") or {}).get("mean_loss")
        extra = {"anchor_loss": anchor} if anchor is not None else {}
        self.champion_doc(cur, self.state["docs"].get(cur, {}).get("generation", g), **extra)
        parent_doc = self.state["docs"].get(self.state["champion"], {})
        for c in gs.get("children", []):
            if c["status"] in ("lint_failed", "infra") or (c["status"] == "inconclusive" and c.get("requeued", 0) < 1
                                                          and any(r["vid"] == c["vid"] for r in self.state["requeue"])):
                continue
            ch = c.get("change") or {}
            ex = c.get("exam") or {}
            kept = c["status"] == "accepted"
            why = {"killed": "cut at prescreen", "pruned": "exam: not better", "skipped": "not examined: another child won",
                   "inconclusive": "exam inconclusive (void)"}.get(c["status"])
            self.doc(c["vid"], g, kept=kept, score=parent_doc.get("score"), shed=c["describe"] if c.get("removal_only") else None,
                     signal=(ch.get("expected_effect") or {}).get("motif") if kept else why,
                     exam={"n": ex.get("n"), "gain": ex.get("gain"), "p": ex.get("p"), "decision": ex.get("decision")} if ex else None,
                     prescreen=c.get("prescreen"), cost_usd=round((c.get("cost") or 0) + (ex.get("cost") or 0), 4),
                     rationale=ch.get("rationale"))
        if gs.get("winner"):
            self.state["champion"], self.state["swapped_at"] = gs["winner"], g
            self.event("swap", champion=gs["winner"])
        self.export()

    def export(self) -> None:
        """Rewrite versions.jsonl (atomically) from the docs: loop.py's shape plus the ladder fields."""
        lines = "".join(json.dumps(self.state["docs"][v]) + "\n" for v in self.state["order"])
        tmp = self.versions_path.with_name(self.versions_path.name + ".tmp")
        tmp.write_text(lines)
        os.replace(tmp, self.versions_path)

    # ---------------- driver

    def run_phase(self, g: int, phase: str, fn) -> None:
        self.gen, self.phase = g, phase
        if phase != "gate":
            self.check_budget()
        t0 = time.time()
        self.event("phase_start")
        fn(g)
        nxt = (SEED_PHASES if g == 0 else PHASES)
        i = nxt.index(phase)
        self.state["phase"] = nxt[i + 1] if i + 1 < len(nxt) else "gate"
        if phase == "store":
            self.state["gen"] = g + 1
        self.save()
        self.event("phase_end", wall_s=round(time.time() - t0, 2))

    def run(self, generations: int = 1, seed_only: bool = False) -> int:
        self.write_version(self.state["harnesses"]["g0"])
        fns = {"gate": self.gate, "play": lambda g: self.play_seed() if g == 0 else self.play_gen(g), "rate": self.rate,
               "mine": self.mine, "propose": self.propose, "prescreen": self.prescreen, "exam": self.exam,
               "store": lambda g: self.store_seed() if g == 0 else self.store(g)}
        last = 0 if seed_only else generations
        try:
            while self.state["gen"] <= last:
                g = self.state["gen"]
                self.gen = g
                phase = self.state["phase"]
                self.run_phase(g, phase, fns[phase])
            self.gen, self.phase = self.state["gen"] - 1, None
            self.event("done", champion=self.state["champion"], spend_usd=self.spend(), estimate=self.estimate())
            return 0
        except BudgetHit as e:
            self.event("budget_stop", reason=str(e))
            return 0
        except Pause as e:
            self.save()
            self.event("pause", reason=e.reason, code=e.code, reset_at=e.reset_at)
            print(f"run.py: pause ({e.code}): {e.reason}", file=sys.stderr)
            return e.code
        finally:
            self.close()

    def store_seed(self, g: int = 0) -> None:
        self.champion_doc("g0", 0, change="seed harness",
                          **({"anchor_loss": self.gs(0)["anchor"]["mean_loss"]} if self.gs(0).get("anchor") else {}))
        self.export()

    def close(self) -> None:
        if self._scorer is not None:
            self._scorer.close()
            self._scorer = None
        if self._dry_player is not None:
            self._dry_player.close()


def _lock(dry: bool, run_dir: Path) -> list[reliable.RunLock]:
    """One process per RUN_ID always; the machine-wide heavy lock for real runs, unless our supervisor (an
    ancestor: it may start us through `uv run`) holds it."""
    locks = [reliable.RunLock(run_dir / "run.lock", "rsi.run")]
    if not dry:
        heavy = reliable.RunLock(reliable.HEAVY_LOCK, f"rsi.run {run_dir.name}")
        holder = heavy.holder() or {}
        ours = os.environ.get("RSI_SUPERVISED") and holder.get("pid") in ancestors() and heavy.held_elsewhere()
        if not ours:
            locks.append(heavy)
    for l in locks:
        if not l.acquire():
            for x in locks:
                x.release()
            raise Pause(f"lock {l.path} is held by {l.holder()}", EXIT_RETRY)
    return locks


def hard_stop(run_dir: Path, signum: int) -> None:
    """SIGTERM/SIGINT: every write is atomic or append-only and every phase resumes from disk, so stopping is the
    same as being SIGKILLed, minus the orphans: kill our children (claude calls run in their own sessions) and go.
    Waiting for worker threads instead would block on a model call for up to a minute."""
    from rsi.supervise import kill_tree
    try:
        reliable.append_jsonl(run_dir / "events.jsonl", {"t": round(time.time(), 3), "run": run_dir.name,
                                                         "kind": "stopped", "signal": signum})
    finally:
        kill_tree(os.getpid(), include_root=False)
        os._exit(143)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, help="RUN_ID: runs/rsi/<RUN_ID>/")
    ap.add_argument("--dry", action="store_true", help="no model calls: a Bot player and a stub proposer")
    ap.add_argument("--generations", type=int, default=1, help="run until this many generations after the seed are done")
    ap.add_argument("--games", type=int, default=None, help="games per generation (frozen at the first start)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--seed-only", action="store_true", help="only rate the seed (generation 0), no proposals")
    ap.add_argument("--root", default=str(RUNS))
    ap.add_argument("--replays", default=str(G.REPLAYS), help="viewer replay folder (tasks/chess/_replays)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                    help="override a run.json value at the first start, e.g. --set games.move_cap=30")
    a = ap.parse_args(argv)
    overrides = {k: json.loads(v) for k, v in (x.split("=", 1) for x in a.set)}
    run_dir = Path(a.root) / a.run
    run_dir.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda s, f: hard_stop(run_dir, s))
    signal.signal(signal.SIGINT, lambda s, f: hard_stop(run_dir, s))
    try:
        locks = _lock(a.dry, run_dir)
    except Pause as e:
        print(f"run.py: {e.reason}", file=sys.stderr)
        return e.code
    try:
        r = Run(a.run, root=a.root, dry=a.dry, games=a.games, workers=a.workers, replays=a.replays, overrides=overrides)
        return r.run(a.generations, a.seed_only)
    except Pause as e:  # config changed
        print(f"run.py: {e.reason}", file=sys.stderr)
        return e.code
    finally:
        for l in locks:
            l.release()


if __name__ == "__main__":
    sys.exit(main())
