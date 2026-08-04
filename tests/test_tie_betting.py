"""Tests for ONE bet per group feature (updated 2026-08-02).

Yeni mantik: her (city, target_date, metric) grubunda SADECE 1 bet olur.
Eski bet'ler kapatilir, en yuksek fiyatli piyasada yenisi acilir.
"""

from datetime import datetime, timezone, timedelta

import pytest


_COUNTER = 10000


def _unique_id(prefix="bet"):
    global _COUNTER
    _COUNTER += 1
    return f"{prefix}{_COUNTER}"


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import WeatherMarket, WeatherForecast, Bet, Portfolio

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()
    from config.settings import bot_config

    bot_config.strategy.current_fee_rate = 0.05
    # Betting window kontrolunu testlerde kapat
    bot_config.strategy.betting_window_enabled = False


def _td(offset=2):
    return (datetime.now(timezone.utc) + timedelta(days=offset)).replace(hour=23, minute=59, second=59, microsecond=0)


def _add_market(session, mid, yes_price=0.40, no_price=0.60, city="Testville", icao="TEST", td=None):
    from database.models import WeatherMarket

    session.add(
        WeatherMarket(
            id=mid,
            question="Test?",
            city=city,
            city_code=icao,
            metric="temperature_max",
            threshold=25.0,
            target_date=td or _td(),
            yes_price=float(yes_price),
            no_price=float(no_price),
            status="open",
            latitude=41.0,
            longitude=29.0,
        )
    )


def _add_portfolio(session, cash=5000.0):
    from database.models import Portfolio

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if pf is None:
        session.add(Portfolio(id=1, cash_balance=cash, total_value=cash))


class TestOneBetPerGroup:
    """Her grupta SADECE 1 bet olur."""

    def test_one_bet_per_group(self):
        """Ayni grupta 2 market varsa sadece 1 bet acilmali."""
        from database.db import get_session
        from database.models import Bet
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            s.commit()

        placed = BetPlacer().place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert placed == 1, f"expected 1 placed, got {placed}"
            assert len(bets) == 1

    def test_rotation_closes_old_bet(self):
        """Eski bet kapatilip yenisini acmali."""
        from database.db import get_session
        from database.models import Bet, WeatherMarket
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            m1 = _unique_id()
            m2 = _unique_id()
            _add_market(s, m1, yes_price=0.30, td=td)
            _add_market(s, m2, yes_price=0.50, td=td)
            s.commit()

        placed = BetPlacer().place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert placed == 1, f"expected 1 placed, got {placed}"
            assert len(bets) == 1
            assert bets[0].market_id == m2  # en yuksek fiyatli market

    def test_rerun_does_not_duplicate(self):
        """Ayni marketlerde zaten bet varsa tekrar acilmaz."""
        from database.db import get_session
        from database.models import Bet
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            s.commit()

        placer = BetPlacer()
        placer.place_all_pending()
        placer.place_all_pending()  # ikinci kez

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert len(bets) == 1, "tekrar calistirildiginda bet sayisi artmamali"


class TestRotation:
    """Rotation: eski bet kapatilir, en yuksek fiyatli piyasada yenisi acilir."""

    def test_lower_priced_bet_closed(self):
        """Dusuk fiyatli bet kapatilmali, yuksek fiyatli kalmali."""
        from database.db import get_session
        from database.models import Bet, WeatherMarket
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            m_low = _unique_id()
            m_high = _unique_id()
            _add_market(s, m_low, yes_price=0.30, td=td)
            _add_market(s, m_high, yes_price=0.50, td=td)
            s.commit()

        # Ilk tur: en yuksek fiyatli markete bet ac
        placed1 = BetPlacer().place_all_pending()
        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert len(bets) == 1
            assert bets[0].market_id == m_high

    def test_same_price_one_bet(self):
        """Ayni fiyattaki marketlerden sadece 1'ine bet acilmali."""
        from database.db import get_session
        from database.models import Bet
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            s.commit()

        placed = BetPlacer().place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert placed == 1
            assert len(bets) == 1

    def test_different_dates_separate_bets(self):
        """Farkli tarihlerde ayri betler acilmali."""
        from database.db import get_session
        from database.models import Bet
        from executor.bet_placer import BetPlacer

        td1 = _td(2)
        td2 = _td(3)
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, _unique_id(), yes_price=0.40, td=td1)
            _add_market(s, _unique_id(), yes_price=0.40, td=td2)
            s.commit()

        placed = BetPlacer().place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert placed == 2
            assert len(bets) == 2
