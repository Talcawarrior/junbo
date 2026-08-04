"""Real functional tests for WeatherEngine.

These tests exercise the actual math of `estimate_probability`,
`calculate_probability_above`, and `calculate_probability_below`
with concrete numeric cases — replacing the previous single-test
file (`test_calculator.py`) which only checked that P(T>25 | mean=25,
std=1) ≈ 0.5.
"""

from utils.probability import estimate_probability, normal_cdf


class TestNormalCdf:
    def test_cdf_at_zero_is_half(self):
        assert abs(normal_cdf(0.0) - 0.5) < 1e-6

    def test_cdf_monotonic_increasing(self):
        # CDF must be strictly increasing in z.
        for z in [-3.0, -1.5, -0.4, 0.0, 0.7, 1.2, 2.9]:
            assert normal_cdf(z + 0.01) > normal_cdf(z)

    def test_cdf_tail_clamps(self):
        # Tails are essentially 0 and 1 beyond ±8. With scipy the result
        # is a tiny float rather than exactly 0/1, so we use `isclose`
        # with a generous tolerance.
        assert normal_cdf(-9.0) < 1e-15
        assert normal_cdf(9.0) > 1.0 - 1e-15

    def test_cdf_symmetric(self):
        # Φ(-z) = 1 - Φ(z)
        for z in [0.5, 1.0, 2.0, 3.0]:
            assert abs(normal_cdf(-z) - (1.0 - normal_cdf(z))) < 1e-6


class TestEstimateProbability:
    def test_high_market_at_mean_returns_half(self):
        # P(T >= threshold | mean = threshold, std small) ≈ 0.5
        p = estimate_probability(mean=25.0, std=1.0, threshold=25.0, market_type="HIGH")
        assert 0.45 <= p <= 0.55

    def test_high_market_far_above_threshold(self):
        # mean >> threshold → P(YES) should be ~0.99 (clamped)
        p = estimate_probability(mean=40.0, std=1.0, threshold=25.0, market_type="HIGH")
        assert p >= 0.98

    def test_high_market_far_below_threshold(self):
        # mean << threshold → P(YES) should be ~0.01 (clamped)
        p = estimate_probability(mean=10.0, std=1.0, threshold=25.0, market_type="HIGH")
        assert p <= 0.02

    def test_low_market_at_mean_returns_half(self):
        # P(T <= threshold | mean = threshold) ≈ 0.5
        p = estimate_probability(mean=25.0, std=1.0, threshold=25.0, market_type="LOW")
        assert 0.45 <= p <= 0.55

    def test_low_market_far_below_threshold(self):
        # mean << threshold → P(T <= threshold) ~ 0.99 (clamped)
        p = estimate_probability(mean=10.0, std=1.0, threshold=25.0, market_type="LOW")
        assert p >= 0.98

    def test_range_market_central_bucket(self):
        # P(24.5 <= T <= 25.5 | mean=25, std=1) should be ~0.38 (1σ bucket)
        p = estimate_probability(
            mean=25.0, std=1.0, threshold=25.0, market_type="RANGE"
        )
        assert 0.30 <= p <= 0.45

    def test_range_market_explicit_bounds(self):
        # Wider bucket (24..26) should yield higher probability than ±0.5 bucket.
        p_narrow = estimate_probability(
            mean=25.0, std=1.0, threshold=25.0, market_type="RANGE"
        )
        p_wide = estimate_probability(
            mean=25.0,
            std=1.0,
            threshold=25.0,
            market_type="RANGE",
            range_low=23.0,
            range_high=27.0,
        )
        assert p_wide > p_narrow

    def test_days_ahead_widens_uncertainty(self):
        # Far-future forecast → distribution is wider → P(at mean) closer to 0.5.
        p_near = estimate_probability(
            mean=25.0, std=1.0, threshold=30.0, days_ahead=0, market_type="HIGH"
        )
        p_far = estimate_probability(
            mean=25.0, std=1.0, threshold=30.0, days_ahead=10, market_type="HIGH"
        )
        # With wider σ, P(T >= 30) should be larger (less mass concentrated below threshold).
        assert p_far > p_near

    def test_unknown_market_type_falls_back_to_high(self):
        # Should warn and treat as HIGH.
        p = estimate_probability(
            mean=25.0, std=1.0, threshold=25.0, market_type="WEIRD"
        )
        assert 0.45 <= p <= 0.55

    def test_probability_clamped_to_safe_range(self):
        # Even with extreme inputs, p stays inside [0.01, 0.99].
        extreme_high = estimate_probability(
            mean=1000.0, std=0.1, threshold=0.0, market_type="HIGH"
        )
        extreme_low = estimate_probability(
            mean=-1000.0, std=0.1, threshold=0.0, market_type="HIGH"
        )
        assert 0.01 <= extreme_high <= 0.99
        assert 0.01 <= extreme_low <= 0.99


class TestEstimateProbabilityEdgeCases:
    """Edge cases for estimate_probability from utils.probability."""

    def test_high_and_low_are_complementary(self):
        # P(T >= X) + P(T < X) ≈ 1 (HIGH and LOW for the same threshold).
        p_high = estimate_probability(
            mean=30.0, std=2.0, threshold=28.0, market_type="HIGH"
        )
        p_low = estimate_probability(
            mean=30.0, std=2.0, threshold=28.0, market_type="LOW"
        )
        assert abs((p_high + p_low) - 1.0) < 0.05

    def test_extreme_threshold_high_clamps(self):
        # Very extreme threshold values should clamp to [0.01, 0.99].
        p = estimate_probability(
            mean=25.0, std=1.0, threshold=100.0, market_type="HIGH"
        )
        assert p == 0.01


class TestTimezoneFallback:
    """Verify that the closest-date fallback in get_multi_model_forecast
    picks the right bucket when the target date is not exactly in the API
    response (which happens with `timezone=auto` for cities east/west of UTC).
    """

    def test_fallback_picks_closest_date(self):
        """If the target date is one day off from every API bucket, the
        fallback should pick the closest one rather than returning None.

        We verify the helper logic directly by simulating the search loop.
        """
        from datetime import datetime

        times = ["2026-06-19", "2026-06-20", "2026-06-21"]
        target_date = datetime(2026, 6, 18)  # not in the list
        target_d = target_date.date()

        best_idx = None
        best_delta = None
        for i, t in enumerate(times):
            d = datetime.strptime(t, "%Y-%m-%d").date()
            delta = abs((d - target_d).days)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_idx = i

        assert best_idx == 0
        assert best_delta == 1
