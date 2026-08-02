"""Test cases for probability functions migrated to utils/probability."""

from utils.probability import estimate_probability


def test_normal_cdf():
    # P(T > 25 | mean=25, std=1) = 0.5
    prob = estimate_probability(mean=25.0, std=1.0, threshold=25.0, market_type="HIGH")
    assert 0.45 <= prob <= 0.55
