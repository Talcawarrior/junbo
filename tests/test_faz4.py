"""
Faz 4 tests: price update, ladder fill, unrealized PnL, portfolio total_value.
"""

import json
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

        ladder_data = json.dumps(
            [
                {
                    "level": 1,
                    "price": 0.35,
                    "size": 5.0,
                    "amount": 5.0,
                    "shares": 14.29,
                    "status": "filled",
                },
                {
                    "level": 2,
                    "price": 0.343,
                    "size": 3.0,
                    "amount": 3.0,
                    "shares": 8.75,
                    "status": "pending",
                },
                {
                    "level": 3,
                    "price": 0.3325,
                    "size": 2.0,
                    "amount": 2.0,
                    "shares": 6.02,
                    "status": "pending",
                },
            ]
        )
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
            ladder_data=ladder_data,
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
            ladder_data="[]",
        )
        session.add(bet2)
        session.commit()


def _teardown_module():
    """Clean up the temp DB file at module end."""
    try:
        os.unlink(_db_path)
    except Exception:
        pass


def test_ladder_price_drops_trigger_fill():
    """YES bet + market price drops -> level 2 should fill (trigger at 0.343, current 0.34)."""
    _setup()
    try:
        from jobs.scheduler import run_update_prices

        result = run_update_prices()
        assert "güncellendi" in result, f"Unexpected result: {result}"

        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-ladder").first()
            assert bet is not None
            # Price updated
            assert bet.current_price == 0.34, f"Expected 0.34, got {bet.current_price}"
            # Unrealized PnL: 28.57 * (0.34 - 0.35) = -0.2857 ~ -0.29
            assert bet.unrealized_pnl is not None
            assert bet.unrealized_pnl < 0, (
                f"Expected negative PnL, got {bet.unrealized_pnl}"
            )

            # Ladder: level 2 should be filled (trigger 0.343 >= current 0.34)
            ladder = json.loads(bet.ladder_data)
            assert ladder[1]["status"] == "filled", f"Level 2 not filled: {ladder[1]}"
            assert ladder[2]["status"] == "pending", (
                f"Level 3 should still be pending: {ladder[2]}"
            )
            assert "filled_at" in ladder[1], "Level 2 missing filled_at"

            # Level 2 amount 3.0 deducted from cash (990 - 3 = 987)
            pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            assert pf is not None
            assert pf.cash_balance == 987.0, f"Expected 987.0, got {pf.cash_balance}"

            # total_value = cash + open_exposure + unrealized
            # cash=987, exposure=10(YES)+20(NO)=30, unrealized~-0.29
            # total = 987 + 30 + (-0.29) = 1016.71
            assert pf.total_value is not None
            assert abs(pf.total_value - 1016.71) < 0.5, (
                f"total_value={pf.total_value}, expected ~1016.71"
            )
    finally:
        _clean()


def test_no_side_unrealized_pnl():
    """NO side bet: yes_price rises -> NO price falls -> negative unrealized PnL."""
    _setup()
    try:
        # Update market price: yes_price=0.65 -> NO price=0.35
        with get_session() as session:
            m = (
                session.query(WeatherMarket)
                .filter(WeatherMarket.id == "test-faz4-no")
                .first()
            )
            m.yes_price = 0.75  # NO price = 0.25
            session.commit()

        from jobs.scheduler import run_update_prices

        run_update_prices()

        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-no").first()
            assert bet is not None
            # NO price = 1 - 0.75 = 0.25
            assert bet.current_price == 0.25, f"Expected 0.25, got {bet.current_price}"
            # PnL = 57.14 * ((1-0.25) - (1-0.35)) = 57.14 * (-0.10) = -5.71
            assert bet.unrealized_pnl is not None
            assert bet.unrealized_pnl < 0, (
                f"Expected negative PnL for NO, got {bet.unrealized_pnl}"
            )
    finally:
        _clean()


def test_ladder_no_price_change_no_fill():
    """Price unchanged -> no ladder fills."""
    _setup()
    try:
        with get_session() as session:
            m = (
                session.query(WeatherMarket)
                .filter(WeatherMarket.id == "test-faz4-ladder")
                .first()
            )
            m.yes_price = 0.35  # Same as entry
            session.commit()

        from jobs.scheduler import run_update_prices

        run_update_prices()

        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-ladder").first()
            ladder = json.loads(bet.ladder_data)
            assert ladder[1]["status"] == "pending", (
                f"Level 2 should be pending: {ladder[1]}"
            )
            assert ladder[2]["status"] == "pending", (
                f"Level 3 should be pending: {ladder[2]}"
            )
            # Cash unchanged
            pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            assert pf is not None
            assert pf.cash_balance == 990.0, f"Expected 990.0, got {pf.cash_balance}"
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
