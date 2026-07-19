"""Regression + safety tests for the per-market re-analysis throttle.

`_should_skip_analysis` decides whether re-running `analyze_market` this cycle
is a pure no-op (inputs unchanged). Skipping must NEVER change a bet decision,
so we assert the exact conditions that force a fresh analysis:
  - never analyzed before,
  - last analysis older than MAX_ANALYSIS_AGE_MIN,
  - a newer weather forecast arrived since the last analysis,
  - the price moved >= PRICE_REANALYZE_DELTA.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

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
    Portfolio,
    WeatherForecast,
    WeatherMarket,
)
from jobs.scheduler import (  # noqa: E402
    MAX_ANALYSIS_AGE_MIN,
    PRICE_REANALYZE_DELTA,
    _should_skip_analysis,
)

MARKET_ID = "throttle-m1"
METRIC = "temperature_max"
YESTERDAY = datetime.now(timezone.utc) - timedelta(days=1)


def _clean():
    with get_session() as s:
        s.query(Analysis).delete()
        s.query(WeatherForecast).delete()
        s.query(WeatherMarket).delete()
        s.query(Portfolio).delete()
        s.commit()


def _now_naive():
    """Mirror run_analyze's clock: tz-naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _setup(market_yes_price=0.35, analysis_yes_price=0.35, age_minutes=1):
    """Create a fresh open market + 1 forecast + 1 analysis.

    `analysis_yes_price` is written to Analysis.market_implied_prob;
    `age_minutes` controls how long ago the analysis "ran".
    """
    _clean()
    tomorrow = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    with get_session() as s:
        s.add(Portfolio(id=1, cash_balance=990.0, total_value=1000.0, current_value=1000.0))
        s.add(
            WeatherMarket(
                id=MARKET_ID,
                question="Will temp exceed 30C?",
                city="New York",
                city_code="KLGA",
                metric=METRIC,
                threshold=30.0,
                target_date=tomorrow,
                yes_price=market_yes_price,
                no_price=round(1.0 - market_yes_price, 2),
                status="open",
                latitude=40.7128,
                longitude=-74.0060,
            )
        )
        # One forecast, "fetched" before the analysis (so it must not trigger).
        s.add(
            WeatherForecast(
                market_id=MARKET_ID,
                city="New York",
                lat=40.7128,
                lon=-74.0060,
                target_date=tomorrow,
                metric=METRIC,
                source="gfs_seamless",
                predicted_value=31.0,
                fetched_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes + 1),
            )
        )
        s.add(
            Analysis(
                market_id=MARKET_ID,
                estimated_probability=0.5,
                market_implied_prob=analysis_yes_price,
                edge=0.1,
                raw_edge=0.1,
                avg_forecast_value=31.0,
                std_forecast_value=0.5,
                num_sources=1,
                recommended_side="YES",
                recommended_amount=1.0,
                confidence_score=0.5,
                should_bet=False,
                reason="test",
                # tz-aware, like the production default -> exercises tz stripping
                analyzed_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
            )
        )
        s.commit()


def _market(sess):
    return sess.query(WeatherMarket).filter_by(id=MARKET_ID).first()


def _set_price(sess, price):
    m = _market(sess)
    m.yes_price = price
    m.no_price = round(1.0 - price, 2)
    sess.commit()


def _add_fresh_forecast(sess):
    tomorrow = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    sess.add(
        WeatherForecast(
            market_id=MARKET_ID,
            city="New York",
            lat=40.7128,
            lon=-74.0060,
            target_date=tomorrow,
            metric=METRIC,
            source="ecmwf_ifs025",
            predicted_value=33.0,
            fetched_at=datetime.now(timezone.utc),  # newer than the analysis
        )
    )
    sess.commit()


def _set_analysis_age(sess, age_minutes):
    a = sess.query(Analysis).filter_by(market_id=MARKET_ID).order_by(Analysis.analyzed_at.desc()).first()
    a.analyzed_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    sess.commit()


def _set_forecast_age(sess, age_minutes):
    f = sess.query(WeatherForecast).filter_by(market_id=MARKET_ID).order_by(WeatherForecast.fetched_at.desc()).first()
    f.fetched_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    sess.commit()


def test_skip_when_nothing_changed():
    _setup(market_yes_price=0.35, analysis_yes_price=0.35, age_minutes=1)
    now = _now_naive()
    with get_session() as s:
        assert _should_skip_analysis(s, _market(s), now) is True
    print("PASS: skip when inputs unchanged")


def test_no_skip_when_never_analyzed():
    _clean()
    tomorrow = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    with get_session() as s:
        s.add(Portfolio(id=1, cash_balance=990.0, total_value=1000.0, current_value=1000.0))
        s.add(
            WeatherMarket(
                id=MARKET_ID,
                question="Will temp exceed 30C?",
                city="New York",
                city_code="KLGA",
                metric=METRIC,
                threshold=30.0,
                target_date=tomorrow,
                yes_price=0.35,
                no_price=0.65,
                status="open",
                latitude=40.7128,
                longitude=-74.0060,
            )
        )
        s.commit()
    now = _now_naive()
    with get_session() as s:
        assert _should_skip_analysis(s, _market(s), now) is False
    print("PASS: no skip when never analyzed")


def test_no_skip_when_price_moved():
    _setup(market_yes_price=0.40, analysis_yes_price=0.35, age_minutes=1)
    now = _now_naive()
    with get_session() as s:
        assert _should_skip_analysis(s, _market(s), now) is False
    print("PASS: no skip when price moved >= delta")


def test_no_skip_when_price_move_below_delta():
    # Move less than PRICE_REANALYZE_DELTA -> still safe to skip.
    small = 0.35 + PRICE_REANALYZE_DELTA * 0.5
    _setup(market_yes_price=small, analysis_yes_price=0.35, age_minutes=1)
    now = _now_naive()
    with get_session() as s:
        assert _should_skip_analysis(s, _market(s), now) is True
    print("PASS: skip when price move below delta")


def test_no_skip_when_new_weather():
    _setup(market_yes_price=0.35, analysis_yes_price=0.35, age_minutes=1)
    now = _now_naive()
    with get_session() as s:
        _add_fresh_forecast(s)
        assert _should_skip_analysis(s, _market(s), now) is False
    print("PASS: no skip when newer forecast arrived")


def test_no_skip_when_too_old():
    _setup(market_yes_price=0.35, analysis_yes_price=0.35, age_minutes=1)
    now = _now_naive()
    with get_session() as s:
        _set_analysis_age(s, MAX_ANALYSIS_AGE_MIN + 15)
        assert _should_skip_analysis(s, _market(s), now) is False
    print("PASS: no skip when analysis older than max age")


def test_skip_at_just_under_max_age():
    _setup(market_yes_price=0.35, analysis_yes_price=0.35, age_minutes=1)
    now = _now_naive()
    with get_session() as s:
        _set_analysis_age(s, MAX_ANALYSIS_AGE_MIN - 1)
        _set_forecast_age(s, MAX_ANALYSIS_AGE_MIN + 5)  # older than analysis
        assert _should_skip_analysis(s, _market(s), now) is True
    print("PASS: skip when analysis just under max age")
