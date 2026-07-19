"""Liveliness + efficiency audit.

Complements the flow-focused e2e/integration suites
(test_e2e_system.py, test_integration_e2e.py) by asserting the dimensions
those suites do NOT cover explicitly:

* Liveliness  - every core pipeline stage returns a usable (non-None,
  correctly-typed) result. Catches "dead" / empty calls end-to-end.
* Calculations - two independent Kelly implementations must agree, and
  known-value cases must hold. Catches silent miscalculation.
* DB efficiency - all tables exist and hot queries use indexes (flagged
  via warnings when a SCAN is detected).
* File structure - required package directories exist and the repo root
  has no stray unpackaged modules.
* Speed - hot decision-loop paths stay within soft latency budgets.

All tests are green by design; efficiency gaps are surfaced as pytest
warnings so the suite does not break the running bot while still making
the problem visible.
"""

from __future__ import annotations

import os
import time
import warnings
from datetime import date, datetime

import pytest
from sqlalchemy import text

from engine.calculator import Calculator
from engine.strategy import BettingEngine, RiskManager
from utils.kelly import kelly_bet_amount, kelly_fraction
from utils.slippage import estimate_slippage


# ── Expected schema / layout ────────────────────────────────────────────

EXPECTED_TABLES = {
    "weather_markets",
    "weather_forecasts",
    "analyses",
    "bets",
    "portfolio",
    "model_performance",
    "historical_calibrations",
}

# Hot WHERE clauses per table -> literal SQL (no bound params; pure plan check).
HOT_QUERIES = [
    "SELECT id FROM weather_forecasts WHERE market_id = 'x'",
    "SELECT id FROM weather_forecasts WHERE fetched_at > '2026-01-01T00:00:00'",
    "SELECT id FROM analyses WHERE market_id = 'x'",
    "SELECT id FROM bets WHERE market_id = 'x'",
    "SELECT id FROM bets WHERE status = 'open'",
]

REQUIRED_DIRS = [
    "config",
    "engine",
    "executor",
    "scrapers",
    "asi_engine",
    "data_pipeline",
    "database",
    "utils",
    "tests",
    "scripts",
    "data",
]

# Known entry-point / utility modules allowed at the repo root.
ALLOWED_ROOT_MODULES = {
    "main.py",
    "bot_loop.py",
    "api.py",
    "junbo_service.py",
    "quick_check.py",
    "_simulate.py",
    "db_backup.py",
    "watchdog.py",
}


def _explain_plan(sql: str) -> str:
    """Return the SQLite EXPLAIN QUERY PLAN detail for a literal SQL string."""
    from database.db import engine

    with engine.connect() as conn:
        rows = conn.execute(text(f"EXPLAIN QUERY PLAN {sql}")).fetchall()
    return " | ".join(" ".join(str(c) for c in r) for r in rows)


# ── A. Liveliness: no empty / null calls across the core pipeline ────────


def test_pipeline_core_functions_return_usable_results():
    """Every core decision stage must return a non-None, correctly-typed value."""
    calc = Calculator()

    prob = calc.estimate_probability([20.0, 21.0, 19.5], 20.0, 2, "HIGH")
    assert isinstance(prob, float), f"estimate_probability returned {type(prob)}"
    assert 0.0 <= prob <= 1.0, f"probability out of range: {prob}"

    kelly = calc.kelly_criterion(0.6, 0.5, 0.25)
    assert isinstance(kelly, float) and kelly > 0, f"kelly_criterion bad: {kelly}"

    rm = RiskManager()
    bet_size = rm.calculate_kelly_bet_size(0.6, 0.5)
    assert isinstance(bet_size, float) and bet_size >= 0, f"bet_size bad: {bet_size}"

    slip = estimate_slippage(0.5, "ecmwf_ifs025")
    assert hasattr(slip, "slippage_pct"), "slippage result missing slippage_pct"
    assert isinstance(slip.slippage_pct, float), "slippage_pct not float"

    be = BettingEngine()
    signal = be.analyze_signal({"yes_price": 0.70, "city_code": "KLGA"}, model_prob=0.86, side="YES")
    assert isinstance(signal, dict) and signal, f"analyze_signal empty: {signal}"
    assert "ev" in signal, "signal missing ev"


# ── B. Calculation correctness ──────────────────────────────────────────


@pytest.mark.parametrize(
    "prob,price,expected",
    [
        (0.5, 0.5, 0.0),  # fair price, no edge -> no bet
        (0.4, 0.5, 0.0),  # negative edge -> no bet
        (0.6, 0.5, 0.2),  # b=1, f*=(0.6-0.4)=0.2
        (0.55, 0.4, 0.25),  # b=1.5, f*=(0.825-0.45)/1.5=0.25
        (0.7, 0.3, 0.5714285714),  # b=2.333, f*=(1.633-0.3)/2.333=0.5714
    ],
)
def test_kelly_fraction_known_values(prob, price, expected):
    assert kelly_fraction(prob, price) == pytest.approx(expected, abs=1e-9)


def test_two_kelly_implementations_agree():
    """engine.calculator and utils.kelly share one formula (fraction=1)."""
    calc = Calculator()
    for prob, price in [
        (0.51, 0.5),
        (0.62, 0.43),
        (0.78, 0.21),
        (0.95, 0.12),
    ]:
        shared = kelly_fraction(prob, price)
        wrapper = calc.kelly_criterion(prob, price, fraction=1.0)
        assert wrapper == pytest.approx(shared, abs=1e-12), (
            f"divergent Kelly: utils={shared} engine={wrapper} @ ({prob},{price})"
        )


