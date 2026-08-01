"""Tests for the tie-betting feature (2026-08-01).

Tie bet: ayni (city, target_date, metric) grubunda en yuksek fiyata sahip
TUM marketlere bet acilir. Zaman gecince biri one gecerse digeri
(close_losing_twin_bets) otomatik kapatilir.
"""

from datetime import datetime, timezone, timedelta

import pytest


_COUNTER = 10000


def _unique_id(prefix="tie"):
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


class TestTieOpen:
    """Tie: ayni max fiyata sahip tum marketlere bet acilir."""

    def test_two_tied_markets_both_open(self):
        """Ayni grup, ayni fiyat (%40): iki bet de acilmalı."""
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
            assert placed == 2, f"expected 2 placed, got {placed}"
            assert len(bets) == 2
            assert len({b.market_id for b in bets}) == 2, "iki farkli markette bet olmali"

    def test_three_tied_markets_all_open(self):
        """Uc market ayni fiyata bagliysa ucune de bet acilir."""
        from database.db import get_session
        from database.models import Bet
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, _unique_id(), yes_price=0.45, td=td)
            _add_market(s, _unique_id(), yes_price=0.45, td=td)
            _add_market(s, _unique_id(), yes_price=0.45, td=td)
            s.commit()

        placed = BetPlacer().place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert placed == 3
            assert len(bets) == 3

    def test_only_tied_markets_open_not_lower(self):
        """En yuksek fiyata bagli olmayan (dusuk fiyatli) markete bet acilmaz."""
        from database.db import get_session
        from database.models import Bet
        from executor.bet_placer import BetPlacer

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            _add_market(s, _unique_id(), yes_price=0.40, td=td)
            _add_market(s, _unique_id(), yes_price=0.05, td=td)
            s.commit()

        placed = BetPlacer().place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert placed == 2, f"only 2 tied markets should open, got {placed}"
            assert len(bets) == 2

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
        second = placer.place_all_pending()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.status.in_(("placed", "pending"))).all()
            assert second == 0, "ikinci calistirmada yeni bet acilmamali"
            assert len(bets) == 2


class TestTwinLoserClose:
    """Tie ile acilan betlerden geride kalan otomatik kapatilir."""

    def _setup_two_bets(self, leader_price=0.50, loser_price=0.30):
        from database.db import get_session
        from executor.bet_placer import BetPlacer

        td = _td()
        m1 = _unique_id()
        m2 = _unique_id()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, m1, yes_price=0.40, td=td)
            _add_market(s, m2, yes_price=0.40, td=td)
            s.commit()

        placer = BetPlacer()
        placer.place_all_pending()
        return placer, m1, m2, td

    def test_laggard_closed_when_gap_exceeds(self):
        """Lider 0.50, gerideki 0.30 (gap 0.20 >= 0.10) -> gerideki kapatilir."""
        from database.db import get_session
        from database.models import Bet, WeatherMarket

        placer, m1, m2, td = self._setup_two_bets()

        with get_session() as s:
            s.query(WeatherMarket).filter_by(id=m1).update({"yes_price": 0.50})
            s.query(WeatherMarket).filter_by(id=m2).update({"yes_price": 0.30})
            s.commit()

        closed = placer.close_losing_twin_bets()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.market_id.in_([m1, m2])).all()
            by_mkt = {b.market_id: b for b in bets}
            assert closed == 1, f"expected 1 closed, got {closed}"
            assert by_mkt[m1].status in ("placed", "pending"), "lider acik kalir"
            assert by_mkt[m2].status == "closed", "gerideki kapatilir"
            assert by_mkt[m2].close_reason == "rotation"

    def test_small_gap_not_closed(self):
        """Lider 0.45, gerideki 0.40 (gap 0.05 < 0.10) -> hicbiri kapatilmaz."""
        from database.db import get_session
        from database.models import Bet, WeatherMarket

        placer, m1, m2, td = self._setup_two_bets()

        with get_session() as s:
            s.query(WeatherMarket).filter_by(id=m1).update({"yes_price": 0.45})
            s.query(WeatherMarket).filter_by(id=m2).update({"yes_price": 0.40})
            s.commit()

        closed = placer.close_losing_twin_bets()

        with get_session() as s:
            bets = s.query(Bet).filter(Bet.market_id.in_([m1, m2])).all()
            assert closed == 0
            assert all(b.status in ("placed", "pending") for b in bets)

    def test_single_bet_never_closed(self):
        """Grupta tek bet varsa kapatma calismaz."""
        from database.db import get_session
        from database.models import WeatherMarket

        td = _td()
        m1 = _unique_id()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, m1, yes_price=0.40, td=td)
            s.commit()

        from executor.bet_placer import BetPlacer

        placer = BetPlacer()
        placer.place_all_pending()

        with get_session() as s:
            s.query(WeatherMarket).filter_by(id=m1).update({"yes_price": 0.70})
            s.commit()

        closed = placer.close_losing_twin_bets()
        assert closed == 0

    def test_gap_zero_disables(self):
        """tie_loser_gap=0 ise kapatma devre disi kalir."""
        from config.settings import bot_config

        placer, m1, m2, td = self._setup_two_bets()
        orig = bot_config.strategy.tie_loser_gap
        try:
            bot_config.strategy.tie_loser_gap = 0.0
            closed = placer.close_losing_twin_bets()
            assert closed == 0
        finally:
            bot_config.strategy.tie_loser_gap = orig


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
