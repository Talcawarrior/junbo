#!/usr/bin/env python3
"""Daily backup for bot.db, orderbook.db, actuals.db, and backtest.db.

Creates timestamped copies in <JUNBO_DATA_DIR>/backups/ and purges old files.

2026-11-18: runs at most ONCE per UTC day (run marker) and keeps only the
newest N copies per prefix (KEEP_LAST_BY_PREFIX). The task fires every 6 hours,
so 3 of 4 runs are no-ops. Before: every run copied ~7 GB and kept 90 days
(~28 GB/day on C:, disk full in ~5 days). Manual extra run: --force.
"""

import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

# Paths: consolidated data root. Override with env for tests.
DATA_DIR = Path(os.getenv("JUNBO_DATA_DIR", get_data_dir()))
ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path(os.getenv("JUNBO_BACKUP_DIR", str(DATA_DIR / "backups")))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DBS = [
    Path(get_db_path()),
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
RETENTION_DAYS = 90  # uzun backtest icin 3 aylik backup (2026-08-11: 30 -> 90)

# 2026-11-18: sayi bazli saklama -- SADECE bu script'in kendi dosyalari
# (<prefix>_YYYYMMDD_HHMMSS.db). db_backup.py dosyalari (bot_startup_*,
# bot_scheduled_*, bot_pre_test_*) kendi limitleriyle yonetilir, dokunulmaz.
# bot=7: db_backup.py zaten 30 gunluk dogrulanmis bot_scheduled yedegi tutuyor;
# ikinci bir 30'luk seri ~50 GB daha ederdi. Canli DB'ler tum gecmisi zaten
# tasidigi icin (append-only) uzun backtest yedege bagli degildir.
KEEP_LAST_BY_PREFIX = {"bot": 7, "orderbook": 7, "backtest": 7, "actuals": 7}
RUN_MARKER = DATA_DIR / ".last_backup_databases_run"

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
    pruned = _prune_by_count()
    if pruned:
        logger.info("Pruned %d backups beyond keep-last limits %s", len(pruned), KEEP_LAST_BY_PREFIX)

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


def _prune_by_count() -> list[Path]:
    """Keep only the newest N files per prefix (this script's own naming only)."""
    deleted: list[Path] = []
    for prefix, keep in KEEP_LAST_BY_PREFIX.items():
        own = re.compile(rf"^{re.escape(prefix)}_\d{{8}}_\d{{6}}\.db$")
        files = sorted(f for f in BACKUP_DIR.glob(f"{prefix}_*.db") if own.match(f.name))
        for old in files[: max(0, len(files) - keep)]:
            # Sidecar dosyalar (-wal/-shm/-journal) da silinir; katalog taramasi
            # yedekleri acip bunlari olusturabiliyor.
            for p in (old, *(old.with_name(old.name + s) for s in ("-wal", "-shm", "-journal"))):
                try:
                    if p.exists():
                        p.unlink()
                        if p == old:
                            deleted.append(old)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", p.name, exc)
    return deleted


def _ran_today(today: str) -> bool:
    try:
        return RUN_MARKER.read_text(encoding="utf-8").strip() == today
    except OSError:
        return False


def main(argv: list[str] | None = None) -> None:
    """Main entry point (once per UTC day unless --force)."""
    import argparse

    parser = argparse.ArgumentParser(description="Daily database backup")
    parser.add_argument("--force", action="store_true", help="run even if already ran today (UTC)")
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not args.force and _ran_today(today):
        logger.info("=== Backup skipped: already ran today (%s UTC); use --force ===", today)
        return
    # Marker KOPYADAN ONCE: yarim kalan/hatali bir kosu her watchdog tick'inde
    # 7 GB'lik yeniden kopya tetiklemesin; sonraki deneme ertesi gun.
    try:
        RUN_MARKER.write_text(today, encoding="utf-8")
    except OSError as exc:
        logger.warning("Run marker write failed: %s", exc)

    logger.info("=== Backup script started ===")
    results = backup_once()

    if results["failed"]:
        logger.error("Some backups failed: %s", results["failed"])
        sys.exit(1)

    logger.info("=== All backups successful ===")


if __name__ == "__main__":
    main()
