"""Opus proposer: reads the train-game digest and returns ONE typed harness change, linted before any games are spent.

A change is one of
  {"field": "rules",  "op": "replace", "text"}
  {"field": "lesson", "op": "add", "text", "motif"} | {"op": "edit", "lesson_id", "text"} | {"op": "retire", "lesson_id"}
  {"field": "tool",   "op": "on"|"off", "tool"}
  {"field": "think",  "op": "set", "think"}            (one step within THINK_LEVELS)
plus "expected_effect": {"motif", "from", "to"} (per 100 moves) and a short "rationale".

The lint rejects square names, SAN/UCI moves, opening names, unknown or engine tools, more than one field, think off
the allowed steps, and an exact repeat of a pruned change. A rejected reply is re-asked once with the reasons.
A proposer infra failure returns status "infra": the caller retries next generation and never stores it as pruned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rsi import mine as M
from rsi import perceive as P
from rsi import reliable

MODEL = "opus"  # the pinned CLI rejects full Opus ids; the resolved id is recorded from the reply
TIMEOUT = 300
MAX_TIER = 2
THINK_LEVELS = (0, 1024, 2048)
RULES_CHARS, LESSON_CHARS, LESSON_CAP = 1200, 220, 8
EFFECT_MOTIFS = set(M.MOTIF_TEXT) | {"unusable_replies", "mean_loss"}

OPS = {"rules": {"replace": {"text"}},
       "lesson": {"add": {"text", "motif"}, "edit": {"lesson_id", "text", "motif"}, "retire": {"lesson_id"}},
       "tool": {"on": {"tool"}, "off": {"tool"}},
       "think": {"set": {"think"}}}
PAYLOAD = {"text", "motif", "lesson_id", "tool", "think"}
META = {"field", "op", "expected_effect", "rationale"}

SQUARE = re.compile(r"\b[a-h][1-8]\b")
UCI = re.compile(r"\b[a-h][1-8][a-h][1-8][qrbn]?\b")
SAN = re.compile(r"\b(?:[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8]|[a-h][18]=[QRBN])[+#]?|\bO-O(?:-O)?\b")
ECO = re.compile(r"\b[A-E]\d\d\b")
OPENING_WORDS = (
    "sicilian", "french", "caro", "kann", "italian", "giuoco", "ruy", "lopez", "spanish", "scotch", "petrov", "petroff",
    "philidor", "vienna", "king's gambit", "kings gambit", "queen's gambit", "queens gambit", "slav", "catalan",
    "nimzo", "king's indian", "kings indian", "grunfeld", "grünfeld", "benoni", "benko", "dutch", "english", "reti",
    "réti", "london", "colle", "torre", "trompowsky", "pirc", "modern defense", "alekhine", "scandinavian", "najdorf",
    "dragon", "sveshnikov", "evans", "two knights", "four knights", "fried liver", "budapest", "bird", "larsen",
    "orangutan", "polish", "stonewall", "hedgehog", "maroczy", "albin", "tarrasch", "winawer", "marshall", "berlin",
)

SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string", "enum": list(OPS)},
        "op": {"type": "string", "enum": sorted({o for v in OPS.values() for o in v})},
        "text": {"type": "string"}, "motif": {"type": "string"}, "lesson_id": {"type": "string"},
        "tool": {"type": "string"}, "think": {"type": "integer"},
        "expected_effect": {"type": "object", "properties": {"motif": {"type": "string"}, "from": {"type": "number"},
                                                             "to": {"type": "number"}},
                            "required": ["motif", "from", "to"]},
        "rationale": {"type": "string"},
    },
    "required": ["field", "op", "expected_effect", "rationale"],
}

SYSTEM = ("You improve a chess-playing agent by editing its harness, one small change at a time. "
          "You reply with a single JSON object that matches the given schema and nothing else.")

PROMPT = """The player is a fixed model ({model}) making one call per move. You may change only its harness:
{harness}

