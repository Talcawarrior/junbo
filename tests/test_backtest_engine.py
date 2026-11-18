"""Backtest engine dogrulama: 12 gun tam evren, won/lost/closed ayri, bias duzeltmeli."""

import sqlite3
from pathlib import Path

from backtest.engine import STAKE, load_data
from utils.db_paths import get_db_path

BOT_DB = Path(get_db_path())  # 2026-11-18: D: sabit varsayilani kaldirildi


def test_load_data_has_settlement():
    _, _, _, actual, _, _, _ = load_data(12)
    assert len(actual) >= 50, f"settlement az: {len(actual)}"
    # her settlement float ve makul aralikta
    for v in actual.values():
        assert -20 < v < 50


def test_bias_varies_by_city():
    _, _, bias_map, _, _, _, _ = load_data(12)
    # Istanbul ecmwf_ifs025 vs meteofrance_seamless bias farkli olmali
    b1 = bias_map.get(("Istanbul", "ecmwf_ifs025"))
    b2 = bias_map.get(("Istanbul", "meteofrance_seamless"))
    assert b1 is not None and b2 is not None
    assert abs(b1 - b2) > 0.5


def test_placed_is_only_open():
    con = sqlite3.connect(BOT_DB)
    cur = con.cursor()
    cur.execute("SELECT count(*) FROM bets WHERE status='open'")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM bets WHERE status='placed'")
    placed = cur.fetchone()[0]
    assert 0 <= placed <= 120  # acik pozisyon degisken (gunluk cap), ama open yok
    if placed > 0:
        cur.execute("SELECT DISTINCT substr(order_id,1,6) FROM bets WHERE status='placed'")
        prefixes = [row[0] for row in cur.fetchall()]
        assert "spread" in prefixes, f"Expected spread prefix, found: {prefixes}"
    con.close()


def test_closed_counts_separate():
    con = sqlite3.connect(BOT_DB)
    cur = con.cursor()
    cur.execute("SELECT count(*) FROM bets WHERE status='closed'")
    closed = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM bets WHERE status IN ('won','lost')")
    wl = cur.fetchone()[0]
    assert closed > wl  # closed won/lost'tan fazla - karisik gorunmemeli
    con.close()


def test_stake_constant():
    assert STAKE == 2.0
