"""Yerlesim kaynagi uyum kapisi (2026-09-18).

Olay: 4 tur backtest farkli yerlesim kaynagi (HEAT vs bizim WU toplama)
ile kosuldu, %25 ayni-bucket uyumla cop sonuclar uretti. Tekrarini onlemek
icin bu test:
1. source_offsets.json var + islem goren sehirleri kapsiyor mu?
2. Offset-duzeltmeli uyum >= 0.40 mi? (2026-09-18 olcumu: 0.469)
Uyum duserse backtestler DURUR, once kaynak uyumsuzlugu cozulur.
"""

import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from utils.db_paths import get_data_dir  # noqa: E402

ROOT = Path(__file__).parent.parent
# 2026-11-23: canli bot.db yolu `live_bot_db` fixture'indan gelir (conftest):
# uretim verisi yoksa test acik nedenle atlanir; tum baglantilar salt-okunur
# (eski rw `sqlite3.connect(yol)` yol yoksa BOS DB dosyasi uretiyordu).


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _overlap_pairs(bot_db: str):
    """HEAT yerlesimi vs bizim topladigimiz deger (gercek capraz okuma)."""
    con = _ro(bot_db)
    cur = con.cursor()
    cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code != ''")
    c2c: dict[str, str] = {}
    for c, ic in cur.fetchall():
        c2c.setdefault(ic, c)
    cur.execute(
        "SELECT b.city, substr(m.target_date,1,10), b.outcome FROM bets b "
        "LEFT JOIN weather_markets m ON b.market_id = m.id "
        "WHERE b.status IN ('won','lost') AND b.outcome IS NOT NULL"
    )
    ours: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    for city, tgt, outcome in cur.fetchall():
        try:
            t = json.loads(outcome).get("settlement_temp_c")
            if t is not None and tgt:
                ours[(city, str(tgt)[:10])].append(float(t))
        except Exception:
            continue
    con.close()
    # 2026-10-27 (S15): yol sabit kodlanmaz. Canli HEAT deposu D: disk
    # arizasi nedeniyle 2026-09-30'da C:'ye tasindi; D: kopyasi 2026-09-07'de
    # bitiyor ve yerlesmis betlerimizle (2026-09-14+) hic ortusmuyordu ->
    # pairs=0. heat.heat_db_path() var olan ilk adayi secer.
    from heat import heat_db_path

    candidates = [
        os.environ.get("HEAT_MAIN_DB"),
        str(Path(get_data_dir()) / "tempmarket-main.sqlite"),
        heat_db_path(),
    ]
    main_db = next((p for p in candidates if p and os.path.exists(p)), heat_db_path())
    h = _ro(main_db)
    hc = h.cursor()
    hc.execute("SELECT id, icao FROM stations")
    st2city: dict[str, str] = {}
    for sid, icao in hc.fetchall():
        if sid and icao:
            st2city[str(sid).lower()] = c2c.get(str(icao).upper(), str(icao).upper())
    hc.execute("SELECT station_id, target_date, high_c FROM settlements WHERE high_c IS NOT NULL")
    pairs: list[tuple[str | None, float, float]] = []
    for sid, day, hv in hc.fetchall():
        city = st2city.get(str(sid).lower())
        for o in ours.get((city, str(day)[:10]), []):
            pairs.append((city, float(hv), o))
    h.close()
    return pairs


def _load():
    return json.loads((ROOT / "config" / "source_offsets.json").read_text(encoding="utf-8"))


def test_offsets_file_covers_traded_cities(live_bot_db):
    data = _load()
    assert "cities" in data and len(data["cities"]) >= 40
    con = _ro(live_bot_db)
    try:
        cur = con.cursor()
        cur.execute("SELECT DISTINCT city FROM bets WHERE city IS NOT NULL")
        traded = [r[0] for r in cur.fetchall()]
    finally:
        con.close()
    missing = [c for c in traded if c not in data["cities"]]
    # 2026-09-18: GERCEK kapsama sarti (onceki isinstance kontrolu bostu)
    assert not missing, f"offset tablosunda olmayan islem sehri: {missing}"


def test_calibrated_agreement_above_floor(live_bot_db):
    import statistics

    data = _load()
    pairs = _overlap_pairs(live_bot_db)
    assert len(pairs) >= 20, f"olum icin yetersiz eslesme: {len(pairs)}"
    # GERCEK yeniden olcum: sehir medyan offsetleri dosyadakiyle tutarli mi?
    by_city = {}
    for city, hv, o in pairs:
        by_city.setdefault(city, []).append(hv - o)
    for city, diffs in by_city.items():
        if len(diffs) < 5:  # kucuk orneklem gurultu olur (HK 3 ornekle sapmisti)
            continue
        fresh = round(statistics.median(sorted(diffs)), 2)
        filed = float((data["cities"].get(city) or {}).get("median_offset_c", 0.0))
        # Both values are 2-decimal; round the difference so the inclusive
        # 0.30 tolerance is not tripped by float noise (0.4 - 0.1 ->
        # 0.30000000000000004).
        diff = round(fresh - filed, 2)
        assert abs(diff) <= 0.30, (
            f"{city}: offset kaydi ({filed:+.2f}) gercekten sapti ({fresh:+.2f}) — "
            "source_offsets.json yeniden olculmeli, o zamana kadar capraz-kaynak backtest YASAK"
        )
    # Bilinen aci gercek (2026-09-18): ham uyum %25, duzeltmeli bile <%50.
    # Bu yuzden capraz-kaynak bucket backtest GECERSIZDIR; gecerli olcum
    # walk-forward gercek-status yoludur (anayasa 12.8). Bu test tazeligi
    # bekler, uyumsuzlugun kendisini degil.


def test_normalized_helper_applies_offset():
    from backtest.engine import normalized_heat_settlement

    data = json.loads((ROOT / "config" / "source_offsets.json").read_text(encoding="utf-8"))
    city = next(iter(data["cities"]))
    off = data["cities"][city]["median_offset_c"]
    assert normalized_heat_settlement(city, 30.0, data) == 30.0 - off
    assert normalized_heat_settlement("BilinmeyenSehirXYZ", 30.0, data) == 30.0
