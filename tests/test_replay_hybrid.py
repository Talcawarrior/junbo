"""Hibrit METAR-peak replay (2026-11-23) birim testleri — SENTETIK veri.

Canli `jobs.metar_peak.run_metar_peak_bets` kuralini replay'de dogrular:
erken giris (ortalama peak saati + ucuz fiyat), 1-dusus kilidi, aktar
zinciri, look-ahead korumalari (gorulme ani + bilgi-sonrasi fiyat), gunluk
cap, METAR-disi cozumlu sehir haric tutma ve settlement yedegi.

Canli DB'ye DOKUNULMAZ: Archive DB okumadan (`__new__`) kurulur.
Sehir EGLL (Londra) — Kasim'da UTC+0, yerel saat = UTC.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import pytest

from replay.archive import Archive
from replay.clock import ReplayClock
from replay.price_index import PriceIndex
from replay.scheduler import ReplayScheduler, _utc_ts

DAY = "2026-11-20"
CODE = "EGLL"


def _e(hh: int, mm: int = 0) -> float:
    return datetime(2026, 11, 20, hh, mm, tzinfo=timezone.utc).timestamp()


def _dt(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, 20, hh, mm)


#: (saat, dakika, sicaklik) — 12:00'de 14C zirve, 13:00/13:30 dusus (kilit 13:30)
DEFAULT_OBS = [
    (6, 0, 8.0),
    (7, 0, 9.0),
    (8, 0, 10.0),
    (9, 0, 11.0),
    (10, 0, 12.0),
    (11, 0, 13.0),
    (12, 0, 14.0),
    (12, 30, 13.5),
    (13, 0, 13.0),
    (13, 30, 13.0),
    (14, 0, 12.0),
]


def _archive(obs=None, seen_delay_min=5, seen_override=None, poly=None, peak_hours=11.5):
    """DB'siz Archive: yalnizca hibrit replay'in kullandigi alanlar doldurulur."""
    a = Archive.__new__(Archive)
    a.start_day = a.end_day = DAY
    a.markets = {}
    a.range_markets = defaultdict(list)
    a.market_by_id = {}
    a.actuals = {}
    a.metar = defaultdict(list)
    a.metar_seen = defaultdict(list)
    a.forecasts = defaultdict(list)
    a.city_name = {CODE: "London"}
    a.city_lon = {CODE: -0.1}
    a.city_bias = {}
    a.price_rows = []
    a._conns = []
    a._day_obs_cache = {}
    a._live_outcomes = {}
    a._peak_hours = {CODE: [("2026-11-17", peak_hours), ("2026-11-18", peak_hours), ("2026-11-19", peak_hours)]}
    seen_override = seen_override or {}
    for hh, mm, temp in obs or DEFAULT_OBS:
        t = _e(hh, mm)
        seen = seen_override.get((hh, mm), t + seen_delay_min * 60)
        a.metar[CODE].append((t, temp))
        a.metar_seen[CODE].append((t, temp, seen))
    poly = {14: True} if poly is None else poly
    for bucket in (12, 13, 14, 15):
        info = {
            "id": f"m{bucket}",
            "city": "London",
            "city_code": CODE,
            "threshold": float(bucket),
            "day": DAY,
            "market_type": "RANGE",
            "first_seen_ts": _e(0),
            "poly_yes": poly.get(bucket, False) if poly else None,
        }
        a.markets[(CODE, DAY, bucket)] = info
        a.range_markets[(CODE, DAY, bucket)].append(info)
        a.market_by_id[info["id"]] = info
    return a


def _prices(rows):
    """rows: (kaynak, market, saat, dakika, fiyat)."""
    p = PriceIndex()
    for source, mid, hh, mm, price in rows:
        p.add(source, mid, _e(hh, mm), price)
    p.finalize()
    return p


#: Ucuz senaryo: 13 bucket'i 11:30'da 0.30 civari, 14 bucket'i 12:10'da 0.40,
#: kilitten sonra (13:30+) 14 bucket'i 0.80.
CHEAP_PRICES = [
    ("snapshot", "m12", 6, 0, 0.20),
    ("snapshot", "m13", 11, 10, 0.30),
    ("orderbook", "m13", 11, 10, 0.32),
    ("orderbook_bid", "m13", 11, 10, 0.28),
    ("orderbook_bid", "m13", 12, 10, 0.05),
    ("snapshot", "m14", 12, 10, 0.40),
    ("orderbook", "m14", 12, 10, 0.42),
    ("orderbook_bid", "m14", 12, 10, 0.38),
    ("snapshot", "m14", 13, 40, 0.80),
    ("orderbook", "m14", 13, 40, 0.82),
    ("orderbook_bid", "m14", 13, 40, 0.78),
    ("snapshot", "m15", 6, 0, 0.05),
]


def _run(archive, prices, **kw):
    clock = ReplayClock(_dt(0), _dt(0).replace(day=21), tick_minutes=10)
    # bu dosyadaki senaryolar hibritin ERKEN girisini test eder; canli varsayilan
    # 2026-11-23'ten beri KAPALI oldugu icin acikca verilmeyen testlerde acilir.
    kw.setdefault("metar_early", True)
    sched = ReplayScheduler(archive, prices, clock, strategy="metar", metar_mode="hybrid", settle="polymarket", **kw)
    return sched.run()


def test_replay_default_follows_live_early_setting():
    """metar_early verilmezse replay CANLI `metar_early_entry` ayarini izler."""
    from unittest.mock import patch

    from config.settings import bot_config

    clock = ReplayClock(_dt(0), _dt(1))
    with patch.object(bot_config.strategy, "metar_early_entry", False):
        assert ReplayScheduler(_archive(), PriceIndex(), clock, metar_mode="hybrid").metar_early is False
    with patch.object(bot_config.strategy, "metar_early_entry", True):
        assert ReplayScheduler(_archive(), PriceIndex(), clock, metar_mode="hybrid").metar_early is True
    # acik parametre canli ayari ezer
    with patch.object(bot_config.strategy, "metar_early_entry", True):
        assert ReplayScheduler(_archive(), PriceIndex(), clock, metar_early=False).metar_early is False


def test_utc_ts_ignores_machine_timezone():
    """Naive replay saati UTC'dir (eski hata: makine UTC+3 -> 3 saat eski veri)."""
    naive = datetime(2026, 11, 20, 12, 0)
    assert _utc_ts(naive) == datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp()


