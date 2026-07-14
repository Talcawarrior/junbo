"""Tests for Polymarket-Gamma-API-based settlement engine.

Replaces the old Open-Meteo weather settlement tests (test_faz6.py).
"""

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import requests

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)

from config.settings import config as _cfg  # noqa: E402

_cfg.DB_PATH = _db_path

import importlib  # noqa: E402

import database.db  # noqa: E402

importlib.reload(database.db)  # noqa: E402

from database.db import get_session, init_db  # noqa: E402

init_db()  # noqa: E402

from database.models import Bet, Portfolio, WeatherMarket  # noqa: E402


def _clean():
    with get_session() as session:
        session.query(Bet).delete()
        session.query(WeatherMarket).delete()
        session.query(Portfolio).delete()
        session.commit()


def _setup_market_with_bets(
    market_id="test-poly-001",
    side_yes="YES",
    side_no="NO",
    yes_price=0.35,
    no_price=None,
    threshold=30.0,
    stake=10.0,
    days_ago=1,
):
    """Create a past-due market and two opposite-side bets (YES + NO).

    Returns (market, yes_bet, no_bet, portfolio).
    """
    _clean()
    if no_price is None:
        no_price = round(1.0 - yes_price, 2)
    target = datetime.now() - timedelta(days=days_ago)
    entry_yes = yes_price
    entry_no = no_price
    shares_yes = stake / entry_yes if entry_yes > 0 else 0
    shares_no = stake / entry_no if entry_no > 0 else 0

    with get_session() as session:
        pf = Portfolio(
            id=1,
            cash_balance=1000.0,
            total_value=1000.0,
            current_value=980.0,
            total_realized_pnl=0.0,
            total_won=0,
            total_lost=0,
        )
        session.add(pf)

        market = WeatherMarket(
            id=market_id,
            question="Test market",
            city="TestCity",
            city_code="TEST",
            metric="temperature_max",
            threshold=threshold,
            target_date=target,
            yes_price=yes_price,
            no_price=no_price,
            status="bet_placed",
            latitude=40.0,
            longitude=-70.0,
            market_type="HIGH",
        )
        session.add(market)

        bet_yes = Bet(
            market_id=market_id,
            side="YES",
            amount=stake,
            price=entry_yes,
            entry_price=entry_yes,
            shares=shares_yes,
            status="placed",
            unrealized_pnl=0.0,
        )
        session.add(bet_yes)

        bet_no = Bet(
            market_id=market_id,
            side="NO",
            amount=stake,
            price=entry_no,
            entry_price=entry_no,
            shares=shares_no,
            status="placed",
            unrealized_pnl=0.0,
        )
        session.add(bet_no)

        session.commit()

    return market, bet_yes, bet_no, pf


def _gamma_mock(closed=True, status="resolved", outcome_prices=None):
    """Build a MagicMock that mimics the Gamma API response for a resolved market."""
    if outcome_prices is None:
        outcome_prices = ["1", "0"]
    mock = MagicMock()
    mock.json.return_value = {
        "closed": closed,
        "umaResolutionStatus": status,
        "outcomePrices": outcome_prices,
    }
    mock.raise_for_status = MagicMock()
    return mock


# ── Tests ──────────────────────────────────────────────────────────────────


