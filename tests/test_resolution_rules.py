"""Cozunurluk kurali tablosu dogrulama (2026-09-16)."""

import json
from pathlib import Path

RULES = Path(__file__).parent.parent / "config" / "resolution_rules.json"


def _load():
    return json.loads(RULES.read_text(encoding="utf-8"))


def test_table_exists_and_covers_traded_cities(live_bot_db):
    """Kural tablosu canli islem sehirlerini kapsiyor mu (uretim makinesi kapili).

    2026-11-23: `live_bot_db` (conftest) — canli DB yoksa acik nedenle atlanir;
    baglanti salt-okunur (eski rw connect yol yoksa BOS bot.db uretiyordu).
    """
    import sqlite3

    rules = _load()
    assert len(rules) >= 40
    con = sqlite3.connect(f"file:{live_bot_db}?mode=ro", uri=True, timeout=30)
    try:
        cur = con.cursor()
        cur.execute("SELECT DISTINCT city_code FROM bets WHERE city_code IS NOT NULL AND city_code != ''")
        missing = [r[0] for r in cur.fetchall() if r[0] not in rules]
    finally:
        con.close()
    assert not missing, f"kural tablosunda olmayan sehirler: {missing}"


def test_each_rule_has_station_and_url():
    for code, r in _load().items():
        assert r.get("station") == code
        assert r.get("resolution_url", "").startswith("http"), code
        assert r.get("rounding") == "floor_1C_bucket", code