def test_price_index_latest_freshest_not_before():
    p = _prices([("snapshot", "x", 10, 0, 0.30), ("orderbook_mid", "x", 10, 20, 0.50), ("snapshot", "x", 11, 0, 0.70)])
    assert p.latest("x", _e(10, 30), "snapshot") == 0.30
    assert p.latest("x", _e(9, 59), "snapshot") is None  # gelecek fiyat gorulmez
    assert p.latest("x", _e(10, 50), "snapshot", max_age=10 * 60) is None  # bayat
    assert p.freshest("x", _e(10, 30), ("snapshot", "orderbook_mid")) == 0.50  # en yeni kazanir
    # bilgi zamani (10:25) oncesi fiyatla dolum YASAK
    assert p.freshest("x", _e(10, 30), ("snapshot", "orderbook_mid"), not_before=_e(10, 25)) is None


def test_archive_avg_peak_hour_and_seen_filter():
    a = _archive(seen_override={(12, 0): _e(12, 40)})
    assert a.avg_peak_hour(CODE, DAY) == pytest.approx(11.5)
    assert a.avg_peak_hour(CODE, "2026-11-18") is None  # yalnizca ONCEKI gunler, >= 3 gun
    # 12:36: 12:30 gozlemi (13.5C, 12:35'te goruldu) VAR; 12:00 (14C) 12:40'ta goruldu -> YOK
    rows = a.metar_seen_day(CODE, DAY, _e(12, 36), 0.0)
    assert max(t for _ts, t in rows) == 13.5
    assert a.metar_info_ts(CODE, DAY, _e(12, 36), 0.0) == _e(12, 35)
    # 12:30'da 12:30 gozlemi de henuz gorulmedi
    assert max(t for _ts, t in a.metar_seen_day(CODE, DAY, _e(12, 30), 0.0)) == 13.0


def test_early_entry_then_aktar_chain():
    """Erken ACIK: 11:30'da 13'e ucuz giris, 14 gelince 13 kapatilir 14'e girilir."""
    res = _run(_archive(), _prices(CHEAP_PRICES))
    bets = sorted(res.bets, key=lambda b: b.opened_at)
    assert [b.bucket for b in bets] == [13, 14]
    first, second = bets
    assert first.open_trigger == "early" and first.close_kind == "aktar"
    assert first.opened_at == _dt(11, 30) and first.entry_price == pytest.approx(0.32)
    assert first.close_price == pytest.approx(0.05) and first.realized_pnl < 0
    assert second.open_trigger == "early+aktar" and second.close_kind == "settlement"
    assert second.opened_at == _dt(12, 10) and second.entry_price == pytest.approx(0.42)
    assert second.won and second.realized_pnl > 0


def test_early_disabled_waits_for_lock():
    """Erken KAPALI: yalnizca 1-dusus kilidi (13:30 gozlemi 13:35'te gorulur)."""
    res = _run(_archive(), _prices(CHEAP_PRICES), metar_early=False)
    assert len(res.bets) == 1
    bet = res.bets[0]
    assert bet.bucket == 14 and bet.open_trigger == "lock"
    assert bet.opened_at == _dt(13, 40) and bet.entry_price == pytest.approx(0.82)
    assert bet.won


