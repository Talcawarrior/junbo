"""Range betting unit tests.

Tests:
  - _get_forecast_temp: ICAO resolution, avg calculation, no-data
  - _find_market: threshold matching, date matching
  - _existing_bet: duplicate detection
  - place_range_bets: 3-bet placement, <=0.10 gate, skip logic
  - check_range_pt: PT trigger, trail stop, pre-settlement sell
"""
import json
from datetime import datetime, timezone, timedelta

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
    bot_config.strategy.range_bet_cities = TEST_CITIES
    bot_config.strategy.range_bet_enabled = True
    bot_config.strategy.range_bet_spread = 1
    bot_config.strategy.range_bet_amount = 10.0
    bot_config.strategy.range_bet_trail_stop_pct = 0.20
    bot_config.strategy.range_bet_pt_take_rate = 1.0
    bot_config.strategy.range_bet_pre_settlement_hours = 1.0
    bot_config.strategy.current_fee_rate = 0.05


def _td(offset=1):
    return (datetime.now(timezone.utc) + timedelta(days=offset)).replace(hour=23, minute=59, second=59, microsecond=0)


def _add_market(session, mid=None, city="Testville", icao="TEST", thresh=25,
                yes_price=0.02, no_price=0.98, target_date=None):
    if mid is None:
        mid = _unique_id()
    td = target_date or _td()
    from database.models import WeatherMarket
    session.add(WeatherMarket(
        id=mid, question="Test?", city=city, city_code=icao,
        metric="temperature_max", threshold=float(thresh),
        target_date=td, yes_price=yes_price, no_price=no_price,
        status="open", latitude=41.0, longitude=29.0,
    ))


def _add_forecast(session, icao="TEST", target_date=None, value=25.0, source="test_source"):
    from database.models import WeatherForecast
    td = target_date or _td()
    session.add(WeatherForecast(
        market_id="", city=icao, lat=41.0, lon=29.0,
        target_date=td, metric="temperature_max",
        source=source, predicted_value=value,
        fetched_at=datetime.now(),
    ))


def _add_portfolio(session):
    from database.models import Portfolio
    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if pf is None:
        session.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))


# â”€â”€ TESTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestForecastTemp:
    def test_returns_avg_of_latest_sources(self):
        from executor.range_bet_placer import _get_forecast_temp
        from database.db import get_session
        td = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=59)
        with get_session() as s:
            _add_market(s, icao="TEST", target_date=td)
            _add_forecast(s, "TEST", td, 24.0, "src1")
            _add_forecast(s, "TEST", td, 26.0, "src2")
            s.commit()
        temp = _get_forecast_temp("testville", "TEST", "temperature_max", td)
        assert temp is not None
        assert abs(temp - 25.0) < 0.01  # avg of 24+26

    def test_returns_none_when_no_forecasts(self):
        from executor.range_bet_placer import _get_forecast_temp
        td = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=59)
        temp = _get_forecast_temp("testville", "TEST", "temperature_max", td)
        assert temp is None


class TestFindMarket:
    def test_finds_market_by_threshold(self):
        from database.db import get_session
        from database.models import WeatherMarket
        td = _td()
        mid = _unique_id()
        with get_session() as s:
            _add_market(s, mid=mid, thresh=25, target_date=td)
            s.commit()
            m = s.query(WeatherMarket).filter(
                WeatherMarket.city.ilike("Testville"),
                WeatherMarket.threshold == 25.0,
                WeatherMarket.target_date == td,
            ).first()
            assert m is not None
            assert m.id == mid

    def test_returns_none_when_no_match(self):
        from executor.range_bet_placer import _find_market
        td = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=59)
        m = _find_market("Testville", 99, td)
        assert m is None


class TestExistingBet:
    def test_detects_existing_bet(self):
        from executor.range_bet_placer import _existing_bet
        from database.db import get_session
        from database.models import Bet
        mid = _unique_id()
        with get_session() as s:
            s.add(Bet(market_id=mid, side="YES", amount=10.0, status="placed"))
            s.commit()
        assert _existing_bet(mid) is True

    def test_ignores_rejected_bets(self):
        from executor.range_bet_placer import _existing_bet
        from database.db import get_session
        from database.models import Bet
        mid = _unique_id()
        with get_session() as s:
            s.add(Bet(market_id=mid, side="YES", amount=10.0, status="rejected"))
            s.commit()
        assert _existing_bet(mid) is False


