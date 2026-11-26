"""Polymarket likidite odulu formulu (research/poly_rewards_scan.py) + kayit raporu (2026-11-26).

Formul kaynagi: docs.polymarket.com/programs/liquidity-rewards.md (2026-11-26 okundu).
  S(v, s) = ((v - s) / v)^2 ; Q_min mid [0.10, 0.90] icinde max(min(Q1, Q2), max(Q1, Q2) / 3), disinda min.
Ag cagrisi YOK; yalnizca saf yardimcilar ve gecici DB.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(RESEARCH))

import poly_mm_reward_backtest as bt  # noqa: E402
import poly_rewards_recorder as rec  # noqa: E402
import poly_rewards_scan as scan  # noqa: E402

_DEG = chr(0xB0)  # derece isareti; kaynak kod ASCII kalsin diye chr ile


@pytest.mark.parametrize(
    ("question", "mtype", "expected"),
    [
        (f"Will the highest temperature in Paris be 5{_DEG}C on November 26?", "RANGE", (4.5, 5.5)),
        (f"Will the highest temperature in Tokyo be 20{_DEG}C or higher on November 18?", "HIGH", (19.5, float("inf"))),
        (f"Will the highest temperature in NYC be 49{_DEG}F or below on November 18?", "LOW", (-float("inf"), 9.7222)),
        (f"Will the highest temperature in NYC be between 50-51{_DEG}F on November 18?", "RANGE", (9.7222, 10.8333)),
    ],
)
def test_backtest_bucket_edges_follow_whole_degree_rounding(question, mtype, expected):
    """Kova kenarlari Polymarket tam derece yuvarlamasina gore (C); F araliklari C'ye cevrilir.

    2026-11-26: derece isareti kaynakta kacis dizisi olarak bozulunca ayristirma sessizce None dondu.
    """
    lo, hi = bt._edges_c(question, mtype)
    assert lo == pytest.approx(expected[0], abs=1e-3)
    assert hi == pytest.approx(expected[1], abs=1e-3)


def test_first_side_score_matches_documented_example():
    """Belge ornegi: mid 0.50, v = 3 sent; 100 alis @0.49, 200 alis @0.48 ve NO'da 100 satis @0.51.

    YES defteri NO defterinin aynasi oldugu icin NO satisi @0.51, YES defterinde 100 alis @0.49 gorunur.
    """
    bids = [(0.49, 100.0), (0.48, 200.0), (0.49, 100.0)]
    expected = (2 / 3) ** 2 * 100 + (1 / 3) ** 2 * 200 + (2 / 3) ** 2 * 100
    assert scan._side_q(bids, 0.50, 3.0, 0.0) == pytest.approx(expected)


def test_orders_outside_window_or_below_min_size_do_not_score():
    bids = [(0.47, 100.0), (0.495, 10.0)]  # 3 sent uzak (s >= v) ve 10 < min size 20
    assert scan._side_q(bids, 0.50, 3.0, 20.0) == 0.0


def test_qmin_single_sided_rule():
    assert scan._qmin(90.0, 0.0, 0.50) == pytest.approx(30.0)  # [0.10, 0.90]: tek taraf / 3 sayilir
    assert scan._qmin(90.0, 0.0, 0.95) == 0.0  # disinda iki taraf sart
    assert scan._qmin(90.0, 60.0, 0.50) == 60.0


def test_others_upper_bound_holds_for_any_split_into_makers():
    """Kitap toplamindan hesaplanan rakip Q_min ust siniri, makerlara her bolunuste gecerli olmali."""
    rnd = random.Random(7)
    for _ in range(2000):
        mid = rnd.choice([0.05, 0.30, 0.50, 0.85, 0.95])
        makers = [(rnd.random() * rnd.choice([0, 1, 50]), rnd.random() * rnd.choice([0, 1, 50])) for _ in range(5)]
        total = sum(scan._qmin(a, b, mid) for a, b in makers)
        q1, q2 = sum(a for a, _ in makers), sum(b for _, b in makers)
        assert total <= scan._others_upper(q1, q2, mid) + 1e-9


def test_adjusted_mid_skips_levels_below_min_size():
    bids = [(0.72, 2.88), (0.71, 86.28)]
    asks = [(0.73, 120.0), (0.75, 22.25)]
    mid, bb, ba = scan._adjusted_mid(bids, asks, 20.0)
    assert (bb, ba) == (0.71, 0.73)
    assert mid == pytest.approx(0.72)


def test_our_quote_rounds_to_tick_and_sizes_capital():
    qmin, cap, n = scan._our_quote(0.725, 4.5, 20.0, 0.01, 100.0, 2.25)
    # alis floor(0.7025) = 0.70, satis ceil(0.7475) = 0.75 -> genislik 0.05 -> n = 100 / 0.95
    assert n == pytest.approx(100 / 0.95)
    assert cap == pytest.approx(100.0)
    assert qmin == pytest.approx(((4.5 - 2.5) / 4.5) ** 2 * n)


def test_recorder_report_decodes_scaled_samples(tmp_path):
    con = rec._db(tmp_path / "r.db")
    con.execute(
        "INSERT INTO markets (id, cid, yes_token, question, event_slug, category, end_date, first_seen) "
        "VALUES (1, '0xabc', '1', 'q', 'e', 'weather', '', 0)"
    )
    # mid 0.50, rakip yok -> pay 1.0; oran $100/gun (x100), v 4.5 (x10), min size 20, tick 0.01 (x10000)
    con.execute(
        "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1795651200, 1, 5000, 4900, 5100, 0, 0, 10000, 45, 20, 100),
    )
    con.commit()
    out = tmp_path / "rapor.md"
    rec._report(con, 100.0, out)
    assert "| weather | 1 | 1 | %100 | $100.00 | $100.00 |" in out.read_text(encoding="utf-8")
