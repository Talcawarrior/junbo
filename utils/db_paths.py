"""Single source of truth for on-disk data paths.

The live DB path comes from .env (DB_PATH). Test/script overrides take
precedence so callers can isolate their data without touching production:

  JUNBO_BOT_DB    -> explicit bot.db path
  JUNBO_DATA_DIR  -> directory holding bot.db and sibling DBs
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def get_db_path() -> str:
    """Absolute path of the live bot DB."""
    override = os.getenv("JUNBO_BOT_DB")
    if override:
        return str(override)

    data_override = os.getenv("JUNBO_DATA_DIR")
    if data_override:
        return str(Path(data_override) / "bot.db")

    try:
        from config.settings import bot_config

        if bot_config.db_path:
            return str(bot_config.db_path)
    except Exception:  # pragma: no cover - config is importable in this repo
        pass

    env_path = os.getenv("DB_PATH")
    if env_path:
        return env_path
    return str(_REPO_ROOT / "data" / "bot.db")


def get_data_dir() -> str:
    """Directory holding bot.db and its sibling data DBs."""
    override = os.getenv("JUNBO_DATA_DIR")
    if override:
        return str(override)
    return str(Path(get_db_path()).parent)
