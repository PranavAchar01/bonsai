"""Proposer lint, apply and the re-ask loop. No model calls: `ask` is a fake."""

import json

import pytest

from rsi import propose as PR
from rsi import reliable

PARENT = {
    "id": "g3a", "parent": "g2a", "model": "claude-haiku-4-5-20251001",
    "rules": "You play chess. Reply with your move in UCI, for example e2e4, or in SAN.",
    "lessons": [{"id": "L2", "text": "Before moving, list every piece of yours the opponent attacks.",
                 "motif": "ignored_threat", "gen_added": 2, "rate_at_add": 3.2}],
    "tools": ["legal_moves"], "think": 0,
}
EFF = {"motif": "hung_moved_piece", "from": 4.0, "to": 2.5}


def ch(**kw):
    return {"expected_effect": EFF, "rationale": "r", **kw}


def bad(change, pruned=()):
    return PR.lint(PARENT, change, pruned)


def test_good_changes_pass():
    assert bad(ch(field="lesson", op="add", text="After choosing a move, check whether the moved piece can be "
                                                  "captured for free.", motif="hung_moved_piece")) == []
    assert bad(ch(field="tool", op="on", tool="threats")) == []
    assert bad(ch(field="tool", op="off", tool="legal_moves")) == []
    assert bad(ch(field="think", op="set", think=1024)) == []
    assert bad(ch(field="lesson", op="retire", lesson_id="L2")) == []
    assert bad(ch(field="lesson", op="edit", lesson_id="L2", text="Name each attacked piece, then save it.")) == []


@pytest.mark.parametrize("text", [
    "Never move the knight to f3 early.",          # square
    "Answer Bb5 by attacking the bishop.",          # SAN
    "Prefer exd5 when the centre opens.",           # SAN pawn capture
    "Avoid g1f3 before castling.",                  # UCI
    "Castle with O-O before move ten.",             # castling SAN
    "In the Sicilian, trade the dark bishop.",      # opening name
    "Against the Queen's Gambit keep the pawn.",    # opening name
    "In B20 lines push the f pawn.",                # ECO code
])
def test_lesson_leaks_rejected(text):
    probs = bad(ch(field="lesson", op="add", text=text, motif="positional"))
    assert probs and any("names" in p for p in probs), probs


def test_rules_may_keep_parent_examples_but_not_add_new_ones():
    keep = PARENT["rules"] + " Look at every capture first."
    assert bad(ch(field="rules", op="replace", text=keep)) == []
    add = PARENT["rules"] + " Prefer d4 squares."
    assert any("square" in p for p in bad(ch(field="rules", op="replace", text=add)))
    assert any("unchanged" in p for p in bad(ch(field="rules", op="replace", text=PARENT["rules"])))
    assert any("over 1200" in p for p in bad(ch(field="rules", op="replace", text="x" * 1300)))


@pytest.mark.parametrize("tool", ["stockfish", "engine_eval", "mate_in_one", "blunder_check"])
def test_engine_and_unknown_tools_rejected(tool):
    assert any("unknown tool" in p or "tier" in p for p in bad(ch(field="tool", op="on", tool=tool)))


def test_tool_noops_rejected():
    assert any("already on" in p for p in bad(ch(field="tool", op="on", tool="legal_moves")))
    assert any("not on" in p for p in bad(ch(field="tool", op="off", tool="pins")))


def test_multi_field_rejected():
    probs = bad(ch(field="lesson", op="add", text="Check captures first.", motif="missed_free_capture",
                   tool="threats"))
    assert any("more than one field" in p for p in probs)
    probs = bad(ch(field="think", op="set", think=1024, text="also new rules"))
    assert any("more than one field" in p for p in probs)
    assert any("unknown keys" in p for p in bad(ch(field="think", op="set", think=1024, samples=3)))


@pytest.mark.parametrize("think", [3000, 4096, 2048, -1024, 512])
def test_think_off_steps_rejected(think):
    assert bad(ch(field="think", op="set", think=think))


def test_think_one_step_from_middle():
    p = {**PARENT, "think": 1024}
    assert PR.lint(p, ch(field="think", op="set", think=0)) == []
    assert PR.lint(p, ch(field="think", op="set", think=2048)) == []
    assert PR.lint(p, ch(field="think", op="set", think=1024))


