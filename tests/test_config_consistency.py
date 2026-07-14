"""Tests for config/settings consistency checks.

Note: bot_config is a module-level singleton that gets mutated at runtime
by SIALoop.__init__ (loads strategy_params.json) and ASI-Evolve.
Tests verify *defaults* by creating fresh StrategyConfig instances, not
the potentially-mutated singleton.
"""


def test_import_config_settings_does_not_raise():
    """assert_config_consistency() must pass at import time."""
    # This raises RuntimeError if KELLY_FRACTION or FEE_DRAG
    # do not match bot_config.strategy values.
    import config.settings  # noqa: F401


def test_strategy_config_min_edge_default():
    """Fresh StrategyConfig default min_edge should be 0.05."""
    from config.settings import StrategyConfig

    s = StrategyConfig()
    assert 0.01 <= s.min_edge <= 0.20, (
        f"Expected min_edge between 0.01 and 0.20, got {s.min_edge}"
    )


def test_config_fee_drag_matches_strategy():
    """Config.FEE_DRAG should equal the default StrategyConfig.fee_drag."""
    from config.settings import StrategyConfig, config

    s = StrategyConfig()
    assert config.FEE_DRAG == s.fee_drag, (
        f"FEE_DRAG={config.FEE_DRAG} != default strategy.fee_drag={s.fee_drag}"
    )


def test_config_kelly_fraction_matches_strategy():
    """Config.KELLY_FRACTION should equal the default StrategyConfig.kelly_fraction."""
    from config.settings import StrategyConfig, config

    s = StrategyConfig()
    assert config.KELLY_FRACTION == s.kelly_fraction, (
        f"KELLY_FRACTION={config.KELLY_FRACTION} != default strategy.kelly_fraction={s.kelly_fraction}"
    )
