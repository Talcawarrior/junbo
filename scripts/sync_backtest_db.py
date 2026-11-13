#!/usr/bin/env python3
"""Sync bot's collected data to a separate backtest.db.

Reads weather_markets, market_snapshots, weather_forecasts, and bets
from bot.db (read-only) and copies them into backtest.db.

This ensures:
- Bot's database is never modified by us (read-only access)
- Backtest data is completely separate from bot data
- If bot is reset, backtest data survives
- Incremental sync: only copies new/updated records

Run periodically (every 6 hours) to keep backtest.db in sync.
"""

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

# Paths: consolidated data root. Override with env for tests.
DATA_DIR = Path(os.getenv("JUNBO_DATA_DIR", r"D:\JUNBO data"))
ROOT = Path(__file__).resolve().parent.parent
BOT_DB = Path(os.getenv("JUNBO_BOT_DB", get_db_path()))
BACKTEST_DB = Path(os.getenv("JUNBO_BACKTEST_DB", str(DATA_DIR / "backtest.db")))
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BACKTEST-SYNC] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "sync_backtest.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("sync_backtest")

# ── Constants ─────────────────────────────────────────────────────────────
TIMEOUT = 180  # 3 minutes
MAX_RETRIES = 5
RETRY_DELAY = 60

# Tables to sync from bot.db -> backtest.db.
# Forecast history is archived daily by scripts/archive_forecasts.py; copying
# weather_forecasts by id is unsafe because bot.db was reset and its id space
# is lower than the old backtest archive. Bets are preserved in bot backups.
TABLES_TO_SYNC = [
    ("weather_markets", "id", "id"),
    ("market_snapshots", "id", "id"),
]


def init_backtest_db() -> None:
    """Create backtest.db if not exists (schema will be copied from bot.db)."""
    if BACKTEST_DB.exists():
        return
    logger.info("Creating backtest.db (empty)")


def get_bot_connection() -> sqlite3.Connection:
    """Read-only connection to bot.db."""
    return sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)


def get_backtest_connection() -> sqlite3.Connection:
    """Read-write connection to backtest.db."""
    conn = sqlite3.connect(str(BACKTEST_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_table_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Get column names and types for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [(row[1], row[2]) for row in cursor.fetchall()]  # (name, type)


def ensure_table_exists(
    backtest_conn: sqlite3.Connection,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Create table in backtest.db if it doesn't exist."""
    # Check if table exists
    cursor = backtest_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    if cursor.fetchone():
        return

    # Build CREATE TABLE statement
    col_defs = []
    for col_name, col_type in columns:
        col_defs.append(f'"{col_name}" {col_type}')
    create_sql = f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(col_defs)})'
    backtest_conn.execute(create_sql)
    backtest_conn.commit()
    logger.info("Created table %s in backtest.db", table)


def get_max_pk(
    conn: sqlite3.Connection,
    table: str,
    pk_column: str,
) -> int | None:
    """Get the max primary key value from a table."""
    try:
        cursor = conn.execute(f'SELECT MAX("{pk_column}") FROM "{table}"')
        result = cursor.fetchone()
        return result[0] if result and result[0] is not None else None
    except sqlite3.OperationalError:
        return None


def sync_table(table: str, pk_column: str, order_by: str) -> dict[str, int]:
    """Sync one table from bot.db to backtest.db with a bulk ATTACH copy."""
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    for attempt in range(1, MAX_RETRIES + 1):
        backtest_conn = None
        attached = False
        try:
            bot_conn = get_bot_connection()
            backtest_conn = get_backtest_connection()

            columns = get_table_columns(bot_conn, table)
            if not columns:
                logger.warning("Table %s not found in bot.db, skipping", table)
                bot_conn.close()
                backtest_conn.close()
                return stats

            ensure_table_exists(backtest_conn, table, columns)
            col_names = [c[0] for c in columns]
            col_list = ", ".join(f'"{c}"' for c in col_names)
            max_pk = get_max_pk(backtest_conn, table, pk_column)

            # Bulk copy inside SQLite: much faster than one INSERT per row for
            # market_snapshots (millions of rows).
            backtest_conn.execute("ATTACH DATABASE ? AS srcdb", (str(BOT_DB),))
            attached = True
            where = f'WHERE "{pk_column}" > ?' if max_pk is not None else ""
            sql = f'INSERT INTO main."{table}" ({col_list}) SELECT {col_list} FROM srcdb."{table}" {where}'
            before = backtest_conn.total_changes
            params = (max_pk,) if max_pk is not None else ()
            backtest_conn.execute(sql, params)
            backtest_conn.commit()
            stats["inserted"] = backtest_conn.total_changes - before

            logger.info("  %s: %d new records synced", table, stats["inserted"])
            return stats

        except sqlite3.Error as exc:
            logger.warning(
                "Attempt %d/%d failed for table %s: %s",
                attempt,
                MAX_RETRIES,
                table,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                logger.error("All %d attempts failed for table %s", MAX_RETRIES, table)
                stats["errors"] += 1
                return stats
        finally:
            if backtest_conn is not None:
                if attached:
                    try:
                        backtest_conn.execute("DETACH DATABASE srcdb")
                    except sqlite3.Error:
                        pass
                backtest_conn.close()

    return stats


def sync_once() -> dict[str, Any]:
    """Run one sync cycle. Returns combined stats."""
    start_time = datetime.now(timezone.utc)
    logger.info("=== Backtest sync started: %s ===", start_time.isoformat())

    init_backtest_db()

    total_stats: dict[str, Any] = {
        "start_time": start_time.isoformat(),
        "tables": {},
        "total_inserted": 0,
        "total_errors": 0,
    }

    for table, pk, order_by in TABLES_TO_SYNC:
        logger.info("Syncing %s...", table)
        table_stats = sync_table(table, pk, order_by)
        total_stats["tables"][table] = table_stats
        total_stats["total_inserted"] += table_stats.get("inserted", 0)
        total_stats["total_errors"] += table_stats.get("errors", 0)

    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()
    total_stats["end_time"] = end_time.isoformat()
    total_stats["duration_seconds"] = duration

    logger.info(
        "=== Sync complete: %d records, %d errors, %.1fs ===",
        total_stats["total_inserted"],
        total_stats["total_errors"],
        duration,
    )
    return total_stats


def main() -> None:
    """Main entry point."""
    logger.info("=== Backtest DB sync started ===")
    stats = sync_once()

    if stats["total_errors"] > 0:
        logger.warning("Sync completed with %d errors", stats["total_errors"])


if __name__ == "__main__":
    main()
