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
    assert fc[("A1", "temperature_max")] == pytest.approx(27.0)


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
        _add_forecast(s, "AAA", "temperature_max", day, 25.0, datetime(2026, 8, 1, 10, 0))
        # Snapshot 0.05, ama CANLI fiyat 0.15 -> canliya gore acilmali
        for thr in range(22, 29):
            _add_market(s, "Testville", "temperature_max", day, thr, yes_price=0.15)
            _add_snapshot(s, "Testville", "temperature_max", day, thr, 0.05)
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        placed = s.query(Bet).filter(Bet.status == "placed").all()
        entries = [float(b.entry_price or 0) for b in placed]
    assert res["placed"] >= 1, "canli fiyat < max_entry ise bet acilmali"
    assert len(entries) >= 1
    for e in entries:
        assert e == pytest.approx(0.15), f"entry canli fiyat olmali: {e}"


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


def test_kayan_pencere_closes_out_of_window():
    from database.db import get_session
    from database.models import Bet
    from executor.spread_placer import place_spread_bets

    day = _day()
    with get_session() as s:
        _add_portfolio(s)
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

        # tahmin 28'e kaydi -> window 25..31; 22,23,24 kapanmali
        _add_forecast(s, "AAA", "temperature_max", day, 28.0, datetime(2026, 8, 2, 10, 0))
        s.commit()
        res = place_spread_bets(day, session=s)
        s.commit()
        closed = s.query(Bet).filter(Bet.status.in_(["closed", "cancelled"])).all()
    assert res["closed"] >= 1
    assert len(closed) >= 1
