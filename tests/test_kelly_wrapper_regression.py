"""Regression test for the kelly_criterion wrapper bug.

PR #9 refactored ``Calculator.kelly_criterion`` to delegate to
``utils.kelly.kelly_fraction`` but in the wrapper did
``kelly_fraction(prob, 1.0/odds)``, treating the second argument as a
decimal odd. The in-process callers (lines 136/148 of
``engine/calculator.py``) actually pass a *market price* (0..1) -- so
for any market with a YES price <= ~0.10, the wrapper computed
``1.0/0.10 = 10.0`` and ``kelly_fraction`` rejected it as out of
range, returning 0. Result: every Polymarket temperature market
priced <= 10% got ``recommended_amount = 0.0`` and the bot placed
zero bets even when the model edge was 80%+.

These tests pin the contract:

* the wrapper takes a *price* (0..1), not a decimal odd;
* a Polymarket-shaped input (high model prob, low market price)
  produces a positive Kelly fraction, not zero.
"""

import pytest

from engine.calculator import Calculator


def _c():
    return Calculator()


def test_high_prob_low_price_returns_positive_kelly():
    """The exact shape of a Polymarket YES @ 1% with strong model
    conviction (84% probability that the threshold is exceeded)."""
    k = _c().kelly_criterion(prob=0.8458, price=0.01, fraction=0.15)
    # 15% of (99 * 0.8458 - 0.1542) / 99 ~ 0.127
    assert k > 0.10
    assert k < 0.20


def test_mid_prob_mid_price_returns_positive_kelly():
    k = _c().kelly_criterion(prob=0.70, price=0.50, fraction=0.15)
    # 15% of (1 * 0.70 - 0.30) / 1 = 0.06
    assert abs(k - 0.06) < 0.001


def test_high_prob_high_price_returns_near_zero():
    """A near-certain bet at near-1 price has no edge -> Kelly says no."""
    k = _c().kelly_criterion(prob=0.99, price=0.99, fraction=0.15)
    assert k < 0.01


def test_low_prob_high_price_returns_zero():
    """No edge (prob < price) -> no bet."""
    assert _c().kelly_criterion(prob=0.05, price=0.50) == 0.0
    assert _c().kelly_criterion(prob=0.10, price=0.80) == 0.0


def test_rejects_out_of_range_price():
    """Price must be in (0, 1) -- out-of-range inputs return 0, not crash."""
    assert _c().kelly_criterion(prob=0.50, price=0.0) == 0.0
    assert _c().kelly_criterion(prob=0.50, price=1.0) == 0.0
    assert _c().kelly_criterion(prob=0.50, price=1.5) == 0.0
    assert _c().kelly_criterion(prob=0.50, price=-0.1) == 0.0


def test_matches_original_implementation():
    """The wrapper must reproduce the pre-PR-9 formula so any
    historical paper-trades or backtests stay reproducible."""
    # Original formula: b = 1/price - 1; f = (b*p - (1-p))/b
    # k = f * fraction. For prob=0.6, price=0.4, fraction=0.15:
    #   b = 1/0.4 - 1 = 1.5
    #   f = (1.5*0.6 - 0.4) / 1.5 = 0.5/1.5 = 0.333
    #   k = 0.333 * 0.15 = 0.05
    k = _c().kelly_criterion(prob=0.6, price=0.4, fraction=0.15)
    assert abs(k - 0.05) < 0.001


@pytest.mark.parametrize(
    "prob,price,expected",
    [
        (0.55, 0.50, 0.015),  # edge 5%: 15% of 0.10 = 0.015
        (0.60, 0.40, 0.05),  # 1.5*0.6-0.4=0.5; 0.5/1.5*0.15=0.05
        (0.75, 0.20, 0.103125),  # 4*0.75-0.25=2.75; 2.75/4*0.15=0.103125
        (0.90, 0.10, 0.1333),  # 9*0.9-0.1=8; 8/9*0.15=0.1333
    ],
)
def test_polymarket_shaped_inputs(prob, price, expected):
    """Verify the wrapper against the pre-PR-9 formula across a
    grid of Polymarket-shaped (prob, price) tuples."""
    k = _c().kelly_criterion(prob=prob, price=price, fraction=0.15)
    assert abs(k - expected) < 0.001
