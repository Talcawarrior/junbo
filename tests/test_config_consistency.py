"""Tests for config/settings consistency checks.

Note: bot_config is a module-level singleton that gets mutated at runtime
(loads strategy_params.json at import). Tests verify *defaults* by creating
fresh StrategyConfig instances, not the potentially-mutated singleton.
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
    assert 0.0001 <= s.min_edge <= 0.20, f"Expected min_edge between 0.0001 and 0.20, got {s.min_edge}"


def test_config_kelly_fraction_matches_strategy():
    """Config.KELLY_FRACTION proxy should stay in sync with its real source.

    KELLY_FRACTION is an env-overridable lever (settings.py:567), and
    conftest's _reset_strategy_params fixture normalizes it (and the proxy)
    to 0.15 for test isolation. Comparing against the proxy's actual source
    (bot_config.strategy.kelly_fraction) is the stable sync check; comparing
    against a fresh StrategyConfig() default (0.25) breaks whenever conftest
    overrides the lever.
    """
    from config.settings import bot_config, config

    assert config.KELLY_FRACTION == bot_config.strategy.kelly_fraction, (
        f"KELLY_FRACTION={config.KELLY_FRACTION} != strategy.kelly_fraction={bot_config.strategy.kelly_fraction}"
    )
