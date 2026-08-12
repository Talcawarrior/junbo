"""Tests for the spread betting placer (executor/spread_placer.py).

Verifies the core mechanics against a temp DB:
  1. last-forecast per (city, metric) selection
  2. first-snapshot price lookup
  3. spread bet opening (radius around forecast center, entry < max)
  4. kayan pencere: forecast shifts -> out-of-window open bets closed
  5. per-day bet limit
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from config.settings import bot_config


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, MarketSnapshot, Portfolio, WeatherForecast, WeatherMarket

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, MarketSnapshot, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()
    bot_config.strategy.current_fee_rate = 0.05


def _day():
    # 2-gun-sonrasi hedef gun (bugun + 2)
    return (datetime.now(timezone.utc) + timedelta(days=2)).date()


def _add_portfolio(session, cash=1000.0):
    from database.models import Portfolio

    if session.query(Portfolio).filter_by(id=1).first() is None:
        session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
        session.commit()


def _add_forecast(session, code, metric, target_day, value, fetched_at):
    from database.models import WeatherForecast

    session.add(
        WeatherForecast(
            market_id="mkt",
            city=code,
            metric=metric,
            target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
            source="gfs_seamless",
            predicted_value=value,
            model_weight=1.0,
            fetched_at=fetched_at,
        )
    )


def _add_market(session, city, metric, target_day, thr, yes_price=0.05, city_code="AAA"):
    from database.models import WeatherMarket

    m = WeatherMarket(
        id=f"{city}-{metric}-{thr}",
        question=f"{city} temp",
        city=city,
        city_code=city_code,
        metric=metric,
        threshold=thr,
        target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
        status="open",
        yes_price=yes_price,
        no_price=1.0 - yes_price,
    )
    session.add(m)
    return m.id


def _add_snapshot(session, city, metric, target_day, thr, price):
    from database.models import MarketSnapshot

    session.add(
        MarketSnapshot(
            market_id=f"{city}-{metric}-{thr}",
            city=city,
            metric=metric,
            target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
            threshold=thr,
            yes_price=price,
            no_price=1.0 - price,
            snapshot_time=datetime(target_day.year, target_day.month, target_day.day, 0, 1, 0),
        )
    )


def test_last_forecast_selection_uses_latest_fetch():
    from database.db import get_session
    from executor.spread_placer import _last_forecast_per_city_metric

    day = _day()
    with get_session() as s:
        _add_forecast(s, "A1", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        _add_forecast(s, "A1", "temperature_max", day, 27.0, datetime(2026, 8, 2, 10, 0))
        s.commit()
        fc = _last_forecast_per_city_metric(s, day)
    mean, std = fc[("A1", "temperature_max")]
    assert mean == pytest.approx(27.0)
    assert std == pytest.approx(1.0)


def test_spread_uses_live_market_price_not_snapshot():
    """2026-08-11: spread bet CANLI weather_markets.yes_price'a gore acilir.

    Eski davranis bayat snapshot fiyatini kullaniyordu (bet 594 entry=0.50
    iken canli 0.0085). Marketin canli fiyati snapshot'tan FARKLI olsa bile
    canli fiyata gore acilir; snapshot hic degerlendirilmez.
    """
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s)
        # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        # Snapshot 0.05, ama CANLI fiyat 0.05 -> canliya gore acilmali
        # (0.05, tahmin 25C icin 22-28 esiklerinde fair degerinin altinda kalir
        #  ve 0.10-0.20 olum bolgeye dusmez; fair-value filtresi gecirir)
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.05)
            _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.15)
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        placed = s.query(Bet).filter(Bet.status == "placed").all()
        entries = [float(b.entry_price or 0) for b in placed]
    assert res["placed"] >= 1, "canli fiyat < max_entry ise bet acilmali"
    assert len(entries) >= 1
    for e in entries:
        assert e == pytest.approx(0.05), f"entry canli fiyat olmali: {e}"


def test_spread_skips_snapshot_low_but_live_high():
    """Canli fiyat max_entry'i asiyorsa bet acilmaz (snapshot dusuk olsa bile)."""
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    bot_config.strategy.spread_max_entry = 0.10
    try:
        with get_session() as s:
            _add_portfolio(s)
            # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87
            _add_calibration(s, "AAA", 0.87)
            _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
            # Snapshot 0.05 (dusuk), CANLI 0.15 (max_entry 0.10 ustunde) -> acilmaz
            for thr in range(22, 29):
                _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.15)
                _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.05)
            s.commit()
            res = place_spread_bets(day, session=s)
            s.commit()
            placed = s.query(Bet).filter(Bet.status == "placed").count()
        assert res["placed"] == 0, "canli fiyat max_entry ustunde ise bet acilmamali"
        assert placed == 0
    finally:
        bot_config.strategy.spread_max_entry = 0.99


