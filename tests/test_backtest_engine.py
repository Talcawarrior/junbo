"""Backtest engine dogrulama: 12 gun tam evren, won/lost/closed ayri, bias duzeltmeli.

2026-11-23: bu dosyadaki veri testleri KODU degil canli bot.db'yi dogrular;
`live_bot_db` fixture'i (conftest) ile uretim makinesine kapilidir ve DB
yalnizca salt-okunur acilir (eski `sqlite3.connect(yol)` rw modda yol yoksa
BOS bot.db uretiyordu).
"""

import sqlite3

import pytest

from backtest.engine import STAKE, load_data

#: Acik (placed) pozisyonlarin ait olabilecegi stratejiler (order_id onekleri).
#: 2026-11-18 METAR-only (SPREAD_ENABLED=false): acik pozisyonlarda spread
#: SART DEGIL; eski spread pozisyonlari settlement'a kadar kalabilir.
#: Bilinmeyen onek = beklenmeyen bir yoldan bet acilmis demektir (bug).
KNOWN_OPEN_PREFIXES = {"spread", "metar_"}


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


@pytest.mark.usefixtures("live_bot_db")
def test_load_data_has_settlement():
    _, _, _, actual, _, _, _ = load_data(12)
    assert len(actual) >= 50, f"settlement az: {len(actual)}"
    # her settlement float ve makul aralikta
    for v in actual.values():
        assert -20 < v < 50


@pytest.mark.usefixtures("live_bot_db")
def test_bias_varies_by_city():
    _, _, bias_map, _, _, _, _ = load_data(12)
    # Istanbul ecmwf_ifs025 vs meteofrance_seamless bias farkli olmali
    b1 = bias_map.get(("Istanbul", "ecmwf_ifs025"))
    b2 = bias_map.get(("Istanbul", "meteofrance_seamless"))
    assert b1 is not None and b2 is not None
    assert abs(b1 - b2) > 0.5


def test_placed_is_only_open(live_bot_db):
    con = _ro(live_bot_db)
    try:
        cur = con.cursor()
        cur.execute("SELECT count(*) FROM bets WHERE status='open'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM bets WHERE status='placed'")
        placed = cur.fetchone()[0]
        assert 0 <= placed <= 120  # acik pozisyon degisken (gunluk cap), ama open yok
        if placed > 0:
            cur.execute("SELECT DISTINCT substr(order_id,1,6) FROM bets WHERE status='placed'")
            prefixes = [row[0] for row in cur.fetchall()]
            # 2026-11-23: eski kontrol "spread" onekini ZORUNLU tutuyordu; METAR-only
            # stratejide (2026-11-18) tek acik METAR betiyle yanlis FAIL veriyordu.
            unknown = sorted(str(p) for p in prefixes if p not in KNOWN_OPEN_PREFIXES)
            assert not unknown, f"bilinmeyen strateji onekli acik pozisyon: {unknown} (tum onekler: {prefixes})"
    finally:
        con.close()


def test_closed_counts_separate(live_bot_db):
    con = _ro(live_bot_db)
    try:
        cur = con.cursor()
        cur.execute("SELECT count(*) FROM bets WHERE status='closed'")
        closed = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM bets WHERE status IN ('won','lost')")
        wl = cur.fetchone()[0]
        assert closed > wl  # closed won/lost'tan fazla - karisik gorunmemeli
    finally:
        con.close()


def test_stake_constant():
    assert STAKE == 2.0
