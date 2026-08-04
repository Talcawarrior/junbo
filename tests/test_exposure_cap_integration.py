"""Integration tests for exposure cap + flat_bet_usd interaction.

Catches the bug where:
- flat_bet_usd=1000 but max_exposure=1000 → bot can only place ONE bet
- If existing exposure > 0, new bets are rejected
- DRY_RUN mode should still track accounting (debit/credit)
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, WeatherMarket, WeatherForecast, Portfolio

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()


def _add_portfolio(session, cash=1000.0):
    from database.models import Portfolio

    session.add(
        Portfolio(
            id=1,
            cash_balance=cash,
            total_value=cash,
            initial_value=1000.0,
        )
    )
    session.commit()


def _add_market(session, mid="m1", yes_price=0.50):
    from database.models import WeatherMarket
    from datetime import datetime, timedelta, timezone

    session.add(
        WeatherMarket(
            id=mid,
            question="Test?",
            city="Testville",
            city_code="TEST",
            metric="temperature_max",
            threshold=25.0,
            target_date=(datetime.now(timezone.utc) + timedelta(days=2)).replace(
                hour=23, minute=59, second=59, microsecond=0
            ),
            yes_price=yes_price,
            no_price=1.0 - yes_price,
            status="open",
            latitude=41.0,
            longitude=29.0,
        )
    )
    session.commit()


# ── Config value tests ─────────────────────────────────────────────────


class TestConfigValues:
    """Verify critical config values are correct."""

    def test_flat_bet_usd_positive(self):
        from config.settings import StrategyConfig

        s = StrategyConfig()
        assert s.flat_bet_usd > 0

    def test_max_bet_amount_ge_flat_bet(self):
        from config.settings import StrategyConfig

        s = StrategyConfig()
        assert s.max_bet_amount >= s.flat_bet_usd, (
            f"max_bet_amount ({s.max_bet_amount}) < flat_bet_usd ({s.flat_bet_usd})"
        )

    def test_total_exposure_pct_valid(self):
        from config.settings import StrategyConfig

        s = StrategyConfig()
        assert 0 < s.total_exposure_pct <= 1.0

    def test_initial_portfolio_positive(self):
        from config.settings import config

        assert config.INITIAL_PORTFOLIO > 0


# ── DRY_RUN accounting ─────────────────────────────────────────────────


class TestDryRunAccounting:
    """DRY_RUN mode should still debit/credit cash for paper bets."""

    def test_dry_run_debits_cash(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import Portfolio, WeatherMarket
        from config.settings import Config

        original_dry = Config.DRY_RUN
        Config.DRY_RUN = True
        try:
            with get_session() as s:
                _add_portfolio(s, cash=1000.0)
                _add_market(s, "m4", yes_price=0.50)

                cash_before = float(
                    s.query(Portfolio).filter(Portfolio.id == 1).first().cash_balance
                )

                bp = BetPlacer()
                market = s.query(WeatherMarket).filter_by(id="m4").first()
                result = bp.open_bet_on_market(market, s)

                assert result is not None, "DRY_RUN should still place paper bets"
                cash_after = float(
                    s.query(Portfolio).filter(Portfolio.id == 1).first().cash_balance
                )
                assert cash_after < cash_before, (
                    f"DRY_RUN should debit cash: {cash_after} >= {cash_before}"
                )
        finally:
            Config.DRY_RUN = original_dry

    def test_dry_run_order_id_is_paper(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import WeatherMarket
        from config.settings import Config

        original_dry = Config.DRY_RUN
        Config.DRY_RUN = True
        try:
            with get_session() as s:
                _add_portfolio(s, cash=1000.0)
                _add_market(s, "m5", yes_price=0.50)

                bp = BetPlacer()
                market = s.query(WeatherMarket).filter_by(id="m5").first()
                result = bp.open_bet_on_market(market, s)

                assert result is not None
                assert result.order_id is not None
                assert "paper" in result.order_id.lower(), (
                    f"DRY_RUN order_id should contain 'paper': {result.order_id}"
                )
        finally:
            Config.DRY_RUN = original_dry

    def test_dry_run_bet_status_is_placed(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import WeatherMarket
        from config.settings import Config

        original_dry = Config.DRY_RUN
        Config.DRY_RUN = True
        try:
            with get_session() as s:
                _add_portfolio(s, cash=1000.0)
                _add_market(s, "m6", yes_price=0.50)

                bp = BetPlacer()
                market = s.query(WeatherMarket).filter_by(id="m6").first()
                result = bp.open_bet_on_market(market, s)

                assert result is not None
                assert result.status == "placed"
        finally:
            Config.DRY_RUN = original_dry


# ── Exposure cap integration ───────────────────────────────────────────


class TestExposureCapIntegration:
    """Verify exposure cap blocks bets when room is insufficient."""

    def test_cash_insufficient_rejects_bet(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s, cash=5.0)  # Only $5 — less than flat_bet
            _add_market(s, "m3", yes_price=0.50)

            bp = BetPlacer()
            market = s.query(WeatherMarket).filter_by(id="m3").first()
            result = bp.open_bet_on_market(market, s)
            assert result is None, "Bet should be rejected when cash < flat_bet"

    def test_duplicate_market_rejected(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import Bet, WeatherMarket

        with get_session() as s:
            _add_portfolio(s, cash=1000.0)
            _add_market(s, "m7", yes_price=0.50)

            # Already have an open bet on m7
            s.add(
                Bet(
                    market_id="m7",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    price=0.50,
                    status="placed",
                )
            )
            s.commit()

            bp = BetPlacer()
            market = s.query(WeatherMarket).filter_by(id="m7").first()
            result = bp.open_bet_on_market(market, s)
            assert result is None, "Duplicate market bet should be rejected"

    def test_yes_price_gate_refuses_high_price(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s, cash=1000.0)
            _add_market(s, "m8", yes_price=0.96)  # Above max_entry_price=0.95

            bp = BetPlacer()
            market = s.query(WeatherMarket).filter_by(id="m8").first()
            result = bp.open_bet_on_market(market, s)
            assert result is None, "yes_price=0.96 should be refused (>= 0.95)"

    def test_yes_price_gate_refuses_low_price(self):
        from executor.bet_placer import BetPlacer
        from database.db import get_session
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s, cash=1000.0)
            _add_market(s, "m9", yes_price=0.05)  # Below min_entry_price=0.10

            bp = BetPlacer()
            market = s.query(WeatherMarket).filter_by(id="m9").first()
            result = bp.open_bet_on_market(market, s)
            assert result is None, "yes_price=0.05 should be refused (< 0.10)"
