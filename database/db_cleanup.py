"""DB archival & cleanup — hot/cold data management.

Hot (main DB):  Last 10 days — bot operational queries.
Cold (Parquet): 10-120 days — Karpathy fine-tuning dataset.

Purge (beyond 120 days): Deleted permanently.

Usage:
    from database.db_cleanup import archive_old_forecasts, load_archives, load_karpathy_dataset
    archive_old_forecasts(hot_days=10, cold_days=120)
    df = load_karpathy_dataset(since="2026-03-01")  # hot + cold combined
"""

import glob
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from config.settings import config

logger = logging.getLogger("DB_CLEANUP")

ARCHIVE_DIR = os.path.join(os.path.dirname(config.DB_PATH), "archive")
FORECAST_TABLE = "weather_forecasts"
ANALYSIS_TABLE = "analyses"
PERF_TABLE = "model_performance"
CALIB_TABLE = "historical_calibrations"

# Actual column names per table (verified via PRAGMA table_info)
_TABLE_DATE_COL = {
    FORECAST_TABLE: "fetched_at",
    ANALYSIS_TABLE: "analyzed_at",
    PERF_TABLE: "recorded_at",
    CALIB_TABLE: "date",
}

ALL_TABLES = [FORECAST_TABLE, ANALYSIS_TABLE, PERF_TABLE, CALIB_TABLE]


def archive_old_forecasts(hot_days: int = 10, cold_days: int = 120) -> dict:
    """Archive rows older than hot_days to Parquet, purge data older than cold_days.

    1. Export rows older than hot_days -> Parquet files
    2. Delete those rows from SQLite
    3. Delete Parquet files older than cold_days (purge)

    Returns dict with counts per table.
    """
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    hot_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=hot_days)).isoformat()
    cold_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=cold_days)).isoformat()

    conn = sqlite3.connect(config.DB_PATH)
    # M13: Set WAL PRAGMAs on raw connection to match SQLAlchemy mode
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    archived = 0
    result = {}

    # --- Step 1+2: Archive old rows ---
    for table in ALL_TABLES:
        try:
            date_col = _TABLE_DATE_COL[table]
            df = pd.read_sql_query(
                f"SELECT * FROM [{table}] WHERE [{date_col}] < ?",
                conn,
                params=[hot_cutoff],
            )
            if df.empty:
                logger.info("No old rows in %s", table)
                continue

            date_tag = datetime.now().strftime("%Y%m%d")
            pq_path = os.path.join(ARCHIVE_DIR, f"{table}_{date_tag}.parquet")

            if os.path.exists(pq_path):
                existing = pd.read_parquet(pq_path)
                df = pd.concat([existing, df], ignore_index=True)

            # H13: Write Parquet FIRST, then DELETE — prevents data loss on write failure
            df.to_parquet(pq_path, index=False, compression="snappy")

            cur = conn.cursor()
            cur.execute(f"DELETE FROM [{table}] WHERE [{date_col}] < ?", [hot_cutoff])
            deleted = cur.rowcount
            conn.commit()

            archived += len(df)
            result[table] = {"archived": len(df), "deleted": deleted, "file": pq_path}
            logger.info(
                "Archived %d / deleted %d rows from %s",
                len(df),
                deleted,
                table,
            )

        except Exception as e:
            logger.warning("Archive failed for %s: %s", table, e)
            result[table] = {"error": str(e)}

    # --- Step 3: Purge old Parquet files beyond cold_days ---
    purged = 0
    for table in ALL_TABLES:
        pattern = os.path.join(ARCHIVE_DIR, f"{table}_*.parquet")
        for pq_file in glob.glob(pattern):
            try:
                fname = os.path.basename(pq_file)
                # Parse date from filename: {table}_YYYYMMDD.parquet
                date_str = fname.rsplit("_", 1)[-1].replace(".parquet", "")
                file_date = datetime.strptime(date_str, "%Y%m%d")
                if file_date.isoformat() < cold_cutoff:
                    os.remove(pq_file)
                    purged += 1
                    logger.info("Purged old archive: %s", fname)
            except Exception as e:
                logger.warning("Purge failed for %s: %s", pq_file, e)

    # --- Step 4: VACUUM (skip if WAL mode — VACUUM blocks all readers) ---
    if archived > 0:
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if journal_mode == "wal":
                logger.info("WAL mode active — using wal_checkpoint instead of VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            else:
                conn.execute("VACUUM")
            logger.info("Maintenance completed")
        except Exception as e:
            logger.warning("Maintenance failed: %s", e)

    conn.close()

    result["total_archived"] = archived  # type: ignore[assignment]
    result["purged_files"] = purged  # type: ignore[assignment]
    logger.info(
        "Archive done: %d rows archived, %d old files purged",
        archived,
        purged,
    )
    return result


def load_archives(table: str = FORECAST_TABLE, since: str | None = None) -> pd.DataFrame:
    """Load archived Parquet data for a single table."""
    pattern = os.path.join(ARCHIVE_DIR, f"{table}_*.parquet")
    files = sorted(glob.glob(pattern))

    if not files:
        return pd.DataFrame()

    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)

    if since:
        ts = pd.to_datetime(since)
        date_col = _TABLE_DATE_COL.get(table, "fetched_at")
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            df = df[df[date_col] >= ts]

    return df


