"""Test configuration.

Reset the Karpathy-search-discovered strategy params to permissive
defaults before every test, so unit tests that exercise the calculator
with low-price markets (yes_price=0.30, etc.) are not blocked by the
production min_entry_price filter.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_karpathy_strategy_params():
    """Reset all Karpathy-search-discovered levers and the legacy Config
    mirror fields to their safe permissive defaults.

    The production bot loads tuned values from data/strategy_params.json
    at import time (via apply_persisted_strategy_params). For unit tests
    we want the *calculator* logic to be exercised regardless of what
    the search found — otherwise any test that uses a low-price market
    or a small portfolio would silently get blocked by the
    min_entry_price gate or the lowered max_bet_pct cap.
    """
    from config.settings import Config, bot_config

    # Snapshot current values so we can restore them after the test.
    original_strategy_min_entry = bot_config.strategy.min_entry_price
    original_strategy_ineff = bot_config.strategy.inefficiency_min
    original_strategy_min_edge = bot_config.strategy.min_edge
    original_strategy_kelly = bot_config.strategy.kelly_fraction
    original_config_kelly = Config.KELLY_FRACTION
    original_config_max_bet_pct = Config.MAX_BET_PCT
    original_config_min_entry = Config.MIN_ENTRY_PRICE

    # Permissive defaults for tests
    bot_config.strategy.min_entry_price = 0.01
    bot_config.strategy.inefficiency_min = -1.0  # gate disabled
    # Restore the canonical defaults so test assertions that hard-code
    # them (kelly=0.15, max_bet_pct=0.03, etc.) pass.
    bot_config.strategy.kelly_fraction = 0.15
    bot_config.strategy.min_edge = 0.05
    Config.KELLY_FRACTION = 0.15
    Config.MAX_BET_PCT = 0.03
    Config.MIN_ENTRY_PRICE = 0.01

    yield

    # Restore whatever was set before the test ran.
    bot_config.strategy.min_entry_price = original_strategy_min_entry
    bot_config.strategy.inefficiency_min = original_strategy_ineff
    bot_config.strategy.min_edge = original_strategy_min_edge
    bot_config.strategy.kelly_fraction = original_strategy_kelly
    Config.KELLY_FRACTION = original_config_kelly
    Config.MAX_BET_PCT = original_config_max_bet_pct
    Config.MIN_ENTRY_PRICE = original_config_min_entry
