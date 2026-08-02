"""Tests for the bug fixes from code review (2026-07-26).

BUG 5: _load_from_db OPEN_BET_STATUSES includes 'placed' and 'pending'
ISSUE 10: execute_signal uses flush() not commit()
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest


_COUNTER = 0


def _unique_id(prefix="m"):
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


def _add_market(
    session, mid=None, city="Testville", icao="TEST", thresh=25, yes_price=0.02, no_price=0.98, target_date=None
):
    if mid is None:
        mid = _unique_id()
    td = target_date or _td()
    from database.models import WeatherMarket

    session.add(
        WeatherMarket(
            id=mid,
            question="Test?",
            city=city,
            city_code=icao,
            metric="temperature_max",
            threshold=float(thresh),
            target_date=td,
            yes_price=yes_price,
            no_price=no_price,
            status="open",
            latitude=41.0,
            longitude=29.0,
        )
    )


def _add_portfolio(session):
    from database.models import Portfolio

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if pf is None:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))


# ── BUG 5: _load_from_db OPEN_BET_STATUSES ────────────────────────────


class TestLoadFromDbStatuses:
    """BUG 5: _load_from_db must count bets with 'placed' and 'pending' status."""

    def test_placed_bets_are_counted(self):
        """Bets with status='placed' should be loaded as active."""
        from engine.strategy import RiskManager
        from database.db import get_session
        from database.models import Bet

        mid = _unique_id()
        with get_session() as s:
            s.add(
                Bet(
                    market_id=mid,
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    status="placed",
                )
            )
            s.commit()
            rm = RiskManager(db_session=s)
            assert rm.open_bets_count == 1
            assert rm.city_bet_counts.get("TEST", 0) == 1

    def test_pending_bets_are_counted(self):
        """Bets with status='pending' should be loaded as active."""
        from engine.strategy import RiskManager
        from database.db import get_session
        from database.models import Bet

        mid = _unique_id()
        with get_session() as s:
            s.add(
                Bet(
                    market_id=mid,
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    status="pending",
                )
            )
            s.commit()
            rm = RiskManager(db_session=s)
            assert rm.open_bets_count == 1

    def test_rejected_bets_not_counted(self):
        """Bets with status='rejected' should NOT be loaded as active."""
        from engine.strategy import RiskManager
        from database.db import get_session
        from database.models import Bet

        mid = _unique_id()
        with get_session() as s:
            s.add(
                Bet(
                    market_id=mid,
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    status="rejected",
                )
            )
            s.commit()
            rm = RiskManager(db_session=s)
            assert rm.open_bets_count == 0

    def test_closed_bets_not_counted(self):
        """Bets with status='closed' should NOT be loaded as active."""
        from engine.strategy import RiskManager
        from database.db import get_session
        from database.models import Bet

        mid = _unique_id()
        with get_session() as s:
            s.add(
                Bet(
                    market_id=mid,
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    status="closed",
                )
            )
            s.commit()
            rm = RiskManager(db_session=s)
            assert rm.open_bets_count == 0

    def test_mixed_statuses_correctly_counted(self):
        """Mix of statuses: only OPEN_BET_STATUSES values counted."""
        from engine.strategy import RiskManager
        from database.db import get_session
        from database.models import Bet, OPEN_BET_STATUSES

        with get_session() as s:
            # Insert one bet for each possible status
            for status in list(OPEN_BET_STATUSES) + ["rejected", "closed", "settled"]:
                s.add(
                    Bet(
                        market_id=_unique_id(),
                        city="Testville",
                        city_code="TEST",
                        side="YES",
                        amount=10.0,
                        status=status,
                    )
                )
            s.commit()
            rm = RiskManager(db_session=s)
            assert rm.open_bets_count == len(OPEN_BET_STATUSES)


# ── BUG 6: STEP 5.5 throttling ────────────────────────────────────────

# ── ISSUE 10: flush vs commit ─────────────────────────────────────────


class TestExecuteSignalFlush:
    """ISSUE 10: execute_signal uses flush() not commit()."""

    def test_execute_signal_uses_flush(self):
        """execute_signal should call db.flush() not db.commit()."""
        import asyncio
        from engine.strategy import BettingEngine
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s)
            # Add a market so the 8h guard doesn't block
            td = _td()
            s.add(
                WeatherMarket(
                    id="flush_test_market",
                    question="Q?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=25.0,
                    target_date=td,
                    yes_price=0.02,
                    no_price=0.98,
                    status="open",
                    latitude=41.0,
                    longitude=29.0,
                )
            )
            s.commit()

            engine = BettingEngine(db_session=s)

            # Mock a signal
            signal = MagicMock()
            signal.edge = 0.05
            signal.bet_size = 10.0
            signal.city_code = "TEST"
            signal.city = "Testville"
            signal.market_id = "flush_test_market"
            signal.entry_price = 0.02
            signal.fair_value = 0.07
            signal.probability = 0.07
            signal.outcome = "YES"
            signal.side = "YES"

            market_data = {"market_id": "flush_test_market", "city_code": "TEST", "yes_price": 0.02}

            with patch.object(s, "flush") as mock_flush:
                with patch.object(s, "commit") as mock_commit:
                    # execute_signal should use flush, not commit
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(engine.execute_signal(signal, market_data))
                    finally:
                        loop.close()

                    # flush should have been called (to assign ID)
                    mock_flush.assert_called()
                    # commit should NOT be called by execute_signal (caller manages transaction)
                    mock_commit.assert_not_called()


# ── Price Gate: yes_price > 0.10 ─────────────────────────────────────


class TestPriceGate:
    """New rule: all bets with yes_price > 0.10 must be refused."""

    def test_execute_signal_refuses_high_price(self):
        """execute_signal should refuse bets with yes_price > 0.10."""
        import asyncio
        from engine.strategy import BettingEngine
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s)
            td = _td()
            s.add(
                WeatherMarket(
                    id="price_gate_market",
                    question="Q?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=25.0,
                    target_date=td,
                    yes_price=0.30,
                    no_price=0.70,
                    status="open",
                    latitude=41.0,
                    longitude=29.0,
                )
            )
            s.commit()

            engine = BettingEngine(db_session=s)
            signal = MagicMock()
            signal.edge = 0.05
            signal.bet_size = 10.0
            signal.city_code = "TEST"
            signal.city = "Testville"
            signal.market_id = "price_gate_market"
            signal.entry_price = 0.30
            signal.fair_value = 0.35
            signal.probability = 0.35
            signal.outcome = "YES"
            signal.side = "YES"

            market_data = {"market_id": "price_gate_market", "city_code": "TEST", "yes_price": 0.30}

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(engine.execute_signal(signal, market_data))
            finally:
                loop.close()
            assert result is None, "Bet with yes_price=0.30 should be refused"

    def test_execute_signal_allows_low_price(self):
        """execute_signal should allow bets with yes_price ≤ 0.10."""
        import asyncio
        from engine.strategy import BettingEngine
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s)
            td = _td()
            s.add(
                WeatherMarket(
                    id="price_ok_market",
                    question="Q?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=25.0,
                    target_date=td,
                    yes_price=0.05,
                    no_price=0.95,
                    status="open",
                    latitude=41.0,
                    longitude=29.0,
                )
            )
            s.commit()

            engine = BettingEngine(db_session=s)
            signal = MagicMock()
            signal.edge = 0.05
            signal.bet_size = 10.0
            signal.city_code = "TEST"
            signal.city = "Testville"
            signal.market_id = "price_ok_market"
            signal.entry_price = 0.05
            signal.fair_value = 0.10
            signal.probability = 0.10
            signal.outcome = "YES"
            signal.side = "YES"

            market_data = {"market_id": "price_ok_market", "city_code": "TEST", "yes_price": 0.05}

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(engine.execute_signal(signal, market_data))
            finally:
                loop.close()
            assert result is not None, "Bet with yes_price=0.05 should be allowed"

    def test_execute_signal_refuses_exactly_011(self):
        """Edge case: yes_price=0.11 should be refused."""
        import asyncio
        from engine.strategy import BettingEngine
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s)
            td = _td()
            s.add(
                WeatherMarket(
                    id="price_edge_market",
                    question="Q?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=25.0,
                    target_date=td,
                    yes_price=0.11,
                    no_price=0.89,
                    status="open",
                    latitude=41.0,
                    longitude=29.0,
                )
            )
            s.commit()

            engine = BettingEngine(db_session=s)
            signal = MagicMock()
            signal.edge = 0.05
            signal.bet_size = 10.0
            signal.city_code = "TEST"
            signal.city = "Testville"
            signal.market_id = "price_edge_market"
            signal.entry_price = 0.11
            signal.fair_value = 0.16
            signal.probability = 0.16
            signal.outcome = "YES"
            signal.side = "YES"

            market_data = {"market_id": "price_edge_market", "city_code": "TEST", "yes_price": 0.11}

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(engine.execute_signal(signal, market_data))
            finally:
                loop.close()
            assert result is None, "Bet with yes_price=0.11 should be refused"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