def load_karpathy_dataset(since: str | None = None, table: str = FORECAST_TABLE) -> pd.DataFrame:
    """Load combined hot (SQLite) + cold (Parquet) data for Karpathy training.

    This gives the FULL dataset: last 120 days (hot 10 + cold 110).

    Args:
        since: ISO date filter (e.g. "2026-03-01")
        table: Table to load (default: weather_forecasts)
    """
    date_col = _TABLE_DATE_COL[table]

    # Load cold (Parquet)
    cold_df = load_archives(table=table, since=since)

    # Load hot (SQLite) — last 10 days
    conn = sqlite3.connect(config.DB_PATH)
    if since:
        hot_df = pd.read_sql_query(
            f"SELECT * FROM [{table}] WHERE [{date_col}] >= ?",
            conn,
            params=[since],
        )
    else:
        hot_df = pd.read_sql_query(f"SELECT * FROM [{table}]", conn)
    conn.close()

    # Combine & deduplicate
    combined = pd.concat([cold_df, hot_df], ignore_index=True)
    combined = combined.drop_duplicates(
        subset=["id"] if "id" in combined.columns else None,
        keep="last",
    )

    logger.info(
        "Karpathy dataset: %d cold + %d hot = %d total rows",
        len(cold_df),
        len(hot_df),
        len(combined),
    )
    return combined


def get_archive_stats() -> dict:
    """Return archive directory stats."""
    if not os.path.isdir(ARCHIVE_DIR):
        return {"files": 0, "total_size_mb": 0}

    files = glob.glob(os.path.join(ARCHIVE_DIR, "*.parquet"))
    total_size = sum(os.path.getsize(f) for f in files)

    by_table: dict[str, dict[str, int | float]] = {}
    for f in files:
        name = os.path.basename(f)
        table = name.rsplit("_", 1)[0]
        size = os.path.getsize(f)
        by_table.setdefault(table, {"count": 0, "size_mb": 0})
        by_table[table]["count"] += 1
        by_table[table]["size_mb"] += size / (1024 * 1024)

    return {
        "files": len(files),
        "total_size_mb": total_size / (1024 * 1024),
        "by_table": by_table,
        "archive_dir": ARCHIVE_DIR,
    }


def auto_cleanup(hot_days: int = 10, cold_days: int = 120) -> dict:
    """Run archival + purge + VACUUM. Called daily by settlement loop."""
    logger.info(
        "Auto-cleanup: hot=%d days, cold=%d days",
        hot_days,
        cold_days,
    )
    result = archive_old_forecasts(hot_days=hot_days, cold_days=cold_days)
    logger.info(
        "Auto-cleanup done: %d archived, %d purged",
        result.get("total_archived", 0),
        result.get("purged_files", 0),
    )
    return result


def archive_bets_and_portfolio():
    """Settlement/reset ÖNCESI bets ve portfolio'yu parquet'a arşivle.

    Silindikten sonra kurtarma için kullanılır.
    Dosyalar: data/archive/bets_snapshot_YYYYMMDD_HHMMSS.parquet
              data/archive/portfolio_snapshot_YYYYMMDD_HHMMSS.parquet
    """
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    conn = sqlite3.connect(config.DB_PATH)
    result = {}

    # Bets tablosunu arşivle
    try:
        df = pd.read_sql_query("SELECT * FROM bets", conn)
        if not df.empty:
            path = os.path.join(ARCHIVE_DIR, f"bets_snapshot_{ts}.parquet")
            df.to_parquet(path, index=False, compression="snappy")
            result["bets"] = {"archived": len(df), "file": path}
            logger.info("Bets archived: %d rows -> %s", len(df), path)
    except Exception as e:
        logger.warning("Bets archive failed: %s", e)
        result["bets"] = {"error": str(e)}

    # Portfolio snapshot
    try:
        df = pd.read_sql_query("SELECT * FROM portfolio", conn)
        if not df.empty:
            path = os.path.join(ARCHIVE_DIR, f"portfolio_snapshot_{ts}.parquet")
            df.to_parquet(path, index=False, compression="snappy")
            result["portfolio"] = {"archived": len(df), "file": path}
            logger.info("Portfolio archived: %s", path)
    except Exception as e:
        logger.warning("Portfolio archive failed: %s", e)
        result["portfolio"] = {"error": str(e)}

    # M15: Cleanup old snapshot files (keep last 30 per prefix)
    for prefix in ("bets_snapshot_", "portfolio_snapshot_"):
        pattern = os.path.join(ARCHIVE_DIR, f"{prefix}*.parquet")
        files = sorted(glob.glob(pattern))
        if len(files) > 30:
            for old_file in files[: len(files) - 30]:
                try:
                    os.remove(old_file)
                    logger.info("Purged old snapshot: %s", os.path.basename(old_file))
                except Exception as e:
                    logger.warning("Snapshot purge failed for %s: %s", old_file, e)

    conn.close()
    return result
