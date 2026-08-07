#!/usr/bin/env python3
"""Periodic backup for bot.db, orderbook.db, actuals.db, and backtest.db.

Creates timestamped copies in data/backups/ and purges files older than retention period.
"""

import logging
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DBS = [
    DATA_DIR / "bot.db",
    DATA_DIR / "orderbook.db",
    DATA_DIR / "actuals.db",
    DATA_DIR / "backtest.db",
]

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BACKUP] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "backup_databases.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("backup_databases")

# ── Constants ─────────────────────────────────────────────────────────────
TIMEOUT = 180  # 3 minutes for file operations
MAX_RETRIES = 5  # retry count on failure
RETRY_DELAY = 60  # seconds between retries
RETENTION_DAYS = 30  # keep backups for 30 days

# Regex: filename icinden YYYYMMDD_HHMMSS zaman damgasini cikar.
# Gercek dosya adlari cesitlidir:
#   bot_20260805_120000.db
#   bot_startup_20260806_184540_253224.db
#   bot_pre_test_20260807_045918.db
#   bot_scheduled_20260803_000137_627160.db
#   bot_manual_20260805_030954_533330.db
#   bot_test_20260802_204632_567647.db
#   orderbook_20260805_120000.db / actuals_... / backtest_...
TIMESTAMP_RE = re.compile(r"(\d{8})[_-](\d{6})")


def _parse_backup_ts(filename: str) -> datetime | None:
    """Dosya adindan UTC zaman damgasini cikar; bulunamazsa None."""
    m = TIMESTAMP_RE.search(filename)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def copy_with_retry(src: Path, dst: Path) -> bool:
    """Copy file with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Use copy2 to preserve metadata
            shutil.copy2(str(src), str(dst))
            return True
        except (OSError, IOError, shutil.Error) as exc:
            logger.warning(
                "Attempt %d/%d failed copying %s -> %s: %s",
                attempt,
                MAX_RETRIES,
                src.name,
                dst.name,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    logger.error("All %d attempts failed for %s", MAX_RETRIES, src.name)
    return False


def get_size_mb(path: Path) -> float:
    """Get file size in MB."""
    return path.stat().st_size / (1024 * 1024)


def backup_once() -> dict[str, Any]:
    """Run one backup cycle. Returns stats dict."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results: dict[str, Any] = {
        "timestamp": timestamp,
        "backed_up": [],
        "failed": [],
        "total_size_mb": 0.0,
    }

    logger.info("=== Backup started: %s ===", timestamp)

    for src in SOURCE_DBS:
        if not src.exists():
            logger.warning("Source not found: %s, skipping", src)
            results["failed"].append({"file": src.name, "reason": "not_found"})
            continue

        size_mb = get_size_mb(src)
        dst = BACKUP_DIR / f"{src.stem}_{timestamp}.db"

        success = copy_with_retry(src, dst)
        if success:
            results["backed_up"].append(
                {
                    "file": src.name,
                    "backup": dst.name,
                    "size_mb": round(size_mb, 2),
                }
            )
            results["total_size_mb"] += size_mb
            logger.info("  %s -> %s (%.2f MB)", src.name, dst.name, size_mb)
        else:
            results["failed"].append({"file": src.name, "reason": "copy_failed"})

    # Purge old backups
    purged = purge_old_backups()
    if purged:
        logger.info("Purged %d old backups (>%d days)", len(purged), RETENTION_DAYS)
        for p in purged:
            logger.debug("  Deleted: %s", p.name)

    logger.info("=== Backup complete: %d files, %.2f MB total ===", len(results["backed_up"]), results["total_size_mb"])
    return results


def purge_old_backups() -> list[Path]:
    """Delete backup files older than RETENTION_DAYS. Returns list of deleted files.

    Tarih, dosya adindaki ilk YYYYMMDD_HHMMSS zaman damgasindan cikarilir
    (bot_startup_, bot_pre_test_, bot_scheduled_, bot_manual_, bot_test_ gibi
    tum adlandirma bicimleri desteklenir). Zaman damgasi yoksa dosya
    dokunulmadan birakilir.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    deleted = []

    for pattern in ["bot_*.db", "orderbook_*.db", "actuals_*.db", "backtest_*.db"]:
        for backup_file in BACKUP_DIR.glob(pattern):
            try:
                file_dt = _parse_backup_ts(backup_file.name)
                if file_dt is None:
                    logger.warning("No timestamp in %s - skipped", backup_file.name)
                    continue
                if file_dt < cutoff:
                    backup_file.unlink()
                    deleted.append(backup_file)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", backup_file.name, exc)

    return deleted


def main() -> None:
    """Main entry point."""
    logger.info("=== Backup script started ===")
    results = backup_once()

    if results["failed"]:
        logger.error("Some backups failed: %s", results["failed"])
        sys.exit(1)

    logger.info("=== All backups successful ===")


if __name__ == "__main__":
    main()
