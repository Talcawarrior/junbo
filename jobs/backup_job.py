"""Daily database backup job.

Runs at most once per UTC day, independent of bot restarts, so a bot that
runs for weeks without a restart still produces fresh, verified backups.

Invoked from the bot settlement loop (bot_loop.py) and can also be run
standalone (e.g. from Windows Task Scheduler) for defense in depth.
"""

from __future__ import annotations

import os
from datetime import date

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backups")
MARKER_PATH = os.path.join(BACKUP_DIR, ".last_backup_run")


def _today_utc() -> str:
    return date.today().strftime("%Y-%m-%d")


def last_backup_date() -> str | None:
    if not os.path.exists(MARKER_PATH):
        return None
    try:
        with open(MARKER_PATH, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def run_backup_once(label: str = "scheduled") -> str | None:
    """Create a backup if one hasn't been taken today (UTC). Returns path or None."""
    today = _today_utc()
    if last_backup_date() == today:
        return None

    from db_backup import create_backup

    path = create_backup(label)
    if path is not None:
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            with open(MARKER_PATH, "w", encoding="utf-8") as fh:
                fh.write(today)
        except OSError:
            pass
    return path


def run_backup_force(label: str = "manual") -> str | None:
    """Force a backup ignoring the daily marker (used by Task Scheduler / CLI)."""
    from db_backup import create_backup

    return create_backup(label)


if __name__ == "__main__":
    result = run_backup_force("cli")
    print(result or "backup skipped")
