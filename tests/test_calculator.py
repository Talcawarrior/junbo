"""Test cases for WeatherEngine calculator."""

from engine.calculator import WeatherEngine


def test_normal_cdf():
    engine = WeatherEngine()
    # P(T > 25 | mean=25, std=1) = 0.5
    consensus = {"weighted_mean": 25.0, "weighted_std": 1.0}
    prob = engine.calculate_probability_above(25.0, consensus)
    assert 0.45 <= prob <= 0.55
