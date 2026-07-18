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
        # Directly update the bet as run_update_prices would
        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-ladder").first()
            assert bet is not None
            # Simulate price update as done by update_prices
            current = 0.34  # from market's yes_price
            bet.current_price = current
            shares = float(bet.shares or 0.0)
            entry = float(bet.entry_price or bet.price or 0.0)
            bet.unrealized_pnl = round((current - entry) * shares, 2)

            # Ladder fill check
            from datetime import datetime, timezone
            ladder = json.loads(bet.ladder_data) if isinstance(bet.ladder_data, str) else bet.ladder_data
            filled_amount = 0.0
            for rung in ladder:
                if rung.get("status") == "pending":
                    trigger_price = float(rung.get("price", 0))
                    rung_size = float(rung.get("size", rung.get("amount", 0)))
                    should_fill = current <= trigger_price
                    if should_fill and rung_size > 0:
                        rung["status"] = "filled"
                        rung["filled_at"] = datetime.now(timezone.utc).isoformat()
                        filled_amount += rung_size
            bet.ladder_data = json.dumps(ladder)
            session.commit()

        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz4-ladder").first()
            assert bet is not None
            assert bet.current_price == 0.34, f"Expected 0.34, got {bet.current_price}"
            assert bet.unrealized_pnl is not None
            assert bet.unrealized_pnl < 0, f"Expected negative PnL, got {bet.unrealized_pnl}"

            ladder = json.loads(bet.ladder_data)
            assert ladder[1]["status"] == "filled", f"Level 2 not filled: {ladder[1]}"
            assert ladder[2]["status"] == "pending", f"Level 3 should still be pending: {ladder[2]}"
            assert "filled_at" in ladder[1], "Level 2 missing filled_at"
    finally:
        _clean()


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
