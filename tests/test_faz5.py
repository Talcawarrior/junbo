"""
Faz 5 tests: end-to-end place bets pipeline (mock).
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

# --- Override DB path BEFORE any project import ---
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)

from config.settings import config as _cfg  # noqa: E402

_cfg.DB_PATH = _db_path

import importlib  # noqa: E402

import database.db  # noqa: E402

importlib.reload(database.db)

from database.db import get_session, init_db  # noqa: E402

init_db()

from database.models import (  # noqa: E402
    Analysis,
    Bet,
    Portfolio,
    WeatherForecast,
    WeatherMarket,
)


def _clean():
    with get_session() as session:
        session.query(Bet).delete()
        session.query(Analysis).delete()
        session.query(WeatherForecast).delete()
        session.query(WeatherMarket).delete()
        session.query(Portfolio).delete()
        session.commit()


def _setup_market_and_forecasts():
    _clean()
    with get_session() as session:
        pf = Portfolio(
            id=1, cash_balance=1000.0, total_value=1000.0, current_value=1000.0
        )
        session.add(pf)
        market = WeatherMarket(
            id="test-faz5-nyc",
            question="Will NYC temp exceed 30C on June 10?",
            city="New York",
            city_code="KLGA",
            metric="temperature_max",
            threshold=30.0,
            target_date=datetime.now(timezone.utc) + timedelta(days=1),
            yes_price=0.30,
            no_price=0.70,
            status="open",
            latitude=40.7128,
            longitude=-74.0060,
        )
        session.add(market)
        for source, temp in [
            ("gfs_seamless", 32.5),
            ("ecmwf_ifs025", 33.1),
            ("gem_global", 31.8),
        ]:
            wf = WeatherForecast(
                market_id="test-faz5-nyc",
                city="New York",
                lat=40.7128,
                lon=-74.0060,
                target_date=datetime.now(timezone.utc) + timedelta(days=1),
                metric="temperature_max",
                source=source,
                predicted_value=temp,
                fetched_at=datetime.now(timezone.utc),
            )
            session.add(wf)
        session.commit()


def test_analyze_creates_analysis():
    _setup_market_and_forecasts()
    try:
        from engine.calculator import Calculator

        calc = Calculator()
        calc.analyze_market("test-faz5-nyc")
        # Re-query from DB to avoid DetachedInstanceError
        with get_session() as session:
            analysis = (
                session.query(Analysis)
                .filter(Analysis.market_id == "test-faz5-nyc")
                .first()
            )
            assert analysis is not None, "Analysis is NULL"
            assert analysis.should_bet, f"should_bet=False (edge={analysis.edge})"
            assert analysis.recommended_amount > 0, "recommended_amount=0"
            assert analysis.edge is not None and analysis.edge > 0, "edge should be > 0"
            assert analysis.recommended_side in (
                "YES",
                "NO",
            ), f"Invalid side: {analysis.recommended_side}"
    finally:
        _clean()


def test_place_bets_creates_bet_row():
    _setup_market_and_forecasts()
    try:
        from engine.calculator import Calculator

        calc = Calculator()
        calc.analyze_market("test-faz5-nyc")
        # Re-query to verify should_bet
        with get_session() as session:
            analysis = (
                session.query(Analysis)
                .filter(Analysis.market_id == "test-faz5-nyc")
                .first()
            )
            assert analysis is not None and analysis.should_bet, (
                "Analysis should_bet is False"
            )
        from jobs.scheduler import run_place_bets

        run_place_bets()
        with get_session() as session:
            bets = session.query(Bet).filter(Bet.market_id == "test-faz5-nyc").all()
            assert len(bets) > 0, "No Bet rows found"
            for b in bets:
                assert b.status in ("placed", "pending"), f"Bad status: {b.status}"
                assert b.amount > 0, f"amount={b.amount}"
                assert b.entry_price is not None, "entry_price is None"
                assert b.shares > 0, f"shares={b.shares}"
    finally:
        _clean()


def test_portfolio_cash_decreases_after_bet():
    _setup_market_and_forecasts()
    try:
        from engine.calculator import Calculator

        calc = Calculator()
        calc.analyze_market("test-faz5-nyc")
        with get_session() as session:
            pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            initial_cash = pf.cash_balance
            assert initial_cash == 1000.0, f"Initial cash={initial_cash}"
        from jobs.scheduler import run_place_bets

        run_place_bets()
        with get_session() as session:
            pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            assert pf.cash_balance < initial_cash, (
                f"Cash did not decrease: {pf.cash_balance}"
            )
            bet = session.query(Bet).filter(Bet.market_id == "test-faz5-nyc").first()
            assert bet is not None
            # Level 1 = 50% of recommended, cash -= Level 1 + entry_fee
            expected_level1 = bet.amount * 0.5
            entry_fee = bet.entry_fee or 0.0
            expected_cash = round(initial_cash - expected_level1 - entry_fee, 2)
            assert abs(pf.cash_balance - expected_cash) < 0.1, (
                f"cash={pf.cash_balance}, expected={expected_cash} "
                f"(bet={bet.amount}, l1={expected_level1}, fee={entry_fee})"
            )
    finally:
        _clean()


def test_ladder_data_json():
    _setup_market_and_forecasts()
    try:
        from engine.calculator import Calculator

        calc = Calculator()
        calc.analyze_market("test-faz5-nyc")
        from jobs.scheduler import run_place_bets

        run_place_bets()
        with get_session() as session:
            bet = session.query(Bet).filter(Bet.market_id == "test-faz5-nyc").first()
            assert bet is not None
            ladder = json.loads(bet.ladder_data)
            assert isinstance(ladder, list), "ladder_data is not a list"
            assert len(ladder) == 3, f"Expected 3 levels, got {len(ladder)}"
            for level in ladder:
                assert "status" in level
                assert level["status"] in (
                    "filled",
                    "pending",
                ), f"Bad status: {level['status']}"
            # L1 is immediately 'filled' at placement (Bug B fix — prevents
            # double-debit in run_update_prices). L2/L3 remain pending.
            assert ladder[0]["status"] == "filled", (
                f"Level 1 should be filled: {ladder[0]}"
            )
            assert ladder[1]["status"] == "pending", (
                f"Level 2 should be pending: {ladder[1]}"
            )
            assert ladder[2]["status"] == "pending", (
                f"Level 3 should be pending: {ladder[2]}"
            )
            # L1 should also have a filled_at timestamp
            assert ladder[0].get("filled_at") is not None, "Level 1 missing filled_at"
    finally:
        _clean()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
