"""Integration test: DrawdownMonitor is wired into BettingEngine's live sizing.

When the bankroll falls from its peak, the monitor must de-risk (scale size
down) on yellow and refuse new bets entirely on critical drawdown.
"""

import pytest

from config.settings import Config


def _make_engine():
    from engine.strategy import BettingEngine, RiskManager

    cfg = Config  # global config exposes MIN_BET_SIZE / MAX_BET_PCT
    rm = RiskManager(db_session=None, cfg=cfg)
    engine = BettingEngine(db_session=None, risk_manager=rm)
    return engine, rm


def _signal(model_prob=0.7, price=0.5):
    return {
        "market_price": price,
        "model_prob": model_prob,
        "edge": 0.2,
        "market_id": "X1",
        "city_code": "TEST",
        "city": "Test",
    }


def _equity(rm, value, peak):
    rm._conservative_portfolio_value = lambda: value
    rm.drawdown._peak = peak
    rm.drawdown._current = value


def test_green_drawdown_does_not_shrink_size():
    engine, rm = _make_engine()
    _equity(rm, 1000.0, 1000.0)  # no drawdown
    size = engine.calculate_position_size(_signal(), 1000.0, rm)
    assert size > 0
    assert rm.drawdown.alpha_multiplier() == 1.0


def test_yellow_drawdown_halves_size():
    engine, rm = _make_engine()
    _equity(rm, 900.0, 1000.0)  # -10% -> yellow, alpha 0.5
    half = engine.calculate_position_size(_signal(), 900.0, rm)
    assert half > 0
    assert rm.drawdown.alpha_multiplier() == 0.5

    # Neutral peak to read the unscaled size for comparison.
    engine2, rm2 = _make_engine()
    _equity(rm2, 900.0, 900.0)
    full = engine2.calculate_position_size(_signal(), 900.0, rm2)
    assert half == pytest.approx(full * 0.5, rel=1e-6)


def test_critical_drawdown_halts_new_bets():
    engine, rm = _make_engine()
    _equity(rm, 690.0, 1000.0)  # -31% -> critical, halt
    size = engine.calculate_position_size(_signal(), 690.0, rm)
    assert size == 0.0
    assert rm.drawdown.halt() is True