Fields you may change, exactly ONE per proposal:
- rules: the system prompt (<= {rules_chars} chars). op "replace" with the full new "text".
- lesson: playbook lines appended to the rules (<= {cap} lessons, <= {lesson_chars} chars each). op "add" ("text",
  "motif" = the motif it targets), "edit" ("lesson_id", "text"), or "retire" ("lesson_id").
- tool: text computed from the board and shown under it each move. op "on"/"off" with "tool". Available:
{tools}
- think: thinking-token budget per call, one of {think}; op "set" with "think", one step from the current value.
  A think increase must win the exam by >= 1.0 win-point per +1024 tokens to be kept (it doubles cost).

Ladder: {ladder}

{digest}
Write ONE change aimed at the motif that costs the most win chance and that the player could plausibly fix.
- Lessons and rules must be general habits or check procedures. No square names, no specific moves (SAN or UCI), no
  opening names: the change is scored on positions and openings you have not seen.
- Prefer making the player look at something it currently misses over exhortations ("be careful").
- At {cap} lessons, retire or edit one instead of adding. Do not repeat a change from the pruned list.
- Only the keys the op needs, plus "expected_effect" {{"motif", "from", "to"}} (per 100 moves, from the digest)
  and "rationale" (one sentence)."""


def known_tools() -> dict:
    return {n: t for n, t in P.TOOLS.items() if t["tier"] <= MAX_TIER}


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def signature(change: dict) -> str:
    """Identity of a change for the pruned list: exact repeats (modulo case and spacing) match."""
    return json.dumps([change.get("field"), change.get("op"), _norm(change.get("text")), change.get("lesson_id"),
                       change.get("tool"), change.get("think")])


def describe(change: dict) -> str:
    f, op = change.get("field"), change.get("op")
    if f == "rules":
        return f"rules replaced: {change.get('text', '')[:80]!r}"
    if f == "lesson":
        return f"lesson {op} {change.get('lesson_id') or ''} {change.get('text', '')[:120]!r}".replace("  ", " ")
    if f == "tool":
        return f"tool {change.get('tool')} {op}"
    return f"think -> {change.get('think')}"


def _leaks(text: str, before: str = "") -> list[str]:
    """Squares, moves, ECO codes and opening names in `text` that were not already in `before`."""
    bad = []
    for name, rx in (("square", SQUARE), ("UCI move", UCI), ("SAN move", SAN), ("ECO code", ECO)):
        new = set(rx.findall(text)) - set(rx.findall(before))
        if new:
            bad.append(f"names a {name}: {sorted(new)[:3]}")
    low, prev = text.lower(), before.lower()
    words = [w for w in OPENING_WORDS if re.search(rf"\b{re.escape(w)}\b", low) and not re.search(rf"\b{re.escape(w)}\b", prev)]
    if words:
        bad.append(f"names an opening: {words[:3]}")
    return bad


def lint(parent: dict, change: dict, pruned: list[dict] | tuple = ()) -> list[str]:
    """Reasons to reject the change before spending anything on it. Empty list = OK."""
    bad = []
    f, op = change.get("field"), change.get("op")
    if f not in OPS:
        return [f"unknown field {f!r} (one of {list(OPS)})"]
    if op not in OPS[f]:
        return [f"op {op!r} is not valid for {f} (one of {list(OPS[f])})"]
    extra = [k for k in change if k not in META and k not in OPS[f][op] and change[k] not in (None, "", [])]
    if extra:
        bad.append(f"changes more than one field: {f} plus {extra}")
    unknown_keys = [k for k in change if k not in META | PAYLOAD]
    if unknown_keys:
        bad.append(f"unknown keys {unknown_keys}")
    lessons = parent.get("lessons") or []
    ids = {l.get("id") for l in lessons}
    if f == "rules":
        text = change.get("text") or ""
        if not text.strip():
            bad.append("rules text is empty")
        if len(text) > RULES_CHARS:
            bad.append(f"rules over {RULES_CHARS} chars ({len(text)})")
        if _norm(text) == _norm(parent.get("rules")):
            bad.append("rules unchanged")
        bad += [f"rules {b}" for b in _leaks(text, parent.get("rules") or "")]
    elif f == "lesson":
        if op in ("edit", "retire") and change.get("lesson_id") not in ids:
            bad.append(f"no lesson with id {change.get('lesson_id')!r} (have {sorted(i for i in ids if i)})")
        if op == "add" and len(lessons) >= LESSON_CAP:
            bad.append(f"already {LESSON_CAP} lessons: retire or edit one instead")
        if op == "add" and change.get("motif") not in M.MOTIF_TEXT:
            bad.append(f"lesson motif {change.get('motif')!r} is not a digest motif")
        if op in ("add", "edit"):
            text = change.get("text") or ""
            if not text.strip():
                bad.append("lesson text is empty")
            if len(text) > LESSON_CHARS:
                bad.append(f"lesson over {LESSON_CHARS} chars ({len(text)})")
            bad += [f"lesson {b}" for b in _leaks(text)]
            if op == "edit" and any(l.get("id") == change.get("lesson_id") and _norm(l.get("text")) == _norm(text)
                                    for l in lessons):
                bad.append("lesson edit does not change the text")
    elif f == "tool":
        t, on = change.get("tool"), (parent.get("tools") or [])
        if t not in P.TOOLS:
            bad.append(f"unknown tool {t!r} (available: {sorted(known_tools())})")
        elif t not in known_tools():
            bad.append(f"tool {t!r} is above assist tier {MAX_TIER}")
        elif op == "on" and t in on:
            bad.append(f"tool {t} is already on")
        elif op == "off" and t not in on:
            bad.append(f"tool {t} is not on")
    elif f == "think":
        new, old = change.get("think"), int(parent.get("think") or 0)
        if new not in THINK_LEVELS:
            bad.append(f"think must be one of {list(THINK_LEVELS)}, got {new!r}")
        elif abs(new - old) != 1024:
            bad.append(f"think may move one step (1024) from {old}, got {new}")
    eff = change.get("expected_effect")
    if not isinstance(eff, dict) or not all(k in eff for k in ("motif", "from", "to")):
        bad.append("expected_effect must be {motif, from, to}")
    elif eff["motif"] not in EFFECT_MOTIFS:
        bad.append(f"expected_effect motif {eff['motif']!r} is not a digest motif")
    elif not all(isinstance(eff[k], (int, float)) for k in ("from", "to")):
        bad.append("expected_effect from/to must be numbers")
    sig = signature(change)
    if any(p.get("sig") == sig for p in pruned):
        bad.append("exact repeat of a pruned change")
    return bad


def apply(parent: dict, change: dict, gen: int, stats: dict | None = None) -> dict:
    """The child harness: the parent with the one linted change applied (id is set by the caller)."""
    child = json.loads(json.dumps(parent))
    child["parent"], child["change"] = parent.get("id"), describe(change)
    child.pop("id", None)
    f, op = change["field"], change["op"]
    if f == "rules":
        child["rules"] = change["text"].strip()
    elif f == "tool":
        tools = list(child.get("tools") or [])
        child["tools"] = tools + [change["tool"]] if op == "on" else [t for t in tools if t != change["tool"]]
    elif f == "think":
        child["think"] = int(change["think"])
    elif op == "retire":
        child["lessons"] = [l for l in child["lessons"] if l.get("id") != change["lesson_id"]]
    elif op == "edit":
        for l in child["lessons"]:
            if l.get("id") == change["lesson_id"]:
                l["text"] = change["text"].strip()
                if change.get("motif") in M.MOTIF_TEXT:
                    l["motif"] = change["motif"]
    else:
        motif = change["motif"]
        taken = {l.get("id") for l in child.get("lessons") or []}
        lid, k = f"L{gen}", 1
        while lid in taken:
            k += 1
            lid = f"L{gen}.{k}"
        rate = ((stats or {}).get("motifs", {}).get(motif) or {}).get("per100", 0.0)
        child["lessons"] = (child.get("lessons") or []) + [
            {"id": lid, "text": change["text"].strip(), "motif": motif, "gen_added": gen, "rate_at_add": rate}]
    return child


def removal_only(change: dict) -> bool:
    """A shed: the child only removes something (exam then keeps it on non-inferiority)."""
    return (change.get("field") == "lesson" and change.get("op") == "retire") or \
           (change.get("field") == "tool" and change.get("op") == "off")


def build_prompt(parent: dict, digest_text: str, ladder: str = "") -> str:
    view = {k: parent.get(k) for k in ("rules", "lessons", "tools", "think")}
    tools = "\n".join(f"  {n} (tier {t['tier']}{', ON' if n in (parent.get('tools') or []) else ''}): {t['describe']}"
                      for n, t in known_tools().items())
    return PROMPT.format(model=parent.get("model", "haiku"), harness=json.dumps(view, indent=1),
                         rules_chars=RULES_CHARS, cap=LESSON_CAP, lesson_chars=LESSON_CHARS, tools=tools,
                         think=list(THINK_LEVELS), ladder=ladder or "(no rating yet)", digest=digest_text)


def _parse(reply: dict) -> dict | None:
    s = reply.get("structured")
    if isinstance(s, dict):
        return s
    text = reply.get("text") or ""
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return None


def propose(parent: dict, digest_text: str, *, gen: int, stats: dict | None = None, pruned=(), ladder: str = "",
            ask=None, model: str = MODEL, think_tokens: int = 0, log=None) -> dict:
    """One proposal. Returns {"status": "ok", change, child, sig, removal_only, ...} or status "lint_failed" | "infra".

    `ask` defaults to reliable.ask_claude (tests pass a fake). Lint failures are re-asked once with the reasons.
    """
    ask = ask or reliable.ask_claude
    prompt = build_prompt(parent, digest_text, ladder)
    cost, models, change, problems = 0.0, [], None, []
    for attempt in (1, 2):
        try:
            reply = ask(prompt, SYSTEM, model=model, schema=SCHEMA, timeout=TIMEOUT, attempts=3,
                        think_tokens=think_tokens, log=log)
        except reliable.InfraError as e:
            return {"status": "infra", "error": str(e), "kind": e.kind, "cost": cost, "attempt": attempt}
        cost += reply.get("cost") or 0.0
        models.append(reply.get("model"))
        change = _parse(reply)
        problems = ["reply is not a JSON object"] if change is None else lint(parent, change, pruned)
        if not problems:
            return {"status": "ok", "change": change, "child": apply(parent, change, gen, stats),
                    "sig": signature(change), "describe": describe(change), "removal_only": removal_only(change),
                    "cost": round(cost, 4), "model": models[-1], "attempts": attempt}
        prompt += ("\n\nYour last proposal was rejected:\n- " + "\n- ".join(problems)
                   + "\nFix that and reply with one JSON object only.")
    return {"status": "lint_failed", "change": change, "lint": problems, "cost": round(cost, 4), "model": models[-1],
            "attempts": 2}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="One proposal from a digest (real Opus call).")
    ap.add_argument("--parent", required=True)
    ap.add_argument("--digest", required=True)
    ap.add_argument("--gen", type=int, default=1)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    res = propose(json.loads(Path(a.parent).read_text()), Path(a.digest).read_text(), gen=a.gen)
    print(json.dumps(res, indent=1))
    if a.out:
        reliable.atomic_write_json(a.out, res, indent=1)


if __name__ == "__main__":
    main()
