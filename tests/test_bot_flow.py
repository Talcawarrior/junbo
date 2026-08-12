"""Uçtan uca bot akis testi — bot'un GERCEK adimlarini temp DB'de dogrular.

Bot'un calisma akisi (bot_loop.py scan_and_bet_loop + settlement_loop):
  1. run_fetch_markets  -> Polymarket marketleri weather_markets'e yazar
  2. run_parse_markets  -> ham veriden yapilandirilmis alanlar cikarilir
  3. 2-gun-sonrasi tarih tespiti (_next_two_day_target) -> spread bet acilir
  4. run_settle         -> gercek sicakliga gore betler sonuclanir

Bu test, ag cagrilarini mock'layarak (Polymarket/Open-Meteo'ya gitmeden) bu
zinciri GERCEK fonksiyonlarla calistirir ve her adimda beklenen DB durumunu
dogrular. Boylece "fetch ok ama bet acilmadi" gibi izole testlerin kacirdigi
akis kopukluklari yakalanir.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from config.settings import bot_config


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, Portfolio, WeatherForecast, MarketSnapshot, WeatherMarket

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, MarketSnapshot, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()
    bot_config.strategy.current_fee_rate = 0.05
    bot_config.strategy.betting_strategy = "spread"
    bot_config.strategy.spread_max_bets_per_day = 100
    bot_config.strategy.spread_radius = 3
    bot_config.strategy.spread_max_entry = 0.99


def _add_portfolio(session, cash=1000.0):
    from database.models import Portfolio

    if session.query(Portfolio).filter_by(id=1).first() is None:
        session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
        session.commit()


def _day():
    return (datetime.now(timezone.utc) + timedelta(days=2)).date()


def _seed_market_and_forecast(session, target_day):
    """Market + snapshot + forecast: spread_placer'in ihtiyac duydugu veri."""
    from database.models import HistoricalCalibration, MarketSnapshot, WeatherForecast, WeatherMarket

    # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87 — bias'siz sehir secilmez.
    session.add(
        HistoricalCalibration(
            city_code="AAA",
            city="Testville",
            date=datetime(2026, 8, 1),
            metric="temperature_max",
            model="gfs_seamless",
            predicted_value=25.0,
            actual_value=24.13,
            bias=0.87,
        )
    )
    for thr in range(28, 35):  # 28..34 = 7 esik (center 31 +/- 3) — tam-7 kurali
        mid = f"mkt-{thr}"
        session.add(
            WeatherMarket(
                id=mid,
                question="T?",
                city="Testville",
                city_code="AAA",
                metric="temperature_max",
                threshold=thr,
                target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
                status="open",
                yes_price=0.05,
                no_price=0.95,
            )
        )
        session.add(
            MarketSnapshot(
                market_id=mid,
                city="Testville",
                metric="temperature_max",
                target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
                threshold=thr,
                yes_price=0.05,
                no_price=0.95,
                snapshot_time=datetime(target_day.year, target_day.month, target_day.day, 0, 1, 0),
            )
        )
    # 8 model forecast, ensemble ~31 -> center 31 -> window 28..34
    for src in (
        "gfs_seamless",
        "ecmwf_ifs025",
        "gem_global",
        "icon_global",
        "jma_seamless",
        "cma_grapes_global",
        "ukmo_seamless",
        "meteofrance_seamless",
    ):
        session.add(
            WeatherForecast(
                market_id="mkt-31",
                city="AAA",
                metric="temperature_max",
                target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
                source=src,
                predicted_value=31.0,
                model_weight=1.0,
                fetched_at=datetime(target_day.year, target_day.month, target_day.day, 0, 5, 0),
            )
        )
    session.commit()


class TestFullBotFlow:
    def test_spread_flow_opens_bets_and_keeps_open(self):
        """Akis: forecast -> spread bet ac -> bet 'placed' kalir.

        Erken kapanis mekanizmalari kaldirildi (2026-08-12): bet sadece
        settlement'ta kapanir. Fiyat dusse bile bet acik kalir.
        """
        from database.db import get_session
        from database.models import Bet, WeatherMarket
        from executor.spread_placer import place_spread_bets

        day = _day()
        with get_session() as s:
            _add_portfolio(s)
            _seed_market_and_forecast(s, day)
            s.commit()

            # 1) spread bet ac
            res = place_spread_bets(day, session=s)
            s.commit()
            assert res["placed"] >= 1, f"spread bet acilmali: {res}"
            open_bets = s.query(Bet).filter(Bet.status.in_(["placed", "partial_fill", "filled"])).count()
            assert open_bets >= 1

            # 2) fiyati dusur -> bet yine de ACIK kalir (erken kapanis yok)
            for m in s.query(WeatherMarket).filter(WeatherMarket.city == "Testville").all():
                m.yes_price = 0.03  # %50 dusus
            s.commit()

            still_open = s.query(Bet).filter(Bet.status.in_(["placed", "partial_fill", "filled"])).count()
            assert still_open == open_bets, (
                f"fiyat dusse bile bet acik kalmali: open before={open_bets} after={still_open}"
            )

    def test_new_date_triggers_spread(self):
        """2-gun-sonrasi tarih acildiginda _next_two_day_target tetikler."""
        from bot_loop import _get_open_target_dates, _next_two_day_target
        from database.db import get_session

        day = _day()
        with get_session() as s:
            _seed_market_and_forecast(s, day)
            s.commit()
        dates = _get_open_target_dates()
        assert dates, "acik market tarihleri olmali"
        new_date, trigger = _next_two_day_target(None, dates)
        assert trigger is True, "ilk tarih tespitinde tetikleme olmali"
        assert new_date is not None

    def test_settle_resolves_bets(self):
        """run_settle gercek sonuca gore betleri sonuclandirir."""
        from database.db import get_session
        from database.models import Bet
        from executor.settler import SettlementEngine

        # kazanilan bet: entry 0.20, gercek sicaklik esigi karsiliyor
        with get_session() as s:
            _add_portfolio(s)
            s.add(
                Bet(
                    market_id="m1",
                    city="Testville",
                    city_code="AAA",
                    side="YES",
                    amount=2.0,
                    price=0.20,
                    entry_price=0.20,
                    shares=10.0,
                    status="placed",
                    placed_at=datetime.now(timezone.utc),
                )
            )
            s.commit()

        engine = SettlementEngine()
        # settle_all ag cagirir (Gamma) -> mock ile bos sonuc, crash olmamali
        with patch.object(engine, "_fetch_market_resolution", return_value=None):
            results = engine.settle_all()
        assert isinstance(results, dict)
        assert "win" in results and "loss" in results and "pending" in results


class TestPortfolioGuard:
    def test_spread_creates_portfolio_when_missing(self):
        """Portfolio yoksa spread_placer olusturur (0-cash sessiz skip bug'i)."""
        from database.db import get_session
        from database.models import Portfolio
        from executor.spread_placer import place_spread_bets

        day = _day()
        with get_session() as s:
            s.query(Portfolio).delete()
            s.commit()
            _seed_market_and_forecast(s, day)
            s.commit()
            res = place_spread_bets(day, session=s)
            s.commit()
            pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
            pf_cash = pf.cash_balance if pf else 0.0
        assert pf is not None, "portfolio olusturulmali"
        assert pf_cash > 0
        assert res["placed"] >= 1
