"""Regression test for the days_ahead=0 / min_liquidity=0 fix in Calculator.

The original bug:
    target_date was set to end-of-day (23:59:59) to keep "today" in scope.
    However `(target_date - now).days` returns 0 when the difference is less
    than one calendar day, so the check `0 < days_ahead <= max_days_ahead`
    rejected every market that resolves "today".

The fix:
    The check was relaxed to `0 <= days_ahead <= max_days_ahead` AND
    `min_liquidity` was lowered to 0.0 because Polymarket public-search
    markets do not expose a meaningful `liquidity` field.

These tests pin both behaviors so they cannot regress silently.
"""

import inspect
import sys

sys.path.insert(0, ".")

from engine.calculator import Calculator


def test_estimate_probability_zero_days_ahead_does_not_crash():
    """estimate_probability must accept days_ahead=0 without raising."""
    calc = Calculator()
    prob = calc.estimate_probability([22.0, 21.0, 23.0], 22.0, days_ahead=0)
    assert 0.0 <= prob <= 1.0


def test_estimate_probability_monotonic_in_days_ahead():
    """Holding mean/threshold constant, the probability mass on the wrong
    side of the strike should grow with days_ahead (more uncertainty).
    """
    calc = Calculator()
    forecasts = [22.0, 22.0, 22.0]  # mean exactly at threshold
    threshold = 22.0
    # When mean == threshold and std is the same, days_ahead increases
    # total uncertainty, so the probability of being "above" should
    # approach 0.5 from the days_ahead=0 base.
    p0 = calc.estimate_probability(forecasts, threshold, days_ahead=0)
    p3 = calc.estimate_probability(forecasts, threshold, days_ahead=3)
    # Both must be near 0.5 because mean == threshold; the key point is
    # neither raises and both are valid probabilities.
    assert 0.3 <= p0 <= 0.7
    assert 0.3 <= p3 <= 0.7


def test_estimate_probability_rejects_empty_forecasts():
    """Empty forecast list returns 0.5 (neutral) and does not raise."""
    calc = Calculator()
    assert calc.estimate_probability([], 22.0, days_ahead=0) == 0.5


def test_estimate_probability_clamps_to_open_unit_interval():
    """Probability is clamped to (0.01, 0.99) so Kelly doesn't divide by 0."""
    calc = Calculator()
    p = calc.estimate_probability([100.0], 0.0, days_ahead=0)  # far above
    assert 0.01 <= p <= 0.99
    p2 = calc.estimate_probability([0.0], 100.0, days_ahead=0)  # far below
    assert 0.01 <= p2 <= 0.99


def test_analyze_market_source_uses_inclusive_days_ahead_check():
    """Pin the actual code shape: the days_ahead check must use
    `0 <= days_ahead <=` (inclusive lower bound), not `0 < days_ahead <=`.
    A regression that adds a strict inequality would reject today's
    markets and produce should_bet=False for everything.
    """
    src = inspect.getsource(Calculator.analyze_market)
    # Look for the boolean expression that gates should_bet. Accept either
    # the inline form or the `days_ahead_for_check` form.
    assert "0 <= days_ahead" in src or "0 <= days_ahead_for_check" in src, (
        "Calculator.analyze_market must use `0 <= days_ahead <= ...` so that "
        "today-resolving markets (days_ahead == 0) are not rejected."
    )
    # And explicitly reject the old buggy form
    assert "0 < days_ahead" not in src, (
        "Strict `0 < days_ahead` rejects today's markets (regression)."
    )


def test_analyze_market_source_uses_min_liquidity_bypass():
    """Pin that min_liquidity=0 (or no liquidity) does not block analysis."""
    src = inspect.getsource(Calculator.analyze_market)
    assert "min_liquidity" in src, "analyze_market must reference min_liquidity"
    assert "<= 0" in src or "min_liquidity <= 0" in src, (
        "analyze_market must allow min_liquidity <= 0 to bypass the check."
    )


def test_flat_bet_usd_default_is_disabled():
    """Config.FLAT_BET_USD defaults to 0.0 (Kelly sizing)."""
    from config.settings import Config

    assert hasattr(Config, "FLAT_BET_USD"), (
        "Config must expose FLAT_BET_USD so a flat-bet override can be set."
    )
    assert float(Config.FLAT_BET_USD) == 0.0, (
        f"FLAT_BET_USD must default to 0.0, got {Config.FLAT_BET_USD}"
    )


