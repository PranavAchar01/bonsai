"""Mining and bank on tiny real games with a low node count (Stockfish only, no model calls)."""

import os
import signal

import chess
import pytest

from rsi import bank as BK
from rsi import mine as M

NODES = 20_000


def record(sans, color, split="train", status="complete", game="t1", **kw):
    b, ucis = chess.Board(), []
    for s in sans:
        m = b.parse_san(s)
        ucis.append(m.uci())
        b.push(m)
    return {"game": game, "moves": ucis, "color": color, "split": split, "status": status, "opening_plies": 0, **kw}


# Black (the agent) allows Scholar's mate: 3...Nf6?? 4.Qxf7#
SCHOLAR = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]
# White (the agent) puts the queen where a pawn takes it: 2.Qg4?? d5xg4... (2...Bxg4 by the c8 bishop after d6)
HANG = ["e4", "d6", "Qg4", "Bxg4"]


@pytest.fixture(scope="module")
def engine():
    e = M.Engine(NODES)
    yield e
    e.close()


def test_win_pct_and_classes():
    assert M.win_pct(0) == 50
    assert M.win_pct(5000) == M.win_pct(1000) > 97
    assert M.classify(16) == "blunder" and M.classify(11) == "mistake" and M.classify(6) == "inaccuracy"
    assert M.classify(2) is None


def test_allowed_mate_is_a_blunder_with_motif(engine):
    g = M.mine_games([record(SCHOLAR, "black", per_move=[{"ply": 5, "reason": "develop the knight",
                                                           "attempts": [{"kind": "illegal", "move": "f8f5"}]}])],
                     engine)[0]
    nf6 = next(m for m in g["moves"] if m["played"] == "Nf6")
    assert nf6["class"] == "blunder" and "allowed_mate_in_1" in nf6["motifs"]
    assert nf6["reason"] == "develop the knight"
    assert nf6["rejected"] == ["piece cannot move there (blocked line / wrong geometry)"]
    assert [m["ply"] for m in g["moves"]] == [1, 3, 5]  # only the agent's moves
    assert nf6["history"] == ["e4", "e5", "Bc4", "Nc6", "Qh5"]


def test_hung_queen(engine):
    g = M.mine_games([record(HANG, "white")], engine)[0]
    qg4 = next(m for m in g["moves"] if m["played"] == "Qg4")
    assert qg4["class"] == "blunder" and "hung_moved_piece" in qg4["motifs"]


def test_opening_plies_skipped(engine):
    g = M.mine_games([{**record(SCHOLAR, "black"), "opening_plies": 4}], engine)[0]
    assert [m["ply"] for m in g["moves"]] == [5]


def test_held_game_raises_and_infra_skipped(engine):
    with pytest.raises(M.HeldGameError):
        M.mine_games([record(SCHOLAR, "black"), record(HANG, "white", split="held", game="h1")], engine)
    with pytest.raises(AssertionError):
        M.digest([{"game": "h", "split": "held", "moves": []}])
    out = M.mine_games([record(SCHOLAR, "black"), record(HANG, "white", status="infra", game="v")], engine)
    assert [g["game"] for g in out] == ["t1"]


def test_reply_cause_uci_and_san():
    fen = chess.STARTING_FEN
    assert M.reply_cause(fen, {"kind": "format"}) == "no move found in the reply"
    assert "not there" in M.reply_cause(fen, {"kind": "illegal", "move": "e3e4"})
    assert "geometry" in M.reply_cause(fen, {"kind": "illegal", "move": "f1c4"})
    assert "not legal" in M.reply_cause(fen, {"kind": "illegal", "move": "Bc4"})
    assert "malformed" in M.reply_cause(fen, {"kind": "illegal", "move": "castle!"})
    pinned = "4k3/4r3/8/8/8/8/4B3/4K3 w - - 0 1"
    assert "check or pin" in M.reply_cause(pinned, {"kind": "illegal", "move": "e2d3"})


def test_digest_contents_trend_ledger_pruned_and_budget(engine):
    games = M.mine_games([record(SCHOLAR, "black"), record(HANG, "white", game="t2")], engine)
    harness = {"lessons": [{"id": "L1", "text": "Check mates first.", "motif": "allowed_mate_in_1", "gen_added": 1,
                            "rate_at_add": 9.0}]}
    hist = [{"gen": 3, "motifs": {"allowed_mate_in_1": {"per100": 7.5}}}, {"gen": 2, "motifs": {}}]
    pruned = [{"change": "tool pins on", "exam": {"n": 40, "gain": -0.8, "p": 0.21}}]
    text, stats = M.digest(games, harness, hist, pruned, {"think 0": "~5 s, $0.003/call"}, title="gen 4")
    for s in ("allowed_mate_in_1", "gen 3", "gen 2", "7.5", "Lesson ledger", "L1", "9 ->", "tool pins on",
              "P(better) 0.21", "think 0", "only after the move", "Worst moves", "Unusable" if stats["rejected"] else ""):
        assert s in text, s
    assert "infra" not in text.lower()
    assert stats["motifs"]["allowed_mate_in_1"]["n"] == 1
    # 200 games of examples still fit the 2.5k-token budget
    many = [{**g, "game": f"x{i}-{g['game']}"} for i in range(100) for g in games]
    big, st = M.digest(many, harness, hist, pruned * 30)
    assert len(big) <= M.DIGEST_TOKENS * M.CHARS_PER_TOKEN and st["digest_tokens_est"] <= M.DIGEST_TOKENS


def test_engine_restarts_after_kill():
    e = M.Engine(NODES)
    try:
        os.killpg(e.eng.transport.get_pid(), signal.SIGKILL)
        info = e.analyse(chess.Board())
        assert "score" in info
    finally:
        e.close()


def test_bank_scorer_and_anchor(tmp_path, engine):
    sc = BK.Scorer(tmp_path / "evalcache.json", engine=engine)
    path = tmp_path / "bank.jsonl"
    with pytest.raises(ValueError):
        BK.add_held_games([record(SCHOLAR, "black")], path, gen=0, scorer=sc, engine=engine)
    held = [record(SCHOLAR, "black", split="held", game="h1", opp_elo=100),
            record(HANG, "white", split="held", game="h2", opp_elo=300)]
    n = BK.add_held_games(held, path, gen=0, scorer=sc, engine=engine)
    assert n >= 2 and BK.add_held_games(held, path, gen=0, scorer=sc, engine=engine) == 0  # deduped
    rows = [r for r in __import__("rsi.reliable", fromlist=["x"]).read_jsonl(path)]
    nf6 = next(r for r in rows if r["src"] == "h1:5")
    assert nf6["mine"] == "black" and nf6["game_loss"] >= 10 and nf6["opp_elo"] == 100
    assert (tmp_path / "evalcache.json").exists()
    assert sc.loss(nf6["fen"], "g8f6") >= 15  # the blunder again
    assert sc.loss(nf6["fen"], nf6["best"]) <= 2
    assert sc.loss(nf6["fen"], None) == BK.FORFEIT_LOSS
    picked = BK.select(rows, 300, 1, seed="s")
    assert picked[0]["opp_elo"] == 300
    anchor = BK.freeze_anchor(path, tmp_path / "anchor.jsonl", n=2)
    assert len(anchor) == 2 and BK.freeze_anchor(path, tmp_path / "anchor.jsonl", n=99) == anchor