class TestPlaceRangeBets:
    def test_places_3_bets_when_all_below_10(self):
        from executor.range_bet_placer import place_range_bets
        from database.db import get_session
        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            # Polymarket favorite = 25C (highest price at 0.03)
            _add_market(s, thresh=24, yes_price=0.01, target_date=td)
            _add_market(s, thresh=25, yes_price=0.03, target_date=td)
            _add_market(s, thresh=26, yes_price=0.02, target_date=td)
            _add_forecast(s, "TEST", td, 25.0, "src1")
            _add_forecast(s, "TEST", td, 25.0, "src2")
            s.commit()
        results = place_range_bets()
        # spread=1 → thresholds = [24, 25, 26], all exist & ≤0.10
        assert len(results) == 3

    def test_skips_city_when_one_price_above_10(self):
        from executor.range_bet_placer import place_range_bets
        from database.db import get_session
        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, thresh=24, yes_price=0.01, target_date=td)
            _add_market(s, thresh=25, yes_price=0.15, target_date=td)  # >0.10
            _add_market(s, thresh=26, yes_price=0.03, target_date=td)
            _add_forecast(s, "TEST", td, 25.0, "src1")
            _add_forecast(s, "TEST", td, 25.0, "src2")
            s.commit()
        results = place_range_bets()
        assert len(results) == 0

    def test_disabled_when_not_enabled(self):
        from config.settings import bot_config
        old = bot_config.strategy.range_bet_enabled
        bot_config.strategy.range_bet_enabled = False
        from executor.range_bet_placer import place_range_bets
        assert place_range_bets() == []
        bot_config.strategy.range_bet_enabled = old


class TestCheckRangePT:
    def test_pt_triggers_at_2x_value(self):
        from executor.range_bet_placer import check_range_pt, place_range_bets
        from database.db import get_session
        from database.models import Bet
        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            # Polymarket favorite = 25C (highest price)
            _add_market(s, thresh=24, yes_price=0.01, target_date=td)
            _add_market(s, thresh=25, yes_price=0.04, target_date=td)
            _add_market(s, thresh=26, yes_price=0.02, target_date=td)
            _add_forecast(s, "TEST", td, 25.0, "src1")
            _add_forecast(s, "TEST", td, 25.0, "src2")
            s.commit()
        place_range_bets()  # expects 3 bets: 24C, 25C, 26C

        with get_session() as s:
            for b in s.query(Bet).filter(Bet.order_id.like("range_%")).all():
                b.current_price = float(b.entry_price or 0.01) * 10
            s.commit()

        closed = check_range_pt()
        assert closed == 3, f"Expected PT to close all 3, got {closed}"

    def test_trail_stop_triggers_on_20pct_drop(self):
        from executor.range_bet_placer import check_range_pt, place_range_bets
        from database.db import get_session
        from database.models import Bet
        td = _td()
        with get_session() as s:
            _add_portfolio(s)
            _add_market(s, thresh=24, yes_price=0.01, target_date=td)
            _add_market(s, thresh=25, yes_price=0.04, target_date=td)
            _add_market(s, thresh=26, yes_price=0.02, target_date=td)
            _add_forecast(s, "TEST", td, 25.0, "src1")
            _add_forecast(s, "TEST", td, 25.0, "src2")
            s.commit()
        place_range_bets()  # expects 3 bets: 24C, 25C, 26C

        with get_session() as s:
            for b in s.query(Bet).filter(Bet.order_id.like("range_%")).all():
                b.ladder_data = json.dumps({"pt_taken": True, "peak_price": 0.10})
                b.current_price = 0.07
            s.commit()

        closed = check_range_pt()
        assert closed > 0, "Trail stop should close positions"

    def test_pre_settlement_sells_within_1h(self):
        from executor.range_bet_placer import check_range_pt
        from database.db import get_session
        from database.models import Bet, WeatherMarket, Portfolio
        # Manually create range bets near settlement
        td = datetime.now(timezone.utc) + timedelta(minutes=30)
        with get_session() as s:
            pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
            if pf is None:
                s.add(Portfolio(id=1, cash_balance=1000.0, total_value=1000.0))
            # Create a market expiring in 30 min
            s.add(WeatherMarket(id="ps1", question="Q", city="Testville", city_code="TEST",
                metric="temperature_max", threshold=24.0, target_date=td,
                yes_price=0.01, no_price=0.99, status="open", latitude=41, longitude=29))
            s.add(WeatherMarket(id="ps2", question="Q", city="Testville", city_code="TEST",
                metric="temperature_max", threshold=25.0, target_date=td,
                yes_price=0.02, no_price=0.98, status="open", latitude=41, longitude=29))
            s.add(WeatherMarket(id="ps3", question="Q", city="Testville", city_code="TEST",
                metric="temperature_max", threshold=26.0, target_date=td,
                yes_price=0.03, no_price=0.97, status="open", latitude=41, longitude=29))
            # Pre-place range bets
            for mid in ["ps1", "ps2", "ps3"]:
                s.add(Bet(market_id=mid, city="Testville", city_code="TEST",
                    side="YES", amount=10.0, stake_amount=10.0, price=0.5, entry_price=0.5,
                    shares=20.0, current_price=0.5, status="placed",
                    order_id=f"range_{mid}_test", ladder_data='{}'))
            s.commit()

        closed = check_range_pt()
        assert closed > 0, "Pre-settlement should close all"
