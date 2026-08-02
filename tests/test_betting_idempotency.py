"""Idempotency test: the bot must never place a duplicate open bet on the
same market. A restart or a double scan loop would otherwise create a second
Bet row -> double exposure -> real money loss.
"""

import os
import tempfile

import pytest

from database.db import get_session, init_db


def _setup_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from config.settings import config as _cfg

    _cfg.DB_PATH = path
    init_db()
    return get_session


@pytest.fixture()
def session_factory():
    return _setup_temp_db()


def _make_signal(market_id: str, side: str = "YES"):
    from engine.strategy import SimpleSignal

    return SimpleSignal(
        market_id=market_id,
        city="Testville",
        city_code="TEST",
        outcome=side,
        entry_price=0.5,
        fair_value=0.7,
        edge=0.2,
        probability=0.7,
        bet_size=10.0,
        side=side,
    )


def _make_market_data(market_id: str):
    return {
        "market_id": market_id,
        "city_code": "TEST",
        "yes_price": 0.05,
        "city": "Testville",
    }


@pytest.mark.asyncio
async def test_execute_signal_refuses_duplicate_open_bet(session_factory):
    from engine.strategy import BettingEngine

    with session_factory() as session:
        strat = BettingEngine(db_session=session)

        bet1 = await strat.execute_signal(_make_signal("M1"), _make_market_data("M1"))
        assert bet1 is not None
        assert bet1.id is not None

        # Second attempt on the same market must be refused and return the
        # existing open bet, NOT a new row.
        bet2 = await strat.execute_signal(_make_signal("M1"), _make_market_data("M1"))
        assert bet2 is bet1
        assert bet2.id == bet1.id

        from database.models import Bet, OPEN_BET_STATUSES

        open_count = session.query(Bet).filter(Bet.market_id == "M1", Bet.status.in_(OPEN_BET_STATUSES)).count()
        assert open_count == 1


@pytest.mark.asyncio
async def test_execute_signal_allows_different_markets(session_factory):
    from engine.strategy import BettingEngine

    with session_factory() as session:
        strat = BettingEngine(db_session=session)
        b1 = await strat.execute_signal(_make_signal("M1"), _make_market_data("M1"))
        b2 = await strat.execute_signal(_make_signal("M2"), _make_market_data("M2"))
        assert b1.id != b2.id
        assert b1.market_id == "M1"
        assert b2.market_id == "M2"
