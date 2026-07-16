"""Test configuration.

PRODUCTION DB PROTECTION:
Every test runs against a temporary database. Production data (data/bot.db) is
NEVER touched. This prevents destructive tests from wiping live data.

Strategy params are reset to permissive defaults so calculator tests aren't
blocked by production min_entry_price filters.
"""

import os
import sys
import tempfile

import pytest


# ── Auto-backup before every test run ────────────────────────────────────

def _pre_test_backup():
    try:
        db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        db_path = os.path.join(db_dir, "bot.db")
        backup_dir = os.path.join(db_dir, "backups")
        if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
            os.makedirs(backup_dir, exist_ok=True)
            from datetime import datetime
            import shutil
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(db_path, os.path.join(backup_dir, f"bot_pre_test_{ts}.db"))
    except Exception:
        pass

_pre_test_backup()


# ── Production DB Protection ─────────────────────────────────────────────
# This fixture runs before EVERY test and redirects all database operations
# to a temporary file. Production data is safe.


@pytest.fixture(autouse=True, scope="session")
def _protect_production_db_session():
    """Session-scoped: swap DB_PATH to a temp file for the entire test run.

    This is the PRIMARY defense against tests wiping production data.
    Every database engine/session created during tests points here, not at
    data/bot.db.
    """
    import config.settings as cfg_mod

    # Save original
    _orig_db_path = cfg_mod.config.DB_PATH

    # Create temp DB
    _fd, _tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)

    cfg_mod.config.DB_PATH = _tmp_path

    # Force-reimport database.db so it creates engine pointing at temp DB
    if "database.db" in sys.modules:
        del sys.modules["database.db"]
    if "database.models" in sys.modules:
        del sys.modules["database.models"]

    # Initialize the temp DB
    from database.db import init_db
    init_db()

    yield

    # Restore original path
    cfg_mod.config.DB_PATH = _orig_db_path

    # Clean up temp file
    try:
        os.unlink(_tmp_path)
    except OSError:
        pass


@pytest.fixture(autouse=True, scope="function")
def _protect_production_db_function():
    """Function-scoped: ensure DB engine points to temp DB for each test.

    Even if a test somehow resets sys.modules, this fixture re-asserts
    the temp DB path.
    """
    import config.settings as cfg_mod

    _orig = cfg_mod.config.DB_PATH

    # Re-read the temp path from the session fixture
    # The session fixture already set config.DB_PATH to temp
    # Just ensure database.db module uses the current config
    if "database.db" in sys.modules:
        import database.db as db_mod
        # Rebuild engine if it points to production
        if hasattr(db_mod, 'engine') and str(db_mod.engine.url).endswith('bot.db'):
            sys.modules.pop("database.db", None)
            sys.modules.pop("database.models", None)
            from database.db import init_db
            init_db()

    yield

    cfg_mod.config.DB_PATH = _orig


# ── Strategy Params Reset ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_karpathy_strategy_params():
    """Reset Karpathy-search-discovered levers to safe permissive defaults."""
    from config.settings import Config, bot_config

    original_strategy_min_entry = bot_config.strategy.min_entry_price
    original_strategy_ineff = bot_config.strategy.inefficiency_min
    original_strategy_min_edge = bot_config.strategy.min_edge
    original_strategy_kelly = bot_config.strategy.kelly_fraction
    original_config_kelly = Config.KELLY_FRACTION
    original_config_max_bet_pct = Config.MAX_BET_PCT
    original_config_min_entry = Config.MIN_ENTRY_PRICE

    bot_config.strategy.min_entry_price = 0.01
    bot_config.strategy.inefficiency_min = -1.0
    bot_config.strategy.kelly_fraction = 0.15
    bot_config.strategy.min_edge = 0.05
    Config.KELLY_FRACTION = 0.15
    Config.MAX_BET_PCT = 0.03
    Config.MIN_ENTRY_PRICE = 0.01

    yield

    bot_config.strategy.min_entry_price = original_strategy_min_entry
    bot_config.strategy.inefficiency_min = original_strategy_ineff
    bot_config.strategy.min_edge = original_strategy_min_edge
    bot_config.strategy.kelly_fraction = original_strategy_kelly
    Config.KELLY_FRACTION = original_config_kelly
    Config.MAX_BET_PCT = original_config_max_bet_pct
    Config.MIN_ENTRY_PRICE = original_config_min_entry