class TestSettlementPolymarket:
    """SettlementEngine tests using Gamma API resolution."""

    def test_resolved_yes(self):
        """Gamma returns YES -> YES bet wins, NO bet loses."""
        market, bet_yes, bet_no, pf = _setup_market_with_bets(
            yes_price=0.35, stake=10.0
        )
        try:
            with patch("executor.settler.requests.get") as mock_get:
                mock_get.return_value = _gamma_mock(
                    outcome_prices=["1", "0"],
                )
                from executor.settler import SettlementEngine

                engine = SettlementEngine()
                results = engine.settle_all()
                assert results["win"] == 1
                assert results["loss"] == 1
                assert results["pending"] == 0

            with get_session() as session:
                b_yes = (
                    session.query(Bet)
                    .filter(Bet.market_id == "test-poly-001", Bet.side == "YES")
                    .first()
                )
                b_no = (
                    session.query(Bet)
                    .filter(Bet.market_id == "test-poly-001", Bet.side == "NO")
                    .first()
                )

                # YES bet won
                assert b_yes.status == "won"
                entry = 0.35
                expected_payout = 10.0 / entry  # 28.5714...
                # Settlement fee = 0 (mathematically zero at p→1).
                # Entry fee for test bets is 0.0 (no entry_fee set).
                expected_pnl = round(expected_payout - 10.0, 2)  # no fee at settlement
                assert b_yes.realized_pnl == expected_pnl

                # NO bet lost
                assert b_no.status == "lost"
                # Lost bet: PnL = -(stake + entry_fee). entry_fee=0 for test bets.
                assert b_no.realized_pnl == -10.0

                mkt = (
                    session.query(WeatherMarket)
                    .filter(WeatherMarket.id == "test-poly-001")
                    .first()
                )
                assert mkt.status == "settled_win"
                rd = json.loads(mkt.raw_data)
                assert rd["source"] == "polymarket"
                assert rd["outcome"] == "YES"

                pf_db = session.query(Portfolio).filter(Portfolio.id == 1).first()
                # YES bet: cash += FULL payout (fee = 0 at settlement, entry fee
                # was separately debited at bet placement time or is 0 for test bets).
                # NO bet: only total_lost incremented, cash unchanged.
                assert pf_db.cash_balance == round(1000.0 + expected_payout, 2)
                assert pf_db.total_won == 1
                assert pf_db.total_lost == 1
        finally:
            _clean()

    def test_resolved_no(self):
        """Gamma returns NO -> NO bet wins, YES bet loses."""
        market, bet_yes, bet_no, pf = _setup_market_with_bets(
            yes_price=0.35, stake=10.0
        )
        try:
            with patch("executor.settler.requests.get") as mock_get:
                mock_get.return_value = _gamma_mock(
                    outcome_prices=["0", "1"],
                )
                from executor.settler import SettlementEngine

                engine = SettlementEngine()
                results = engine.settle_all()
                assert results["win"] == 1
                assert results["loss"] == 1
                assert results["pending"] == 0

            with get_session() as session:
                b_yes = (
                    session.query(Bet)
                    .filter(Bet.market_id == "test-poly-001", Bet.side == "YES")
                    .first()
                )
                b_no = (
                    session.query(Bet)
                    .filter(Bet.market_id == "test-poly-001", Bet.side == "NO")
                    .first()
                )

                assert b_yes.status == "lost"
                assert b_yes.realized_pnl == -10.0  # -stake, entry_fee=0 for test bet

                assert b_no.status == "won"
                entry_no = 0.65
                expected_payout = 10.0 / entry_no  # 15.3846...
                # Settlement fee = 0 (p→1). entry_fee for test bets = 0.
                expected_pnl = round(expected_payout - 10.0, 2)
                assert b_no.realized_pnl == expected_pnl

                mkt = (
                    session.query(WeatherMarket)
                    .filter(WeatherMarket.id == "test-poly-001")
                    .first()
                )
                assert mkt.status == "settled_loss"

                pf_db = session.query(Portfolio).filter(Portfolio.id == 1).first()
                assert pf_db.total_won == 1
                assert pf_db.total_lost == 1
        finally:
            _clean()

    def test_not_yet_resolved(self):
        """Gamma says closed=false -> None, no state changes."""
        market, bet_yes, bet_no, pf = _setup_market_with_bets()
        try:
            # Capture pre-settlement state
            with get_session() as session:
                mkt_before = (
                    session.query(WeatherMarket)
                    .filter(WeatherMarket.id == "test-poly-001")
                    .first()
                )
                assert mkt_before.status == "bet_placed"

            with patch("executor.settler.requests.get") as mock_get:
                mock_get.return_value = _gamma_mock(
                    closed=False,
                    status="open",
                    outcome_prices=None,
                )
                from executor.settler import SettlementEngine

                engine = SettlementEngine()
                results = engine.settle_all()
                assert results["pending"] == 1
                assert results["win"] == 0
                assert results["loss"] == 0

            with get_session() as session:
                # Bet statuses unchanged
                bets = session.query(Bet).filter(Bet.market_id == "test-poly-001").all()
                for b in bets:
                    assert b.status in ("placed",), (
                        f"Bet {b.id} status changed: {b.status}"
                    )

                # Market status unchanged
                mkt = (
                    session.query(WeatherMarket)
                    .filter(WeatherMarket.id == "test-poly-001")
                    .first()
                )
                assert mkt.status == "bet_placed"
                assert mkt.raw_data is None

                # Portfolio unchanged
                pf_db = session.query(Portfolio).filter(Portfolio.id == 1).first()
                assert pf_db.cash_balance == 1000.0
                assert pf_db.total_realized_pnl == 0.0
        finally:
            _clean()

    def test_split_resolution(self):
        """outcomePrices [0.5,0.5] -> None returned, no bets settled."""
        _setup_market_with_bets()
        try:
            with patch("executor.settler.requests.get") as mock_get:
                mock_get.return_value = _gamma_mock(
                    outcome_prices=["0.5", "0.5"],
                )
                from executor.settler import SettlementEngine

                engine = SettlementEngine()
                results = engine.settle_all()
                assert results["pending"] == 1
                assert results["win"] == 0
                assert results["loss"] == 0

            with get_session() as session:
                bets = session.query(Bet).filter(Bet.market_id == "test-poly-001").all()
                for b in bets:
                    assert b.status == "placed"
        finally:
            _clean()

    def test_gamma_http_error(self):
        """requests.get raises -> None, no crash, no state change."""
        _setup_market_with_bets()
        try:
            with patch("executor.settler.requests.get") as mock_get:
                mock_get.side_effect = requests.ConnectionError("timeout")
                from executor.settler import SettlementEngine

                engine = SettlementEngine()
                results = engine.settle_all()
                assert results["pending"] == 1
                assert results["win"] == 0
                assert results["loss"] == 0

            with get_session() as session:
                bets = session.query(Bet).filter(Bet.market_id == "test-poly-001").all()
                for b in bets:
                    assert b.status == "placed"
                mkt = (
                    session.query(WeatherMarket)
                    .filter(WeatherMarket.id == "test-poly-001")
                    .first()
                )
                assert mkt.status == "bet_placed"
        finally:
            _clean()

    def test_pnl_cash_reconciliation(self):
        """1 win + 1 loss: cash reflects win payout-fee only; total_realized_pnl sums both."""
        market, bet_yes, bet_no, pf = _setup_market_with_bets(
            yes_price=0.35, stake=10.0
        )
        try:
            with patch("executor.settler.requests.get") as mock_get:
                mock_get.return_value = _gamma_mock(
                    outcome_prices=["1", "0"],
                )
                from executor.settler import SettlementEngine

                engine = SettlementEngine()
                engine.settle_all()

            with get_session() as session:
                b_yes = (
                    session.query(Bet)
                    .filter(Bet.market_id == "test-poly-001", Bet.side == "YES")
                    .first()
                )
                b_no = (
                    session.query(Bet)
                    .filter(Bet.market_id == "test-poly-001", Bet.side == "NO")
                    .first()
                )
                pf_db = session.query(Portfolio).filter(Portfolio.id == 1).first()

                # Win PnL (entry_fee=0 for test bets, settlement fee=0 mathematically)
                entry = 0.35
                payout = 10.0 / entry
                win_pnl = round(payout - 10.0, 2)

                loss_pnl = -10.0

                # Cash should equal initial + FULL payout — fee at settlement is 0
                # (mathematical zero at p→1). Entry fee was not set for test bets.
                assert pf_db.cash_balance == round(1000.0 + payout, 2), (
                    f"cash={pf_db.cash_balance} != {round(1000.0 + payout, 2)}"
                )

                expected_total = win_pnl + loss_pnl
                assert pf_db.total_realized_pnl == pytest.approx(
                    expected_total, rel=1e-3
                ), f"total_realized_pnl={pf_db.total_realized_pnl} != {expected_total}"

                assert b_yes.realized_pnl == win_pnl
                assert b_no.realized_pnl == loss_pnl
        finally:
            _clean()
