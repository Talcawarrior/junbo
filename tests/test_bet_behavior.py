"""BetPlacer davranis testleri — tum acilis kurallari (mock yok, gercek DB).

Kurallar (README/ANAYASA + kod):
- Kapanis 24:00 UTC = target_date (12:00 etiketi) + 12h
- Acilis kisiti: kapanis > now+30dk VE kapanis <= now+20h
  (SQLite-safe: target_date > now-11h30dk VE target_date <= now+8h)
- YES-only, [0.10, 0.95) fiyat gate
- Ayni markete duplicate bet yok
- SL sonrasi ayni markete 6 saat re-entry guard
- Exposure room + nakit siniri
- Pencere (04:00-23:30 UTC) disinda bet yok
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, Portfolio, WeatherForecast, WeatherMarket

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()
    from config.settings import bot_config

    bot_config.strategy.current_fee_rate = 0.05


def _td(hours_ahead):
    """Kapanis (target+12h) now+20h icinde kalacak target_date."""
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(hours=hours_ahead)


def _add_market(session, mid, yes_price, td, city="Testville", metric="temperature_max"):
    from database.models import WeatherMarket

    session.add(
        WeatherMarket(
            id=mid,
            question="T?",
            city=city,
            city_code="TEST",
            metric=metric,
            threshold=25.0,
            target_date=td,
            latitude=41.0,
            longitude=29.0,
            market_type="HIGH",
            yes_price=yes_price,
            no_price=round(1.0 - yes_price, 4),
            status="open",
        )
    )


def _add_portfolio(session, cash=5000.0):
    from database.models import Portfolio

    session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
    session.commit()


class TestOpenMarketFilter:
    """place_all_pending'in acilis filtresi: kapanis 24:00 = target+12h."""

    def test_today_market_after_noon_is_still_openable(self):
        """OGLEDEN SONRA bile bugunku market (kapanis 24:00) acilabilir olmali.

        2026-08-08 bug: target_date 12:00 etiketi kapanis saniyordu ->
        saat 12:30 UTC'den sonra hicbir markete bet acilamiyordu.
        """
        from executor.bet_placer import BetPlacer

        # target_date = now - 1h -> etiket gecti ama kapanis (target+12h) gecmedi
        td = _td(-1)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.60, td)
            s.commit()

        placed = BetPlacer().place_all_pending()
        assert placed >= 1, "target_date etiketi gecti diye market acilamaz olmamali - kapanis target+12h (24:00 UTC)"

    def test_market_past_close_not_openable(self):
        """Kapanis (target+12h) gectiyse bet acilmaz."""
        from executor.bet_placer import BetPlacer

        td = _td(-13)  # target -13h -> kapanis -1h (gecti)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m2", 0.60, td)
            s.commit()

        placed = BetPlacer().place_all_pending()
        assert placed == 0, "kapanis gecti, bet acilmamali"

    def test_future_market_beyond_20h_not_openable(self):
        """Kapanisa 20h+ kala bet acilmaz (fiyat henuz oturmadi)."""
        from executor.bet_placer import BetPlacer

        td = _td(9)  # kapanis = now + 21h > now + 20h -> disarida
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m3", 0.60, td)
            s.commit()

        placed = BetPlacer().place_all_pending()
        assert placed == 0, "kapanisa 20h+ kala bet acilmamali"


class TestBetGates:
    def test_high_price_rejected(self):
        """yes_price >= 0.95 gate (0.99 da dahil)."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.98, td)
            s.commit()
            assert BetPlacer().place_all_pending() == 0

    def test_low_price_rejected(self):
        """yes_price < 0.10 gate."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m2", 0.05, td)
            s.commit()
            assert BetPlacer().place_all_pending() == 0

    def test_duplicate_market_no_second_bet(self):
        """Ayni markete ikinci bet acilmaz."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.60, td)
            s.commit()

        placer = BetPlacer()
        assert placer.place_all_pending() == 1
        assert placer.place_all_pending() == 0, "duplicate markete bet acilmamali"

    def test_insufficient_cash_rejects(self):
        """Nakit yetersizse bet acilmaz."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s, cash=1.0)  # flat_bet 10 > nakit 1
            _add_market(s, "m1", 0.60, td)
            s.commit()
            assert BetPlacer().place_all_pending() == 0

    def test_one_bet_per_group(self):
        """Ayni (city, date, metric) grubunda tek bet."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.60, td)
            _add_market(s, "m2", 0.55, td)  # ayni grup, daha dusuk
            s.commit()
            assert BetPlacer().place_all_pending() == 1

    def test_different_groups_separate_bets(self):
        """Farkli sehir -> ayri bet."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.60, td, city="CityA")
            _add_market(s, "m2", 0.58, td, city="CityB")
            s.commit()
            assert BetPlacer().place_all_pending() == 2


class TestRotationBehavior:
    def test_rotation_only_on_threshold_improvement(self):
        """Fiyat iyilesmesi threshold (%15) altindaysa rotation yok."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.50, td)
            s.commit()
            placer = BetPlacer()
            assert placer.place_all_pending() == 1

            # m1 fiyati 0.55'e ciksin (iyilesme %10 < %15 threshold) + m2 0.60
            from database.models import WeatherMarket

            s.query(WeatherMarket).filter_by(id="m1").update({"yes_price": 0.55})
            s.add(
                __import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket(
                    id="m2",
                    question="T?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=26.0,
                    target_date=td,
                    latitude=41.0,
                    longitude=29.0,
                    market_type="HIGH",
                    yes_price=0.60,
                    no_price=0.40,
                    status="open",
                )
            )
            s.commit()
            # Ayni gruba ikinci market eklendi; eski bet m1'de
            assert placer.place_all_pending() == 0  # yeni bet yok (m1 hala en yuksek degil mi kontrol)

    def test_rotation_closes_old_bet_when_new_higher(self):
        """Yeni market cok daha yuksek fiyatliysa eski bet kapanir, yenisi acilir."""
        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.40, td)
            s.commit()
            placer = BetPlacer()
            assert placer.place_all_pending() == 1

            from database.models import Analysis, Bet, WeatherMarket

            # m1'in fiyati 0.20'ye dus; m2 0.70 ile gruba girsin
            s.query(WeatherMarket).filter_by(id="m1").update({"yes_price": 0.20})
            from database.models import WeatherMarket as WM

            s.add(
                WM(
                    id="m2",
                    question="T?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=26.0,
                    target_date=td,
                    latitude=41.0,
                    longitude=29.0,
                    market_type="HIGH",
                    yes_price=0.70,
                    no_price=0.30,
                    status="open",
                )
            )
            # _check_rotation_edge analysis ister -> m2 icin yeterli edge'li kayit
            from datetime import datetime, timezone

            s.add(
                Analysis(
                    market_id="m2",
                    edge=0.10,
                    estimated_probability=0.75,
                    market_implied_prob=0.65,
                    avg_forecast_value=26.0,
                    std_forecast_value=2.0,
                    num_sources=8,
                    analyzed_at=datetime.now(timezone.utc),
                )
            )
            s.commit()

            placer.place_all_pending()
            old_bet = s.query(Bet).filter(Bet.market_id == "m1").first()
            new_bet = s.query(Bet).filter(Bet.market_id == "m2", Bet.status.in_(("placed", "pending"))).first()
            assert new_bet is not None, "yeni yuksek fiyatliya bet acilmali"
            assert old_bet.status == "closed", "eski bet rotasyonla kapanmali"