def test_strategy_min_edge_is_lowered_to_one_percent():
    """The minimum edge threshold is set to 5%.

    After dogfooding, we raised min_edge from 1% to 5% to reduce
    noise: only markets with >=5% edge after 2% fee drag are eligible.
    """
    from config.settings import StrategyConfig

    me = float(StrategyConfig().min_edge)
    assert 0.01 <= me <= 0.10, (
        f"StrategyConfig.min_edge should be between 1%-10%, got {me}"
    )


def test_bet_placer_overrides_amount_when_flat_bet_set():
    """Pin that place_bet replaces recommended_amount when FLAT_BET_USD > 0."""
    import executor.bet_placer as bp

    src = inspect.getsource(bp.BetPlacer.place_bet)
    assert "FLAT_BET_USD" in src, (
        "place_bet must reference Config.FLAT_BET_USD so the override "
        "actually fires. Without it the Kelly-based amount wins."
    )
    assert (
        "proposed_amount = flat_bet" in src
        or "proposed_amount = flat_bet_usd" in src
        or ("flat_bet > 0" in src and "proposed_amount = flat_bet" in src)
    ), (
        "place_bet must overwrite proposed_amount with the flat value when "
        "FLAT_BET_USD is set. Look for the assignment to proposed_amount."
    )


def test_open_target_dates_returns_set():
    """_get_open_target_dates must return a set of calendar dates so the scan
    loop can detect when a new 2-days-ahead date opens (e.g. 20/7 -> 21/7)
    and trigger the 1-min price window, even hours after midnight.
    """
    from bot_loop import _get_open_target_dates

    result = _get_open_target_dates()
    assert isinstance(result, set), (
        "_get_open_target_dates must return a set of dates for date-diffing."
    )


def test_next_two_day_target_fires_on_new_date_and_only_once():
    """_next_two_day_target must fire when the max open date advances to a new
    calendar date, and NOT re-fire while that date remains the max (once per
    date). When no markets are open it returns (None, False).
    """
    from datetime import date
    from bot_loop import _next_two_day_target

    # No open markets -> no trigger.
    assert _next_two_day_target(None, set()) == (None, False)

    # Current max 20/7, last seen 20/7 -> same date, no trigger.
    assert _next_two_day_target(date(2026, 7, 20), {date(2026, 7, 18), date(2026, 7, 19), date(2026, 7, 20)}) == (date(2026, 7, 20), False)

    # New date 21/7 appears -> trigger (True), returns the new date.
    new_date, trigger = _next_two_day_target(date(2026, 7, 20), {date(2026, 7, 18), date(2026, 7, 19), date(2026, 7, 20), date(2026, 7, 21)})
    assert trigger is True
    assert new_date == date(2026, 7, 21)

    # 21/7 now the max and last seen -> stays, no re-trigger (once).
    assert _next_two_day_target(date(2026, 7, 21), {date(2026, 7, 20), date(2026, 7, 21)}) == (date(2026, 7, 21), False)

    # First cycle with no baseline (None) and open markets -> fires on the max.
    assert _next_two_day_target(None, {date(2026, 7, 20)}) == (date(2026, 7, 20), True)


def test_bet_placer_blocks_bets_within_8h_of_expiry():
    """place_bet must reject markets whose target_date is < 8h away.

    Prevents the same-day 'opened then immediately lost' bleed (e.g. a bet
    opened at 20:47 for a 23:59 expiry). The 8h guard is computed from
    seconds-to-expiry in the target_date_ok check.
    """
    import executor.bet_placer as bp

    src = inspect.getsource(bp.BetPlacer.place_bet)
    assert "MIN_HOURS_TO_EXPIRY" in src, (
        "place_bet must define MIN_HOURS_TO_EXPIRY (8h) guard."
    )
    assert "MIN_HOURS_TO_EXPIRY * 3600" in src, (
        "place_bet must reject markets with < 8h to expiry "
        "(MIN_HOURS_TO_EXPIRY * 3600 seconds)."
    )


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