def test_place_spread_bets_opens_within_radius():
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s)
        # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87
        _add_calibration(s, "AAA", 0.87)
        # forecast center = 25, radius 3 -> thresholds 22..28
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.05)
            _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.05)
        # city_code map: weather_markets city_code 'TEST' for Testville
        s.commit()

        res = place_spread_bets(day, session=s)
        s.commit()

        placed = s.query(Bet).filter(Bet.status == "placed").all()
    assert res["placed"] >= 3
    assert len(placed) >= 3


def test_spread_limit_respected():
    from database.db import get_session
    from executor.spread_placer import place_spread_bets

    day = _day()
    bot_config.strategy.spread_max_bets_per_day = 2
    try:
        with get_session() as s:
            _add_portfolio(s)
            _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
            for thr in range(22, 29):
                _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.05)
                _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.05)
            s.commit()
            res = place_spread_bets(day, session=s)
        assert res["placed"] <= 2
    finally:
        bot_config.strategy.spread_max_bets_per_day = 30


def test_place_spread_bets_creates_portfolio_when_missing():
    """Portfolio satiri yoksa spread placer otomatik olusturur (0-cash bug)."""
    from database.db import get_session
    from database.models import Portfolio
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        # portfolio YOK (bilerek)
        s.query(Portfolio).delete()
        s.commit()
        # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.05)
            _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.05)
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
        pf_cash = pf.cash_balance if pf else 0.0
    assert pf is not None, "portfolio olusturulmali"
    assert pf_cash > 0
    assert res["placed"] >= 1


def test_merkez_kayinca_betler_acik_kalir():
    """Kullanici karari 2026-08-12: pencere kaydirmasi KAPATILDI. Tahmin kayarsa
    (center 25 -> 28) eski penceredeki acik betler KAPATILMAZ — settlement'a kadar
    tutulur. Backtest: kaydirma her config'de zarar (shift -26 vs noshift +74)."""
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s)
        # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.05)
            _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.05)
        s.commit()
        # ilk run: center 25 -> window 22..28, opens a few bets
        place_spread_bets(day, session=s)
        s.commit()
        opened = s.query(Bet).filter(Bet.status == "placed").count()
        assert opened >= 3

        # tahmin 28'e kaydi -> window 25..31; 22,23,24 ESCKI pencerede ama KAPANMAMALI
        _add_forecast(s, "AAA", "temperature_max", day, 28.0, datetime(2026, 8, 2, 10, 0))
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        closed = s.query(Bet).filter(Bet.status.in_(["closed", "cancelled"])).all()
    assert res["closed"] == 0
    assert len(closed) == 0


def _add_calibration(session, city_code, bias):
    """historical_calibrations'a tek (sehir, model) bias kaydi ekler."""
    from database.models import HistoricalCalibration

    session.add(
        HistoricalCalibration(
            city_code=city_code,
            city=city_code,
            date=datetime(2026, 8, 1),
            metric="temperature_max",
            model="gfs_seamless",
            predicted_value=0.0,
            actual_value=0.0,
            bias=bias,
        )
    )


