#!/usr/bin/env python3
"""Daily forecast snapshot archive.

The bot keeps only a short hot window of weather_forecasts and prunes the rest.
This job writes the current forecasts to a dated parquet file so future
backtests always have a complete forecast history. It also appends a manifest
row with row counts and target-date coverage.

Usage:
    python scripts/archive_forecasts.py
"""

import argparse
import csv
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir  # noqa: E402

import pandas as pd

DATA_DIR = Path(os.getenv("JUNBO_DATA_DIR", get_data_dir()))
BOT_DB = Path(os.getenv("JUNBO_BOT_DB", str(DATA_DIR / "bot.db")))
ARCHIVE_DIR = DATA_DIR / "archive"
LOG_DIR = DATA_DIR / "logs"
MANIFEST = ARCHIVE_DIR / "forecast_archive_manifest.csv"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FORECAST-ARCHIVE] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "archive_forecasts.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("archive_forecasts")


def _manifest_row(path: Path, df: pd.DataFrame, now: datetime) -> dict:
    target = pd.to_datetime(df["target_date"], errors="coerce")
    fetched = pd.to_datetime(df["fetched_at"], errors="coerce")
    return {
        "archived_at": now.isoformat(),
        "file": path.name,
        "rows": int(len(df)),
        "target_min": str(target.min()),
        "target_max": str(target.max()),
        "target_days": int(target.dt.strftime("%Y-%m-%d").nunique()),
        "sources": int(df["source"].nunique()) if "source" in df else 0,
        "fetched_min": str(fetched.min()),
        "fetched_max": str(fetched.max()),
    }


def archive_once(retention_days: int = 365) -> dict:
    now = datetime.now(timezone.utc)
    if not BOT_DB.exists():
        logger.error("bot.db not found: %s", BOT_DB)
        return {"rows": 0, "file": "", "error": "bot.db missing"}

    conn = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=30)
    try:
        df = pd.read_sql_query("SELECT * FROM weather_forecasts", conn)
    finally:
        conn.close()

    if df.empty:
        logger.warning("weather_forecasts is empty; nothing archived")
        return {"rows": 0, "file": "", "error": "empty"}

    df["archived_at"] = now.isoformat()
    path = ARCHIVE_DIR / f"forecasts_snapshot_{now.strftime('%Y%m%d')}.parquet"

    # Same-day reruns merge and keep the newest copy of each forecast row.
    if path.exists():
        try:
            old = pd.read_parquet(path)
            df = pd.concat([old, df], ignore_index=True)
            subset = [c for c in ("id", "fetched_at") if c in df.columns]
            if subset:
                df = df.drop_duplicates(subset=subset, keep="last")
        except Exception as exc:
            logger.warning("Could not merge existing %s: %s", path.name, exc)

    df.to_parquet(path, index=False, compression="snappy")

    row = _manifest_row(path, df, now)
    write_header = not MANIFEST.exists()
    with open(MANIFEST, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)

    if retention_days > 0:
        cutoff = now - timedelta(days=retention_days)
        for old in ARCHIVE_DIR.glob("forecasts_snapshot_*.parquet"):
            try:
                stamp = datetime.strptime(old.stem.rsplit("_", 1)[-1], "%Y%m%d").replace(tzinfo=timezone.utc)
                if stamp < cutoff:
                    old.unlink()
            except (ValueError, OSError):
                pass

    logger.info(
        "Archived %d rows -> %s (target %s..%s, %d sources)",
        row["rows"],
        path.name,
        row["target_min"],
        row["target_max"],
        row["sources"],
    )
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--retention-days", type=int, default=365)
    args = ap.parse_args()
    archive_once(retention_days=args.retention_days)


if __name__ == "__main__":
    main()