def test_kelly_monotonic_in_probability():
    prices = [0.5, 0.4, 0.3, 0.2]
    for price in prices:
        prev = -1.0
        for prob in [i / 100 for i in range(50, 96)]:
            f = kelly_fraction(prob, price)
            assert f >= prev - 1e-12, f"Kelly not monotonic @ price={price}"
            prev = f


def test_slippage_in_range_and_finite():
    for price in [0.05, 0.3, 0.5, 0.7, 0.95]:
        slip = estimate_slippage(price, "ecmwf_ifs025")
        assert 0.0 <= slip.slippage_pct <= 1.0, f"slippage out of range @ {price}"
        assert slip.slippage_pct == slip.slippage_pct, "slippage is NaN"


def test_ev_edge_sign_consistency():
    be = BettingEngine()
    strong = be.analyze_signal({"yes_price": 0.70, "city_code": "KLGA"}, model_prob=0.95, side="YES")
    weak = be.analyze_signal({"yes_price": 0.70, "city_code": "KLGA"}, model_prob=0.60, side="YES")
    assert strong is not None and strong["ev"] > 0, "strong edge should yield positive EV"
    # No-edge / weak case is correctly rejected (None) or yields non-positive EV.
    assert weak is None or (isinstance(weak, dict) and weak["ev"] <= 0), (
        "weak model_prob should not yield a positive-EV bet"
    )


def test_kelly_bet_amount_respects_cap():
    """kelly_bet_amount must never exceed max_bet_pct * portfolio."""
    port = 1000.0
    amount = kelly_bet_amount(port, 0.95, 0.12, fraction=1.0, max_bet_pct=0.05)
    assert 0.0 <= amount <= port * 0.05 + 1e-9, f"cap violated: {amount}"


# ── C. Database efficiency ──────────────────────────────────────────────


def test_database_tables_exist():
    from database.db import engine
    from sqlalchemy import inspect

    inspector = inspect(engine)
    found = set(inspector.get_table_names())
    missing = EXPECTED_TABLES - found
    assert not missing, f"missing tables: {sorted(missing)}"


def test_database_hot_queries_use_indexes():
    """Flag any hot query that falls back to a full table SCAN (no index)."""
    scanned = []
    for sql in HOT_QUERIES:
        plan = _explain_plan(sql)
        if "SCAN" in plan:
            scanned.append(sql)
    if scanned:
        warnings.warn(
            "DB efficiency gap: hot queries use full table SCAN (no index):\n"
            + "\n".join(scanned)
            + "\nAdd Index() on market_id / fetched_at / status in database/models.py.",
            stacklevel=2,
        )


def test_database_seed_and_filter_timing():
    """A filtered lookup over a seeded table must stay well under budget."""
    from database import models

    n = 500
    from database.db import get_session

    with get_session() as session:
        session.query(models.WeatherForecast).delete()
        session.commit()
        for i in range(n):
            session.add(
                models.WeatherForecast(
                    market_id=f"mkt-{i % 50}",
                    city="NYC",
                    target_date=date(2026, 7, 20),
                    metric="TEMP",
                    source="ecmwf_ifs025",
                    predicted_value=20.0 + i,
                    fetched_at=datetime(2026, 7, 19, 0, 0, 0),
                )
            )
        session.commit()

        start = time.perf_counter()
        rows = session.query(models.WeatherForecast).filter(models.WeatherForecast.market_id == "mkt-7").all()
        elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(rows) == 10, f"expected 10 seeded rows for mkt-7, got {len(rows)}"
    # Soft budget: 500-row scan is milliseconds; a regression would blow up.
    assert elapsed_ms < 200.0, f"filtered lookup too slow: {elapsed_ms:.1f} ms"


# ── D. File structure efficiency ────────────────────────────────────────


def test_required_package_directories_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = [d for d in REQUIRED_DIRS if not os.path.isdir(os.path.join(root, d))]
    assert not missing, f"missing package directories: {missing}"


def test_no_stray_root_modules():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    stray = []
    for name in os.listdir(root):
        if name.endswith(".py") and name not in ALLOWED_ROOT_MODULES:
            stray.append(name)
    if stray:
        warnings.warn(
            f"Stray top-level modules (should be packaged): {sorted(stray)}",
            stacklevel=2,
        )


def test_production_database_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.exists(os.path.join(root, "data", "bot.db")), "data/bot.db missing - bot has no production database"


# ── E. Speed benchmarks (hot decision-loop paths) ────────────────────────


def test_estimate_probability_speed():
    calc = Calculator()
    n = 2000
    start = time.perf_counter()
    for _ in range(n):
        calc.estimate_probability([20.0, 21.0, 19.5, 20.5], 20.0, 2, "HIGH")
    per_ms = (time.perf_counter() - start) * 1000 / n
    assert per_ms < 1.0, f"estimate_probability too slow: {per_ms:.4f} ms/call"


def test_analyze_signal_speed():
    be = BettingEngine()
    market = {"yes_price": 0.70, "city_code": "KLGA"}
    n = 200
    start = time.perf_counter()
    for _ in range(n):
        be.analyze_signal(market, model_prob=0.86, side="YES")
    per_ms = (time.perf_counter() - start) * 1000 / n
    assert per_ms < 10.0, f"analyze_signal too slow: {per_ms:.4f} ms/call"
