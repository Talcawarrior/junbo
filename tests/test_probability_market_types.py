"""Tests for utils/probability.py — market-type-aware probability estimation.

Verifies:
  1. test_high — HIGH: mean=25, std=1, strike=23 → P > 0.9; strike=27 → P < 0.1.
  2. test_low_is_complement — P_LOW(X) ≈ 1 - P_HIGH(X) for same params.
  3. test_range_bucket — bucket ±0.5 at mean → ~0.383; shifted → < 0.01.
  4. test_range_sums_to_one — covering buckets sum > 0.99.
  5. test_low_market_signal_direction — LOW market edge positive, side YES.
  6. test_no_signal_when_fair — model_prob ≈ market → should_bet False.
"""

import sys

sys.path.insert(0, ".")

from utils.probability import estimate_probability, normal_cdf

# ── helpers ───────────────────────────────────────────────────────────────────


def _cdf_diff(z_low: float, z_high: float) -> float:
    """P(z_low < Z < z_high) for standard normal."""
    return normal_cdf(z_high) - normal_cdf(z_low)


# ── 1. HIGH market type ──────────────────────────────────────────────────────


def test_high():
    """HIGH market: mean=25, std=1, strike=23 → P > 0.9 (most mass above 23)."""
    p_high = estimate_probability(
        mean=25.0, std=1.0, threshold=23.0, market_type="HIGH"
    )
    assert p_high > 0.9, f"Expected >0.9, got {p_high}"

    # strike=27 (above mean) → P < 0.1
    p_low = estimate_probability(mean=25.0, std=1.0, threshold=27.0, market_type="HIGH")
    assert p_low < 0.1, f"Expected <0.1, got {p_low}"

    # Boundary: mean == threshold → P ≈ 0.5
    p_mid = estimate_probability(mean=25.0, std=1.0, threshold=25.0, market_type="HIGH")
    assert 0.4 <= p_mid <= 0.6, f"Expected ~0.5, got {p_mid}"


# ── 2. LOW is complement of HIGH ─────────────────────────────────────────────


def test_low_is_complement():
    """P_LOW(X) + P_HIGH(X) ≈ 1 for same parameters."""
    for mean in (15.0, 20.0, 25.0):
        for threshold in (mean - 5, mean, mean + 5):
            p_high = estimate_probability(
                mean, std=2.0, threshold=threshold, market_type="HIGH"
            )
            p_low = estimate_probability(
                mean, std=2.0, threshold=threshold, market_type="LOW"
            )
            total = p_high + p_low
            assert abs(total - 1.0) < 1e-9, (
                f"mean={mean}, threshold={threshold}: P_HIGH={p_high}, P_LOW={p_low}, sum={total}"
            )


# ── 3. RANGE bucket probabilities ────────────────────────────────────────────


def test_range_bucket():
    """RANGE ±0.5 bucket centred at mean → 2*CDF(0.5)-1 ≈ 0.383.

    The standard normal CDF at z=0.5 is ~0.6915, so
    P(-0.5 < Z < 0.5) = 0.6915 - (1-0.6915) = 0.3830.
    """
    p_range = estimate_probability(
        mean=18.0, std=1.0, threshold=18.0, market_type="RANGE"
    )
    # Expected: normal_cdf(0.5) - normal_cdf(-0.5) = 0.6915 - 0.3085 = 0.3829
    expected_half = normal_cdf(0.5)  # ~0.6915
    expected = expected_half - (1.0 - expected_half)  # ~0.383
    assert abs(p_range - expected) < 0.005, f"Expected ~{expected:.4f}, got {p_range}"

    # mean=21, threshold=18 → bucket far from mean → P <= 0.01 (clamp floor)
    p_far = estimate_probability(
        mean=21.0, std=1.0, threshold=18.0, market_type="RANGE"
    )
    assert p_far <= 0.01, f"Expected <=0.01, got {p_far}"


# ── 4. RANGE buckets sum to ~1 ────────────────────────────────────────────────


def test_range_sums_to_one():
    """Sum of RANGE probabilities for strikes 10..30 should be > 0.99.

    With mean=20, std=1.5, the ±0.5 buckets at integer strikes cover the
    entire real line from 9.5 to 30.5, leaving only the extreme tails
    outside.
    """
    mean = 20.0
    std = 1.5
    total = 0.0
    for strike in range(10, 31):
        total += estimate_probability(
            mean, std, threshold=float(strike), market_type="RANGE"
        )
    assert total > 0.99, f"Expected sum > 0.99, got {total}"


# ── 5. LOW market edge direction ─────────────────────────────────────────────


def test_low_market_signal_direction():
    """LOW market: mean ~16, strike=17 → P(T <= 17) > 0.5.

    With yes_price=0.30, the edge must be POSITIVE and recommended side
    should be YES.  (The old code wrongfully recommended NO for LOW
    markets because it always computed P(above).)
    """
    from engine.calculator import Calculator

    calc = Calculator()
    estimated_prob = calc.estimate_probability(
        forecasts=[15.0, 16.0, 17.0],  # mean ≈ 16.0
        threshold=17.0,
        days_ahead=0,
        market_type="LOW",
    )
    # P(T <= 17) with mean 16 and std ~1.0 → well above 0.5
    assert estimated_prob > 0.5, f"P(LOW) should be > 0.5, got {estimated_prob}"

    yes_price = 0.30
    edge = estimated_prob - yes_price
    assert edge > 0, (
        f"LOW market edge should be positive (YES undervalued), "
        f"got edge={edge:.4f} (prob={estimated_prob:.4f}, price={yes_price})"
    )

    # Verify the recommended side from analyze_market logic
    market_implied = yes_price
    if edge > 0:
        kelly_frac = calc.kelly_criterion(estimated_prob, market_implied, 0.15)
        recommended_side = "YES"
    else:
        no_prob = 1.0 - estimated_prob
        no_implied = 1.0 - market_implied
        no_edge = no_prob - no_implied
        if no_edge > 0:
            kelly_frac = calc.kelly_criterion(no_prob, no_implied, 0.15)
            recommended_side = "NO"
        else:
            kelly_frac = 0.0
            recommended_side = None

    assert recommended_side == "YES", (
        f"Expected recommended_side='YES' for LOW market, got {recommended_side}"
    )
    assert kelly_frac > 0, "Kelly fraction should be > 0 for positive-edge LOW"


# ── 6. No signal when fair ────────────────────────────────────────────────────


def test_no_signal_when_fair():
    """When model probability ≈ market probability, should_bet must be False."""
    from engine.calculator import Calculator

    calc = Calculator()
    # Model says P(T >= 25) ≈ 0.50; market says yes_price = 0.50
    estimated_prob = calc.estimate_probability(
        forecasts=[24.0, 25.0, 26.0],  # mean ≈ 25
        threshold=25.0,
        days_ahead=0,
        market_type="HIGH",
    )
    # Edge is near zero
    yes_price = 0.50
    edge = estimated_prob - yes_price

    # Use the same min_edge threshold the bot uses
    from config.settings import bot_config

    min_edge = bot_config.strategy.min_edge
    should_bet = abs(edge) >= min_edge

    assert not should_bet, (
        f"Fair market should not trigger bet: "
        f"prob={estimated_prob:.4f}, price={yes_price}, "
        f"edge={edge:.4f}, min_edge={min_edge}"
    )