def test_repeat_of_pruned_rejected_modulo_case_and_spacing():
    c = ch(field="lesson", op="add", text="Check every capture the opponent has.", motif="ignored_threat")
    pruned = [{"change": PR.describe(c), "sig": PR.signature(c), "exam": {"n": 40, "gain": -1.2, "p": 0.2}}]
    again = ch(field="lesson", op="add", text="check  every capture the opponent has.", motif="ignored_threat")
    assert any("pruned" in p for p in bad(again, pruned))
    other = ch(field="lesson", op="add", text="Check every capture you have.", motif="missed_free_capture")
    assert bad(other, pruned) == []


def test_lesson_cap_ids_length_and_effect():
    full = {**PARENT, "lessons": [{"id": f"L{i}", "text": "t", "motif": "positional"} for i in range(8)]}
    assert any("already 8" in p for p in PR.lint(full, ch(field="lesson", op="add", text="New habit.",
                                                          motif="positional")))
    assert any("no lesson with id" in p for p in bad(ch(field="lesson", op="retire", lesson_id="L9")))
    assert any("over 220" in p for p in bad(ch(field="lesson", op="add", text="y" * 230, motif="positional")))
    assert any("not a digest motif" in p for p in bad(ch(field="lesson", op="add", text="Habit.", motif="vibes")))
    assert any("expected_effect" in p for p in PR.lint(PARENT, {"field": "think", "op": "set", "think": 1024}))
    assert any("unknown field" in p for p in bad(ch(field="samples", op="set")))


def test_apply_each_field():
    c = PR.apply(PARENT, ch(field="lesson", op="add", text="Count attackers.", motif="hung_moved_piece"), gen=5,
                 stats={"motifs": {"hung_moved_piece": {"per100": 4.0}}})
    new = c["lessons"][-1]
    assert new == {"id": "L5", "text": "Count attackers.", "motif": "hung_moved_piece", "gen_added": 5,
                   "rate_at_add": 4.0}
    assert c["parent"] == "g3a" and "id" not in c and PARENT["lessons"][-1]["id"] == "L2"  # parent untouched
    assert PR.apply(PARENT, ch(field="lesson", op="retire", lesson_id="L2"), 5)["lessons"] == []
    assert PR.apply(PARENT, ch(field="tool", op="on", tool="threats"), 5)["tools"] == ["legal_moves", "threats"]
    assert PR.apply(PARENT, ch(field="think", op="set", think=1024), 5)["think"] == 1024
    assert PR.removal_only(ch(field="lesson", op="retire", lesson_id="L2"))
    assert not PR.removal_only(ch(field="tool", op="on", tool="threats"))


class FakeAsk:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, prompt, system, **kw):
        self.calls.append((prompt, system, kw))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"kind": "ok", "structured": r, "text": json.dumps(r), "cost": 0.05, "model": "claude-opus-x"}


def test_propose_reasks_once_then_succeeds():
    leaky = ch(field="lesson", op="add", text="Never play Nf3.", motif="positional")
    good = ch(field="tool", op="on", tool="threats")
    ask = FakeAsk([leaky, good])
    r = PR.propose(PARENT, "# digest", gen=4, ask=ask)
    assert r["status"] == "ok" and r["attempts"] == 2 and r["child"]["tools"] == ["legal_moves", "threats"]
    assert len(ask.calls) == 2 and "rejected" in ask.calls[1][0] and "SAN" in ask.calls[1][0]
    kw = ask.calls[0][2]
    assert kw["model"] == "opus" and kw["schema"] == PR.SCHEMA and kw["timeout"] == 300
    assert r["cost"] == 0.1 and r["model"] == "claude-opus-x"


def test_propose_gives_up_after_second_lint_failure():
    leaky = ch(field="lesson", op="add", text="Play e4.", motif="positional")
    r = PR.propose(PARENT, "# digest", gen=4, ask=FakeAsk([leaky, leaky]))
    assert r["status"] == "lint_failed" and r["lint"]


def test_propose_infra_is_not_a_pruned_child():
    r = PR.propose(PARENT, "# digest", gen=4, ask=FakeAsk([reliable.InfraError("timeout", "no reply")]))
    assert r["status"] == "infra" and r["kind"] == "timeout" and "sig" not in r


def test_prompt_lists_only_tier2_tools_and_digest():
    p = PR.build_prompt(PARENT, "# Mistake digest XYZ", "rung E100")
    assert "XYZ" in p and "threats" in p and "rung E100" in p and "stockfish" not in p.lower()