def test_city_accuracy_uses_abs_bias():
    """_city_accuracy: ortalama MUTLAK bias doner (dusuk = dogru)."""
    from database.db import get_session
    from executor.spread_placer import _city_accuracy

    with get_session() as s:
        _add_calibration(s, "ACC", 0.4)
        _add_calibration(s, "ACC", 0.6)
        _add_calibration(s, "DRIFT", 2.5)
        _add_calibration(s, "DRIFT", 3.5)
        s.commit()
        acc = _city_accuracy(s)
    assert acc["ACC"] == pytest.approx(0.5)
    assert acc["DRIFT"] == pytest.approx(3.0)
    assert acc["ACC"] < acc["DRIFT"]


def test_spread_prefers_accurate_city_over_hot():
    """Kullanici karari 2026-08-11: sehir secimi en sicak DEGIL, tahmini en
    dogru tutan (dusuk |bias|) sehir once gelir. SICAK ama sapan sehir,
    serin ama dogru sehirden SONRA betlenir (limit yetiyorsa)."""
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s, cash=1000.0)
        # "ACC" serin ama DOGRU (bias 0.3) — tahmin 20C
        _add_calibration(s, "ACC", 0.3)
        _add_forecast(s, "ACC", "temperature_max", day, 20.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(17, 24):
            _add_market(s, "Accville", "temperature_max", day, thr, yes_price=0.05, city_code="ACC")
        # "HOT" sicak ama SAPAN (bias 4.0) — tahmin 40C
        _add_calibration(s, "HOT", 4.0)
        _add_forecast(s, "HOT", "temperature_max", day, 40.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(37, 44):
            _add_market(s, "Hotville", "temperature_max", day, thr, yes_price=0.05, city_code="HOT")
        s.commit()

        # max_cities=1 ile SADECE en dogru sehir secilir
        old = bot_config.strategy.spread_max_cities
        bot_config.strategy.spread_max_cities = 1
        try:
            res = place_spread_bets(day, session=s)
            s.commit()
            cities = {b.city for b in s.query(Bet).filter(Bet.status == "placed").all()}
        finally:
            bot_config.strategy.spread_max_cities = old
    assert res["placed"] >= 1
    assert cities == {"Accville"}, f"SICAK ama sapan sehir degil, dogru sehir secilmeli: {cities}"


def test_out_of_selection_bets_kept_open():
    """Kullanici karari 2026-08-12: ilk-15 bias SADECE yeni gun acilisinda sehir
    secer; top-15 disinda kalan sehirlerin ACIK betleri KAPATILMAZ (settlement'a
    kadar tutulur). Eski davranis (2026-08-11) kapatiyordu — kaldirildi."""
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s, cash=10000.0)
        # Secilecek sehir: ACC (dusuk bias -> az sapan), tahmin 25C
        _add_calibration(s, "ACC", 0.3)
        _add_forecast(s, "ACC", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(22, 29):
            _add_market(s, "Accville", "temperature_max", day, thr, yes_price=0.05, city_code="ACC")
        # Secilemeyecek (sapan) sehir: ZZZ bias 4.0, tahmin 40C
        _add_calibration(s, "ZZZ", 4.0)
        _add_forecast(s, "ZZZ", "temperature_max", day, 40.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(37, 44):
            _add_market(s, "Zzzville", "temperature_max", day, thr, yes_price=0.05, city_code="ZZZ")
        s.commit()

        # once ZZZ bet acilir (manuel eski bet gibi) -> sonra place_spread_bets KAPATMAMALI
        from executor.spread_placer import _find_market

        m = _find_market(s, "Zzzville", "temperature_max", day, 40)
        s.add(
            Bet(
                market_id=str(m.id),
                city="Zzzville",
                city_code="ZZZ",
                side="YES",
                amount=2.0,
                price=0.05,
                entry_price=0.05,
                shares=40.0,
                current_price=0.05,
                status="placed",
                placed_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

        old = bot_config.strategy.spread_max_cities
        bot_config.strategy.spread_max_cities = 1
        try:
            res = place_spread_bets(day, session=s)
            s.commit()
            zzz_statuses = [b.status for b in s.query(Bet).filter(Bet.city == "Zzzville").all()]
        finally:
            bot_config.strategy.spread_max_cities = old
    assert res["placed"] >= 1
    assert zzz_statuses, "ZZZ beti var olmali"
    assert all(st in ("placed", "active") for st in zzz_statuses), "top-15 disindaki sehir betleri KAPATILMAMALI"
    assert res["closed"] == 0


def test_past_day_no_bets_opened():
    """Kullanici istegi 2026-08-11: gecmis gune (bugun oncesi) bet acilmaz.
    Polymarket dongusunde eski gunun marketleri hala acik gorunebilir ama
    sonucu belirli; onlara yeni bet acmak gereksiz."""
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    past = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    with get_session() as s:
        _add_portfolio(s)
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", past, 25.0, datetime(2026, 8, 1, 10, 0))
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", past, thr, yes_price=0.05)
        s.commit()
        res = place_spread_bets(past, session=s)
        s.commit()
        placed = s.query(Bet).filter(Bet.status == "placed").count()
    assert res["placed"] == 0, "gecmis gune bet acilmamali"
    assert placed == 0


def test_open_legs_when_center_market_missing():
    """2026-08-11 kullanici karari A: tam-7 zorunlu KALDIRILDI.
    Merkez esigin marketi olmasa da acilabilen ayaklar acilir (kayan pencere
    + max_entry 0.30 ile; tam-7 esigi yok)."""
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s, cash=1000.0)
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        # Merkez (25) HARI 22,23,24,26,27,28 marketleri var — merkez marketi YOK
        for thr in [22, 23, 24, 26, 27, 28]:
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.05)
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        placed = s.query(Bet).filter(Bet.status == "placed").count()
    assert res["placed"] >= 1, "merkez marketi yoksa bile acilabilen ayaklar acilir (tam-7 kaldirildi)"
    assert placed >= 1


def test_fair_value_filter_skips_overpriced_market():
    """Kullanici karari 2026-08-12: market fiyati model probunun (fair value)
    ALTINDA olmali — 'piyasa zaten biliyor' durumunda edge yok, bet acilmaz.

    Tahmin 25C, esik 28C -> P(T>=28) dusuk (fair ~0.02-0.03). Market fiyati
    0.50 -> market fair'dan YUKSEK -> bet ACILMAMALI.
    """
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s, cash=1000.0)
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        # Sadece YUKSEK esikler (27, 28) — tahmin 25C icin fair ~0.02-0.15,
        # market 0.50 fair'dan cok ustunde -> acilmamali. Dusuk esikler
        # (22-24) fair'i yuksek oldugundan testin kapsaminda degil.
        for thr in [27, 28]:
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.50)
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        placed = s.query(Bet).filter(Bet.status == "placed").count()
    assert res["placed"] == 0, "market fiyati fair degerin ustundeyse bet acilmamali"
    assert placed == 0


def test_fair_value_filter_opens_undervalued_market():
    """Market fiyati model probunun ALTINDA ise (ucuz) bet AÇILIR.

    Tahmin 25C, esik 28C -> fair ~0.02-0.03. Market fiyati 0.01 -> fair'dan
    dusuk -> bet AÇILIR (longshot, 0.10-0.20 olum bolge disinda).
    """
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s, cash=1000.0)
        _add_calibration(s, "AAA", 0.87)
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        # Esik 28C: market fiyati 0.01 (fair ~0.02-0.03 altinda) -> AÇILIR
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.01)
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        placed = s.query(Bet).filter(Bet.status == "placed").count()
    assert res["placed"] >= 1, "market fiyati fair degerin altindaysa bet acilmali"
    assert placed >= 1
