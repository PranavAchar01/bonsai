"""Sight tools: tiers, registry shape and the text each tool shows on hand-built positions."""

import chess
import pytest

from rsi import perceive as P


def test_registry_shape_and_tiers():
    assert set(P.TOOLS) == {"legal_moves", "pieces", "threats", "hanging", "checks", "pins"}
    for name, t in P.TOOLS.items():
        assert set(t) == {"tier", "describe", "fn"}
        assert t["tier"] in (1, 2) and t["describe"] and callable(t["fn"])
    assert P.tier([]) == 0
    assert P.tier(["legal_moves", "pieces"]) == 1
    assert P.tier(["legal_moves", "pins"]) == 2


def test_no_tier3_tools_exposed():
    for name in ("mate_in_one", "blunder_check", "targets"):
        assert name not in P.TOOLS
    assert max(t["tier"] for t in P.TOOLS.values()) <= 2


def test_render_order_dedup_and_unknown():
    b = chess.Board()
    out = P.render(b, chess.WHITE, ["pieces", "legal_moves", "pieces"])
    assert out.index("Your pieces") < out.index("Legal moves:")
    assert out.count("Your pieces") == 1
    assert P.render(b, chess.WHITE, []) == ""
    with pytest.raises(ValueError):
        P.render(b, chess.WHITE, ["legal_moves", "mate_in_one"])
    assert P.unknown(["pins", "x"]) == ["x"]


def test_legal_moves_start():
    out = P.render(chess.Board(), chess.WHITE, ["legal_moves"])
    assert "e4(e2e4)" in out and "Nf3(g1f3)" in out
    assert len(out.split(":", 1)[1].split()) == 20
    assert "not your turn" in P.render(chess.Board(), chess.BLACK, ["legal_moves"])


def test_pieces_perspective():
    b = chess.Board()
    w = P.render(b, chess.WHITE, ["pieces"])
    assert w.startswith("Your pieces (White): king e1; queen d1")
    k = P.render(b, chess.BLACK, ["pieces"])
    assert k.startswith("Your pieces (Black): king e8; queen d8")


def test_threats_and_hanging():
    # White knight e5 is attacked by the black pawn d6 and defended by nothing: it can be won.
    b = chess.Board("4k3/8/3p4/4N3/8/8/8/4K3 w - - 0 1")
    th = P.render(b, chess.WHITE, ["threats"])
    assert "knight on e5" in th and "lose 3" in th
    # From Black's side the same knight is a winning capture.
    b.turn = chess.BLACK
    hg = P.render(b, chess.BLACK, ["hanging"])
    assert "dxe5" in hg and "(+3)" in hg
    # Black can still see it even when it's White to move (null-move view).
    b.turn = chess.WHITE
    assert "dxe5" in P.render(b, chess.BLACK, ["hanging"])


def test_defended_piece_not_hanging_and_bad_capture_skipped():
    # Queen takes a pawn defended by a pawn: SEE negative, not listed.
    b = chess.Board("4k3/2p5/3p4/8/8/8/8/3QK3 w - - 0 1")
    assert P.render(b, chess.WHITE, ["hanging"]) == "Opponent pieces you can win: none."
    assert "none of your pieces" in P.render(chess.Board(), chess.WHITE, ["threats"])


def test_checks():
    b = chess.Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
    out = P.render(b, chess.WHITE, ["checks"])
    assert "Ra8+" in out and "Re1" not in out
    b = chess.Board("4k3/8/8/8/8/8/8/4R1K1 b - - 0 1")
    out = P.render(b, chess.BLACK, ["checks"])
    assert "You are in check from e1" in out


def test_pins():
    # Black knight e5 pinned to its king e8 by the white rook e1.
    b = chess.Board("4k3/8/8/4n3/8/8/8/4RK2 w - - 0 1")
    out = P.render(b, chess.WHITE, ["pins"])
    assert "Your pieces pinned to your king: none" in out
    assert "knight e5 (by e1)" in out
    assert P.pins(b, chess.BLACK) == [(chess.E5, chess.E1)]


def test_all_tools_survive_odd_positions():
    fens = [
        chess.STARTING_FEN,
        "4k3/8/8/8/8/8/8/4R1K1 b - - 0 1",              # in check
        "7k/5QQ1/8/8/8/8/8/6K1 b - - 0 1",              # stalemate
        "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3",  # checkmated
        "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2",            # en passant
    ]
    for fen in fens:
        b = chess.Board(fen)
        for color in (chess.WHITE, chess.BLACK):
            out = P.render(b, color, list(P.TOOLS))
            assert isinstance(out, str) and out
        assert b.fen() == chess.Board(fen).fen()  # tools never mutate the board


def test_miner_helpers():
    b = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    assert [m.uci() for m in P.mates_in_one(b)] == ["a1a8"]
    assert P.material(chess.Board(), chess.WHITE) == 39
    b = chess.Board("4k3/8/3p4/4N3/8/8/8/4K3 b - - 0 1")
    assert P.see(b, chess.Move.from_uci("d6e5")) == 3
    assert P.en_prise(b, chess.WHITE) == [(chess.E5, 3)]
    b = chess.Board("r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1")
    m = chess.Move.from_uci("b5c7")
    b.push(m)
    assert P.is_fork(b, m)


def test_mate_not_leaked_through_san():
    b = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
    out = P.render(b, chess.WHITE, list(P.TOOLS))
    assert "#" not in out and "Qxf7+(h5f7)" in out
