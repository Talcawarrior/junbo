"""Junbo DB bakim scripti -- ANALYZE + VACUUM + WAL checkpoint.

Veri buyudukce ve gunluk cleanup_old_snapshots / stale silme islemi sonrasinda
SQLite dosya boyutu ve tablo istatistikleri eskimeye baslar. Bu script gunde 1
kez (data_watchdog icinden, 02:00-04:00 UTC penceresinde) calisir:

  1. PRAGMA wal_checkpoint(TRUNCATE) -- WAL'i ana DB'ye cevir, -wal dosyayi bosalt
  2. ANALYZE -- istatistikleri guncelle (sorgu plancisina yardim)
  3. VACUUM -- dolmus sayfalari geri topla (dosya boyutunu kucult)

Neden VACUUM ayri script'te ve gunde 1 kez: canli bot + diger loop'lar ayni
anda DB'ye bagliyken VACUUM 'database is locked' riski tasir. Sonuc, boyut
oncesi/sonrasi data/logs/db_maintenance.log dosyasina yazilir.

Elle calistirma:  python scripts/db_maintenance.py
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "data" / "logs" / "db_maintenance.log"

DBS = [
    ("bot.db", REPO / "data" / "bot.db"),
    ("backtest.db", REPO / "data" / "backtest.db"),
]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024) if path.exists() else 0.0


def _maintain_one(name: str, path: Path) -> bool:
    if not path.exists():
        log(f"{name} MISSING ({path}) -- skip")
        return False
    before = _size_mb(path)
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("ANALYZE")
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"{name} FAIL: {e}")
        return False
    after = _size_mb(path)
    log(f"{name}: {before:.2f}MB -> {after:.2f}MB (saved {max(0.0, before - after):.2f}MB)")
    return True


def main() -> int:
    ok = True
    for name, path in DBS:
        if not _maintain_one(name, path):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001
        log(f"DB MAINTENANCE CRASH: {e}")
        raise
