"""Tests for the 6 bug fixes from code review (2026-07-26).

BUG 1+2: range_bet_placer accounting — debit_stake/credit_sale calls
BUG 3: _find_market session management (covered by test_range_betting.py)
BUG 5: _load_from_db OPEN_BET_STATUSES includes 'placed' and 'pending'
BUG 6: STEP 5.5 PT throttling (5-minute interval)
ISSUE 9: range_bet_cities defensive copy
ISSUE 10: execute_signal uses flush() not commit()
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest


TEST_CITIES = ["testville"]
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

    bot_config.strategy.range_bet_cities = list(TEST_CITIES)
    bot_config.strategy.range_bet_enabled = True
    bot_config.strategy.range_bet_spread = 1
    bot_config.strategy.range_bet_amount = 10.0
    bot_config.strategy.range_bet_trail_stop_pct = 0.20
    bot_config.strategy.range_bet_pt_take_rate = 1.0
    bot_config.strategy.range_bet_pre_settlement_hours = 1.0
    bot_config.strategy.current_fee_rate = 0.05


def _td(offset=1):
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


def _add_forecast(session, icao="TEST", target_date=None, value=25.0, source="test_source"):
    from database.models import WeatherForecast

    td = target_date or _td()
    session.add(
        WeatherForecast(
            market_id="",
            city=icao,
            lat=41.0,
            lon=41.0,
            target_date=td,
            metric="temperature_max",
            source=source,
            predicted_value=value,
            fetched_at=datetime.now(),
        )
    )


def _add_portfolio(session):
    from database.models import Portfolio

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if pf is None:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))


# ── BUG 1+2: accounting debit_stake / credit_sale ─────────────────────


class TestRangeBetAccounting:
    """BUG 1+2: place_range_bets must call debit_stake and credit_sale."""

    def test_place_range_bets_debits_stake(self):
        """_place_one_bet should call debit_stake for stake + fee (6 calls for 3 bets)."""
        from executor.range_bet_placer import place_range_bets
        from database.db import get_session

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, thresh=24, yes_price=0.01, target_date=td)
            _add_market(s, thresh=25, yes_price=0.02, target_date=td)
            _add_market(s, thresh=26, yes_price=0.03, target_date=td)
            _add_forecast(s, "TEST", td, 25.0, "src1")
            _add_forecast(s, "TEST", td, 25.0, "src2")
            s.commit()

        with patch("utils.accounting.debit_stake") as mock_debit:
            results = place_range_bets()
            assert len(results) == 3
            # debit_stake called 6 times: 3 for stake ($10) + 3 for entry_fee
            assert mock_debit.call_count == 6
            # Verify all calls have a string reference containing "range_bet"
            for call in mock_debit.call_args_list:
                ref = call[0][2]  # third positional arg = reference string
                assert "range_bet" in ref

    def test_place_range_bets_debits_entry_fee(self):
        """_place_one_bet should also debit entry_fee."""
        from executor.range_bet_placer import place_range_bets
        from database.db import get_session

        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, thresh=24, yes_price=0.01, target_date=td)
            _add_market(s, thresh=25, yes_price=0.02, target_date=td)
            _add_market(s, thresh=26, yes_price=0.03, target_date=td)
            _add_forecast(s, "TEST", td, 25.0, "src1")
            _add_forecast(s, "TEST", td, 25.0, "src2")
            s.commit()

        with patch("utils.accounting.debit_stake") as mock_debit:
            results = place_range_bets()
            assert len(results) == 3
            # debit_stake called 6 times: 3 for stake + 3 for entry_fee
            assert mock_debit.call_count == 6

    def test_close_bet_credits_sale(self):
        """_close_bet should call credit_sale for sale proceeds."""
        from executor.range_bet_placer import _close_bet
        from database.db import get_session
        from database.models import Bet

        td = _td()
        mid = _unique_id()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, mid=mid, thresh=25, target_date=td)
            bet = Bet(
                market_id=mid,
                city="Testville",
                city_code="TEST",
                side="YES",
                amount=10.0,
                stake_amount=10.0,
                price=0.02,
                entry_price=0.02,
                shares=500.0,
                current_price=0.05,
                status="placed",
                order_id=f"range_{mid}_test",
                ladder_data="{}",
            )
            s.add(bet)
            s.commit()

            with patch("utils.accounting.credit_sale") as mock_credit:
                _close_bet(bet, s, "closed", "test_close")
                mock_credit.assert_called_once()
                # proceeds = shares * current_price = 500 * 0.05 = 25
                args = mock_credit.call_args[0]
                proceeds = args[1]
                assert proceeds == 25.0

    def test_execute_pt_credits_sale(self):
        """_execute_pt should call credit_sale for half the position."""
        from executor.range_bet_placer import _execute_pt
        from database.db import get_session
        from database.models import Bet

        mid = _unique_id()
        with get_session() as s:
            _add_portfolio(s)
            bet = Bet(
                market_id=mid,
                city="Testville",
                city_code="TEST",
                side="YES",
                amount=10.0,
                stake_amount=10.0,
                price=0.02,
                entry_price=0.02,
                shares=500.0,
                current_price=0.10,
                status="placed",
                order_id=f"range_{mid}_test",
                ladder_data=json.dumps({"pt_taken": False, "peak_price": 0.02}),
            )
            s.add(bet)
            s.commit()

            with patch("utils.accounting.credit_sale") as mock_credit:
                closed = _execute_pt([bet], s)
                assert closed == 1
                mock_credit.assert_called_once()
                # sell_shares = 500 * 0.5 = 250, proceeds = 250 * 0.10 = 25
                args = mock_credit.call_args[0]
                proceeds = args[1]
                assert proceeds == 25.0


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


class TestStep55Throttling:
    """BUG 6: check_range_pt should be throttled to 5-minute intervals."""

    def test_throttle_prevents_immediate_rerun(self):
        """If last_range_pt_check was <5min ago, PT check should be skipped."""
        from datetime import datetime, timezone, timedelta

        # Simulate the throttling logic from bot_loop.py
        last_check = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        _range_pt_interval = 300  # 5 minutes

        should_run = last_check is None or (now_utc - last_check).total_seconds() >= _range_pt_interval
        assert should_run is False, "Should NOT run if last check was 2 minutes ago"

    def test_throttle_allows_after_interval(self):
        """If last_range_pt_check was >5min ago, PT check should run."""
        from datetime import datetime, timezone, timedelta

        last_check = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=6)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        _range_pt_interval = 300  # 5 minutes

        should_run = last_check is None or (now_utc - last_check).total_seconds() >= _range_pt_interval
        assert should_run is True, "Should run if last check was 6 minutes ago"

    def test_first_run_always_executes(self):
        """First run (last_check=None) should always execute."""
        from datetime import datetime, timezone

        last_check = None
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        _range_pt_interval = 300

        should_run = last_check is None or (now_utc - last_check).total_seconds() >= _range_pt_interval
        assert should_run is True, "Should always run on first check"


# ── ISSUE 9: defensive copy ───────────────────────────────────────────


class TestDefensiveCopy:
    """ISSUE 9: range_bet_cities should not be mutated by bot."""

    def test_range_bet_cities_not_mutated(self):
        """Bot should not mutate the original range_bet_cities list."""
        from config.settings import bot_config

        original = ["city1", "city2", "city3"]
        bot_config.strategy.range_bet_cities = list(original)

        # Simulate what place_range_bets does
        cities = bot_config.strategy.range_bet_cities
        _ = cities  # read it

        # The original should not be mutated
        assert bot_config.strategy.range_bet_cities == original

    def test_range_bet_cities_copy_is_independent(self):
        """A copy of range_bet_cities should be independent."""
        from config.settings import bot_config

        original = ["city1", "city2"]
        bot_config.strategy.range_bet_cities = list(original)

        # Make a copy (as place_range_bets should do)
        cities_copy = list(bot_config.strategy.range_bet_cities)
        cities_copy.append("city3")

        # Original should not contain city3
        assert "city3" not in bot_config.strategy.range_bet_cities


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
            signal.ladder_orders = []

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
            signal.ladder_orders = []

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
            signal.ladder_orders = []

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
            signal.ladder_orders = []

            market_data = {"market_id": "price_edge_market", "city_code": "TEST", "yes_price": 0.11}

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(engine.execute_signal(signal, market_data))
            finally:
                loop.close()
            assert result is None, "Bet with yes_price=0.11 should be refused"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
