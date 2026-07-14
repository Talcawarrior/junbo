"""Calculator._compute_effective_min_edge regression test.

Bug: Calculator.analyze_market called self._compute_effective_min_edge
which did not exist on the class (only on WeatherEngine). The bot
stalled for 6+ hours with AttributeError on every scan cycle. This
test guards the Calculator side of the same logic so the same
mistake can't be made again on the other class.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.settings import bot_config
from engine.calculator import Calculator


def _market(hours_from_now: float) -> SimpleNamespace:
    return SimpleNamespace(
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    )


def test_calculator_static_method_exists():
    """The method must be available as a static method on Calculator
    so analyze_market can call it via self."""
    c = Calculator()
    assert callable(getattr(c, "_compute_effective_min_edge", None))


def test_calculator_matches_weather_engine_behavior():
    """Calculator and WeatherEngine must produce the same answer
    for the same market so the two classes cannot drift apart."""
    from engine.calculator import WeatherEngine

    cases = [48, 24, 12, 1, 0, -5]
    for h in cases:
        m = _market(h)
        a = Calculator._compute_effective_min_edge(m)
        b = WeatherEngine._compute_effective_min_edge(m)
        assert abs(a - b) < 1e-8, f"drift at h={h}: {a} vs {b}"


def test_calculator_no_target_date_returns_min_edge():
    c = Calculator()
    m = SimpleNamespace()  # no resolution_date / target_date
    assert c._compute_effective_min_edge(m) == bot_config.strategy.min_edge


def test_calculator_zero_escalation_hours_does_not_div_by_zero():
    from config.settings import StrategyConfig

    s = StrategyConfig(edge_escalation_hours=0, edge_escalation_multiplier=2.0)
    original = bot_config.strategy
    try:
        bot_config.strategy = s
        c = Calculator()
        # 10h > 1h (clamped), so returns min_edge
        m = _market(hours_from_now=10)
        assert c._compute_effective_min_edge(m) == s.min_edge
    finally:
        bot_config.strategy = original
