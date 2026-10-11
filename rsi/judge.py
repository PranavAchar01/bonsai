"""Judge page: one static HTML file showing a run's climb up the Stockfish-anchored Elo ladder.

  uv run -q --no-project --with chess python -m rsi.judge --run judge1 --out web/judge.html

Reads runs/rsi/<run>/ (state.json, events.jsonl, games/*/*.json and *.state.json, versions.jsonl) and writes one
HTML file with the data embedded as JSON. No server; re-run any time to refresh (safe while the run is going).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import chess

from rsi import rating as R
from rsi.ladder import LADDER_JSON, load_ladder

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).with_name("judge_template.html")


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _replay(uci: list[str]) -> tuple[list[str], list[str]]:
    """SAN and board placement after every ply (placement[0] = start). Stops at the first illegal move."""
    b = chess.Board()
    san, fens = [], [b.board_fen()]
    for u in uci:
        try:
            m = chess.Move.from_uci(u)
            if m not in b.legal_moves:
                break
            san.append(b.san(m))
            b.push(m)
            fens.append(b.board_fen())
        except ValueError:
            break
    return san, fens


def _slots(state: dict) -> dict:
    out = {}
    for g in state.get("gens", {}).values():
        for s in g.get("slots", []) or []:
            out[s["id"]] = s
        for rd in g.get("rounds", []) or []:
            for s in rd.get("slots", []) or []:
                out[s["id"]] = s
    return out


def _est(games: list[dict], window: int) -> dict:
    e = R.estimate(games, window)
    return {k: (round(e[k]) if isinstance(e.get(k), (int, float)) else e.get(k)) for k in ("elo", "lo", "hi", "n")} | {
        "score": e.get("score")}


def collect(run_dir: Path) -> dict:
    state = _read(run_dir / "state.json", {}) or {}
    cfg = _read(run_dir / "run.json", {}) or {}
    window = (cfg.get("games") or {}).get("rating_window", R.WINDOW)
    events = _jsonl(run_dir / "events.jsonl")
    slots = _slots(state)
    harn = state.get("harnesses", {})
    now = time.time()
    done = any(e.get("kind") == "done" for e in events)

    games, usd, calls = [], float(state.get("canary_usd") or 0.0), 0
    for f in sorted((run_dir / "games").glob("*/*.json")):
        final = not f.name.endswith(".state.json")
        if not final and f.with_name(f.name.replace(".state.json", ".json")).exists():
            continue
        rec = _read(f)
        if not rec:
            continue
        slot_id = rec.get("slot") or f.name.split(".")[0]
        slot = slots.get(slot_id, {})
        opp = rec.get("opp") or slot.get("opp") or {}
        uci = rec.get("moves") or []
        san, fens = _replay(uci)
        pm = rec.get("per_move") or []
        if final:
            usd += rec.get("cost_usd") or 0
            calls += rec.get("calls") or 0
            status = rec.get("status") or "complete"
        else:
            usd += sum(p.get("cost") or 0 for p in pm)
            calls += sum(p.get("calls") or 0 for p in pm)
            status = "in_progress" if not done else "abandoned"
        how = rec.get("how")
        if rec.get("adjudication"):
            how = f"{how} (adjudicated)" if how else "adjudicated"
        if rec.get("forfeit"):
            how = f"forfeit: {rec['forfeit']}"
        games.append({
            "id": f"{f.parent.name}/{slot_id}", "vid": rec.get("harness") or f.parent.name, "slot": slot_id,
            "gen": rec.get("gen", slot.get("gen")), "status": status, "score": rec.get("score") if final else None,
            "result": rec.get("result"), "how": how, "infra": rec.get("infra_kind") or rec.get("infra"),
            "color": rec.get("color") or rec.get("agent_plays") or slot.get("color"),
            "family": rec.get("family") or slot.get("family"), "eco": rec.get("eco"),
            "split": rec.get("split") or slot.get("split"),
            "opp_name": opp.get("name"), "opp_elo": rec.get("opp_elo", opp.get("elo")),
            "started": rec.get("started"), "finished": rec.get("finished") if final else None,
            "elapsed": rec.get("elapsed_s"), "cost": rec.get("cost_usd") if final else round(sum(p.get("cost") or 0 for p in pm), 4),
            "model": rec.get("model") or (harn.get(rec.get("harness"), {}) or {}).get("model"),
            "uci": uci[:len(san)], "san": san, "fens": fens, "book": rec.get("opening_plies") or 0,
        })
    for gs in state.get("gens", {}).values():
        usd += sum(c.get("cost") or 0 for c in gs.get("children", []) or [])

    games.sort(key=lambda g: (g["finished"] is None, g["finished"] or g["started"] or 0, g["id"]))
    for i, g in enumerate(games):
        g["i"] = i

    # lineage of the champion: the run's rating input
    champ = state.get("champion") or "g0"
    lineage, v = [], champ
    while v and v in harn and v not in lineage:
        lineage.append(v)
        v = harn[v].get("parent")
    lineage = lineage or [champ]
    complete = [g for g in games if g["status"] == "complete" and g["score"] is not None and g["opp_elo"] is not None]
    hist = [g for g in complete if g["vid"] in lineage]

    timeline, seen = [], []
    for g in hist:
        seen.append(g)
        e = R.estimate(seen, window)
        if e["elo"] is not None:
            timeline.append({"t": g["finished"], "i": g["i"], "elo": round(e["elo"]),
                             "lo": None if e["lo"] is None else round(e["lo"]),
                             "hi": None if e["hi"] is None else round(e["hi"]), "n": e["n"]})

    docs = {d["id"]: d for d in _jsonl(run_dir / "versions.jsonl") if d.get("id")}
    pruned = {p.get("vid"): p for p in state.get("pruned", []) or []}
    order = state.get("order") or list(harn)
    versions = []
    for vid in order:
        h = harn.get(vid) or {}
        mine = [g for g in complete if g["vid"] == vid]
        allg = [g for g in games if g["vid"] == vid]
        t0 = min((g["started"] for g in allg if g["started"]), default=None)
        t1 = max(((g["finished"] or now) for g in allg), default=None)
        d = docs.get(vid, {})
        if vid == champ:
            status = "champion"
        elif vid in pruned or d.get("kept") is False:
            status = "pruned"
        elif vid in lineage:
            status = "kept"
        else:
            status = "kept" if d.get("kept") else "pending"
        p = pruned.get(vid) or {}
        exam = d.get("exam") or p.get("exam")
        versions.append({
            "id": vid, "parent": h.get("parent"), "change": h.get("change") or ("seed harness" if not h.get("parent") else None),
            "model": h.get("model"), "status": status, "signal": d.get("signal") or (f"pruned at {p['stage']}" if p.get("stage") else None),
            "est": _est(mine, 10 ** 6) if mine else None,
            "games": len(mine), "w": sum(g["score"] > 0.5 for g in mine), "d": sum(g["score"] == 0.5 for g in mine),
            "l": sum(g["score"] < 0.5 for g in mine), "live": sum(g["status"] == "in_progress" for g in allg),
            "dur": (t1 - t0) if t0 and t1 else None, "exam": exam,
        })
    by = {v["id"]: v for v in versions}
    for v in versions:
        par = by.get(v["parent"])
        if v["est"] and par and par["est"] and v["est"]["elo"] is not None and par["est"]["elo"] is not None:
            v["gain"] = v["est"]["elo"] - par["est"]["elo"]

    ladder = load_ladder(LADDER_JSON)
    climb = set(ladder.get("climb", []))
    t_start = state.get("started") or (events[0]["t"] if events else None)
    t_last = max([e.get("t") or 0 for e in events] + [g["finished"] or 0 for g in games] + [t_start or 0])
    g0 = [g for g in complete if g["vid"] == (order[0] if order else "g0")]
    return {
        "run": state.get("run") or run_dir.name, "generated": now, "done": done, "phase": state.get("phase"),
        "gen": state.get("gen"), "champion": champ, "lineage": lineage, "window": window,
        "scale": ladder.get("scale"), "calibration": ladder.get("calibration"), "stockfish": ladder.get("stockfish_version"),
        "rungs": [{"name": r["name"], "elo": r["elo"], "lo": r.get("lo"), "hi": r.get("hi"), "climb": r["name"] in climb,
                   "kind": r["spec"]["kind"]} for r in ladder["rungs"]],
        "started": t_start, "last": t_last if done else now, "spend_usd": round(usd, 4), "calls": calls,
        "start_est": _est(g0[:window], window) if g0 else None,
        "cur_est": _est(hist, window) if hist else None,
        "milestones": state.get("milestones") or [],
        "versions": versions, "games": games, "timeline": timeline,
    }


def render(data: dict) -> str:
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("__DATA__", blob).replace("__RUN__", data["run"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--runs", default=str(ROOT / "runs" / "rsi"))
    ap.add_argument("--out", default=str(ROOT / "web" / "judge.html"))
    a = ap.parse_args()
    data = collect(Path(a.runs) / a.run)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(render(data))
    tmp.replace(out)
    print(f"{out}: {len(data['games'])} games, {len(data['versions'])} versions")


if __name__ == "__main__":
    main()
