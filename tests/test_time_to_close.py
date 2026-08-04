"""Time-to-close edge escalation tests (Tier 3 #13).

Validates ``compute_effective_min_edge`` from ``utils.probability``:
- Above window: full min_edge (1x)
- Boundary (==esc_h): full min_edge
- Mid-window: 1x + (mult-1) * (1 - hours_left/esc_h) * min_edge
- At 0h: max mult * min_edge
- Past resolution: clamped to mult * min_edge
- No resolution date attribute: full min_edge
- esc_h = 0: avoids divide-by-zero
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.settings import bot_config
from utils.probability import compute_effective_min_edge


def _market(hours_from_now: float) -> SimpleNamespace:
    return SimpleNamespace(
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    )


def test_above_window_returns_min_edge():
    m = _market(hours_from_now=48)
    assert compute_effective_min_edge(m) == bot_config.strategy.min_edge


def test_boundary_returns_min_edge():
    m = _market(hours_from_now=bot_config.strategy.edge_escalation_hours)
    assert compute_effective_min_edge(m) == bot_config.strategy.min_edge


def test_midpoint_returns_linear_interp():
    half = bot_config.strategy.edge_escalation_hours / 2
    m = _market(hours_from_now=half)
    expected = bot_config.strategy.min_edge * (
        1.0 + (bot_config.strategy.edge_escalation_multiplier - 1.0) * 0.5
    )
    actual = compute_effective_min_edge(m)
    assert abs(actual - expected) < 1e-6


def test_at_close_returns_max_multiplier():
    m = _market(hours_from_now=0)
    expected = (
        bot_config.strategy.min_edge * bot_config.strategy.edge_escalation_multiplier
    )
    assert abs(compute_effective_min_edge(m) - expected) < 1e-9


def test_past_resolution_clamped_to_max():
    m = _market(hours_from_now=-5)
    expected = (
        bot_config.strategy.min_edge * bot_config.strategy.edge_escalation_multiplier
    )
    assert abs(compute_effective_min_edge(m) - expected) < 1e-9


def test_no_resolution_attribute_returns_min_edge():
    m = SimpleNamespace()  # no resolution_date / target_date
    assert compute_effective_min_edge(m) == bot_config.strategy.min_edge


def test_naive_datetime_treated_as_utc():
    m = SimpleNamespace(
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=48)  # no tzinfo
    )
    assert compute_effective_min_edge(m) == bot_config.strategy.min_edge


def test_zero_escalation_hours_does_not_div_by_zero():
    """If edge_escalation_hours is 0, the function should still not divide by zero."""
    from config.settings import StrategyConfig

    s = StrategyConfig(edge_escalation_hours=0, edge_escalation_multiplier=2.0)
    original = bot_config.strategy
    try:
        bot_config.strategy = s
        # 10h > 1h (clamped), so returns min_edge
        m = _market(hours_from_now=10)
        assert compute_effective_min_edge(m) == s.min_edge
    finally:
        bot_config.strategy = original