def test_expensive_bucket_waits_for_lock_even_when_early_enabled():
    """Erken adayin fiyati > EARLY_MAX_PRICE ise kilit beklenir (canli hibrit esigi)."""
    res = _run(_archive(), _prices(CHEAP_PRICES), early_max_price=0.0)
    assert len(res.bets) == 1
    assert res.bets[0].open_trigger == "lock" and res.bets[0].opened_at == _dt(13, 40)


def test_seen_time_delays_aktar_no_lookahead():
    """14C gozlemi 12:40'ta goruldu -> aktar 12:10'da DEGIL, 12:40'ta.

    Esik kontrolu, satis ve dolum icin 12:40 SONRASI fiyat gerekir (bilgi zamani).
    """
    prices = _prices(
        CHEAP_PRICES
        + [
            ("snapshot", "m14", 12, 40, 0.44),
            ("orderbook", "m14", 12, 40, 0.45),
            ("orderbook_bid", "m13", 12, 40, 0.04),
        ]
    )
    res = _run(_archive(seen_override={(12, 0): _e(12, 40)}), prices)
    first, second = sorted(res.bets, key=lambda b: b.opened_at)
    assert first.close_kind == "aktar" and first.close_price == pytest.approx(0.04)
    assert second.bucket == 14 and second.opened_at == _dt(12, 40)
    assert second.entry_price == pytest.approx(0.45)  # bilgi-sonrasi ask, 12:10'daki 0.42 DEGIL


def test_no_post_info_price_defers_decision():
    """Bilgi-sonrasi fiyat yoksa esik/aktar ertelenir; kilit gelince girilir."""
    prices = _prices(CHEAP_PRICES + [("orderbook", "m14", 12, 40, 0.45)])
    res = _run(_archive(seen_override={(12, 0): _e(12, 40)}), prices)
    bets = sorted(res.bets, key=lambda b: b.opened_at)
    # 12:40'ta m14 icin bilgi-sonrasi DB fiyati yok -> bekle; 13:40 kilit + 0.80 fiyat
    assert bets[-1].bucket == 14 and bets[-1].opened_at == _dt(13, 40)
    assert bets[-1].open_trigger == "lock+aktar"


def test_pre_info_price_is_rejected():
    """Bilgi aninda (11:05) sonra kaydedilmis fiyat yoksa giris YAPILMAZ."""
    prices = _prices([("snapshot", "m13", 11, 0, 0.30), ("orderbook", "m13", 11, 0, 0.32)])
    res = _run(_archive(), prices)
    assert res.bets == []
    # duyarlilik modu (info_prices=False) ayni fiyatla girer — iyimser/look-ahead
    res_opt = _run(_archive(), prices, info_prices=False)
    assert [b.bucket for b in res_opt.bets] == [13]


def test_daily_cap_blocks_reentry_but_not_aktar():
    res = _run(_archive(), _prices(CHEAP_PRICES), metar_max_per_day=1)
    assert len(res.bets) == 1
    assert res.bets[0].bucket == 13 and res.bets[0].close_kind == "aktar"


def test_non_metar_resolution_city_skipped(monkeypatch):
    import jobs.metar_peak as mp

    monkeypatch.setattr(mp, "NON_METAR_RESOLUTION", {CODE: "test"})
    assert _run(_archive(), _prices(CHEAP_PRICES)).bets == []
    assert _run(_archive(), _prices(CHEAP_PRICES), include_non_metar=True).bets


def test_settle_falls_back_to_archive_metar_bucket():
    """Polymarket sonucu yoksa gunun arsiv METAR max'i (14C) kullanilir."""
    res = _run(_archive(poly={}), _prices(CHEAP_PRICES), metar_early=False)
    assert len(res.bets) == 1 and res.bets[0].won
    assert res.bets[0].price_source.endswith("settle=metar_arsiv")


def test_fixed_stake_mode_uses_live_metar_stake():
    """fixed mod METAR stake'i canli config'ten okur (eski sabit $3 drift)."""
    import jobs.metar_peak as mp
    from config.settings import bot_config
    from replay.main import _apply_stake_mode, _restore_stakes, _snapshot_stakes

    snap = _snapshot_stakes()
    prev = bot_config.strategy.metar_stake_usd
    try:
        bot_config.strategy.metar_stake_usd = 2.5
        _apply_stake_mode("fixed", 1000.0)
        assert mp.METAR_STAKE == pytest.approx(2.5)
    finally:
        bot_config.strategy.metar_stake_usd = prev
        _restore_stakes(snap)


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        ReplayScheduler(_archive(), PriceIndex(), ReplayClock(_dt(0), _dt(1)), metar_mode="bilinmeyen")
