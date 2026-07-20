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
# CRITICAL FIX: the swap to a temp DB MUST run in pytest_configure, which
# executes BEFORE pytest collects/imports the test modules. Many test files
# do ``import database.db`` / ``from database.db import get_session`` at
# module top level, so they bind get_session to whatever engine exists at
# import time. The old session-scoped fixture swapped DB_PATH only AFTER
# collection, so those modules had already bound to the REAL engine
# (bot.db); any test calling ``session.query(...).delete()`` then wiped the
# live production database. Swapping here guarantees every top-level import
# lands on the temp engine.
_TMP_DB_PATH = None


def pytest_configure(config):
    global _TMP_DB_PATH
    if _TMP_DB_PATH is None:
        _fd, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
        os.close(_fd)
        import config.settings as cfg_mod

        cfg_mod.config.DB_PATH = _TMP_DB_PATH
        sys.modules.pop("database.db", None)
        sys.modules.pop("database.models", None)
        from database.db import init_db

        init_db()


# This fixture runs before EVERY test and redirects all database operations
# to a temporary file. Production data is safe.


@pytest.fixture(autouse=True, scope="session")
def _protect_production_db_session():
    """Session-scoped: DB already redirected to a temp file by
    pytest_configure (which runs BEFORE collection). This fixture just
    (re)initialises the temp schema and removes the temp file at the end.

    Production data (data/bot.db) is never touched.
    """
    from database.db import init_db

    init_db()

    yield

    if _TMP_DB_PATH:
        try:
            os.unlink(_TMP_DB_PATH)
        except OSError:
            pass


@pytest.fixture(autouse=True, scope="function")
def _protect_production_db_function():
    """Function-scoped safety net: if any engine still points at the real
    bot.db (e.g. a test imported database.db before pytest_configure could
    swap it), redirect it to a fresh temp DB before the test runs. With the
    pytest_configure swap in place this should rarely trigger.
    """
    import config.settings as cfg_mod
    import database.db as db_mod

    if hasattr(db_mod, "engine") and str(db_mod.engine.url).endswith("bot.db"):
        _fd, _p = tempfile.mkstemp(suffix=".db")
        os.close(_fd)
        cfg_mod.config.DB_PATH = _p
        sys.modules.pop("database.db", None)
        sys.modules.pop("database.models", None)
        from database.db import init_db

        init_db()

    yield


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
