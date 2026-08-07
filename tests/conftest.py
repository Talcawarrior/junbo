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
from datetime import datetime

import pytest


# ── Auto-backup before every test run ────────────────────────────────────


def _pre_test_backup():
    """Production bot.db'yi gunluk en fazla 1 kez yedekle (Marker Throttling).

    Testler zaten temp DB kullandigi icin bu yedek yalnizca "test oncesi
    durum" referansi olarak tutulur; her test kosusunda 157 MB'lik bot.db
    kopyalanmasi disk'i hizla doldurur (eski davranis: gunde yuzlerce dosya).
    Gunluk bir kopya yeterlidir.
    """
    try:
        db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        db_path = os.path.join(db_dir, "bot.db")
        backup_dir = os.path.join(db_dir, "backups")
        marker = os.path.join(db_dir, ".last_pre_test_backup")
        if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
            return

        # Ayni gun daha once alindiysa atla (marker icerigi: YYYYMMDD).
        today = datetime.now().strftime("%Y%m%d")
        if os.path.exists(marker):
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == today:
                    return

        os.makedirs(backup_dir, exist_ok=True)
        import shutil

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(db_path, os.path.join(backup_dir, f"bot_pre_test_{ts}.db"))
        with open(marker, "w", encoding="utf-8") as f:
            f.write(today)
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


# ── Shared market factory ─────────────────────────────────────────────────

_FAKE_MARKET_SEQ = [0]


@pytest.fixture
def market_factory():
    """Create a fresh WeatherMarket row in the temp DB and return its id."""
    from database.db import get_session
    from database.models import WeatherMarket

    def _create(**overrides):
        _FAKE_MARKET_SEQ[0] += 1
        n = _FAKE_MARKET_SEQ[0]
        defaults = dict(
            id=f"test-mkt-{n}",
            question=f"Will London exceed 30C on 2026-08-08? #{n}",
            city="London",
            city_code="EGLL",
            metric="temperature_max",
            threshold=30.0,
            threshold_unit="celsius",
            target_date=datetime(2026, 8, 8, 12, 0, 0),
            latitude=51.5,
            longitude=-0.1,
            market_type="HIGH",
            yes_price=0.6,
            no_price=0.4,
            status="open",
            raw_data="{}",
        )
        defaults.update(overrides)
        with get_session() as session:
            m = WeatherMarket(**defaults)
            session.add(m)
            session.commit()
            mid = m.id
            session.expunge(m)
            return mid

    return _create


# ── Strategy Params Reset ────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_strategy_params():
    """Reset strategy levers to safe permissive defaults."""
    from config.settings import Config, bot_config

    original_strategy_min_edge = bot_config.strategy.min_edge
    original_strategy_kelly = bot_config.strategy.kelly_fraction
    original_strategy_flat_bet = bot_config.strategy.flat_bet_usd
    original_config_kelly = Config.KELLY_FRACTION
    original_config_max_bet_pct = Config.MAX_BET_PCT

    bot_config.strategy.kelly_fraction = 0.15
    bot_config.strategy.min_edge = 0.05
    bot_config.strategy.flat_bet_usd = 0.0
    Config.KELLY_FRACTION = 0.15
    Config.MAX_BET_PCT = 1.0

    yield

    bot_config.strategy.min_edge = original_strategy_min_edge
    bot_config.strategy.kelly_fraction = original_strategy_kelly
    bot_config.strategy.flat_bet_usd = original_strategy_flat_bet
    Config.KELLY_FRACTION = original_config_kelly
    Config.MAX_BET_PCT = original_config_max_bet_pct
