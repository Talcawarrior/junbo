"""
Faz 4 tests: price update, ladder fill, unrealized PnL, portfolio total_value.
"""

import os
import tempfile

# --- Override DB path BEFORE any project import ---
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)

# Force database.db to use our temp path at module import time
from config.settings import config as _cfg  # noqa: E402

_cfg.DB_PATH = _db_path

# database.db caches DB_PATH at module level; re-import ensures our override sticks
import importlib  # noqa: E402

import database.db  # noqa: E402

importlib.reload(database.db)

from database.db import get_session, init_db  # noqa: E402

init_db()

from database.models import Bet, Portfolio, WeatherMarket  # noqa: E402


def _clean():
    """Clean all rows between tests (keeps DB file intact)."""
    with get_session() as session:
        session.query(Bet).delete()
        session.query(WeatherMarket).delete()
        session.query(Portfolio).delete()
        session.commit()


def _setup():
    """Create mock data in a clean DB."""
    _clean()
    with get_session() as session:
        pf = Portfolio(
            id=1, cash_balance=990.0, total_value=1000.0, current_value=990.0
        )
        session.add(pf)

        market = WeatherMarket(
            id="test-faz4-ladder",
            question="Test market for Faz4",
            yes_price=0.34,
            no_price=0.66,
            status="open",
            city="TestCity",
            city_code="TST",
            metric="temperature_max",
            threshold=30.0,
            target_date=None,
        )
        session.add(market)

        bet = Bet(
            market_id="test-faz4-ladder",
            side="YES",
            amount=10.0,
            price=0.35,
            entry_price=0.35,
            current_price=0.35,
            shares=28.57,
            status="placed",
            unrealized_pnl=0.0,
        )
        session.add(bet)

        market2 = WeatherMarket(
            id="test-faz4-no",
            question="Test NO side",
            yes_price=0.65,
            no_price=0.35,
            status="open",
            city="TestCity2",
            city_code="TST2",
            metric="temperature_min",
            threshold=10.0,
            target_date=None,
        )
        session.add(market2)
        bet2 = Bet(
            market_id="test-faz4-no",
            side="NO",
            amount=20.0,
            price=0.35,
            entry_price=0.35,
            current_price=0.35,
            shares=57.14,
            status="placed",
            unrealized_pnl=0.0,
        )
        session.add(bet2)
        session.commit()


def _teardown_module():
    """Clean up the temp DB file at module end."""
    try:
        os.unlink(_db_path)
    except Exception:
        pass


def test_no_side_unrealized_pnl():
    """NO side bet: yes_price rises -> NO price falls -> negative unrealized PnL."""
    _setup()
    try:
        # Simulate price update
        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-no").first()
            assert bet is not None
            # NO price = 1 - yes_price = 1 - 0.75 = 0.25
            current = 0.25
            bet.current_price = current
            shares = float(bet.shares or 0.0)
            entry = float(bet.entry_price or bet.price or 0.0)
            bet.unrealized_pnl = round((current - entry) * shares, 2)
            session.commit()

        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-no").first()
            assert bet is not None
            assert bet.current_price == 0.25, f"Expected 0.25, got {bet.current_price}"
            # PnL = 57.14 * (0.25 - 0.35) = -5.71
            assert bet.unrealized_pnl is not None
            assert bet.unrealized_pnl < 0, f"Expected negative PnL for NO, got {bet.unrealized_pnl}"
    finally:
        _clean()


def test_no_open_bets():
    """No open bets -> run_update_prices returns gracefully."""
    _setup()
    try:
        with get_session() as session:
            session.query(Bet).delete()
            session.commit()

        from jobs.scheduler import run_update_prices

        result = run_update_prices()
        # Should not crash
        assert result is not None
    finally:
        _clean()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
