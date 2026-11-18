"""Junbo data watchdog -- backtest veri setini kendi kendine tam tutar.

Task Scheduler'dan her 6 dakikada bir calisir (JunboDataWatchdog, PT6M).
Her veri kaynaginin tazeligini kontrol eder; bayat ise ilgili toplayiciyi
KENDI kendine baslatir (kullanici mudahalesi gerekmez):

  - market_snapshots (bot.db)  : son kayit > 40dk -> snapshot_only.py
  - orderbook_snapshots        : son kayit > 10dk -> collect_orderbook.py
  - actuals (actuals.db)       : son kayit > 7 saat -> collect_actuals.py
  - backtest.db sync           : son sync > 7 saat -> sync_backtest_db.py
  - backups                    : gunluk yedek marker'i > 26 saat -> backup_databases.py

Ayrica her 15 dakikada bir Task Scheduler gorev durumlarini kontrol eder;
bir gorev disabled olduysa yeniden enable eder (kendi kendini duzeltir).

Log: <JUNBO_DATA_DIR>/logs/data_watchdog.log
"""

import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PY = r"C:\Users\fdemir\AppData\Local\Programs\Python\Python312\python.exe"
DATA = Path(os.getenv("JUNBO_DATA_DIR", get_data_dir()))
LOG = DATA / "logs" / "data_watchdog.log"

SNAPSHOT_MAX_AGE = 40 * 60  # saniye
ORDERBOOK_MAX_AGE = 10 * 60  # 2026-08-21: 45dk -> 10dk (paralel collect ~4dk'ya indi)
ACTUALS_MAX_AGE = 7 * 3600
SYNC_MAX_AGE = 7 * 3600
# 2026-11-18: backup_databases.py artik gunde 1 kez (UTC marker) calisir.
BACKUP_MAX_AGE = 26 * 3600

TASKS = {
    "JunboSnapshot": r"snapshot_only.py",
    "Junbo-OrderbookCollect": r"scripts\collect_orderbook.py",
    "Junbo-ActualsCollect": r"scripts\collect_actuals.py",
    "Junbo-SyncBacktest": r"scripts\sync_backtest_db.py",
    "Junbo-ArchiveForecasts": r"scripts\archive_forecasts.py",
    "Junbo-DataCatalog": r"scripts\build_data_catalog.py",
    "Junbo-StrategySleeves": r"scripts\strategy_sleeves_report.py",
    "Junbo-BackupDatabases": r"scripts\backup_databases.py",
    "JunboBotWatchdog": r"scripts\bot_watchdog.py",
}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def db_max_age(db_path: str, table: str, time_col: str) -> float | None:
    """Son kaydin yasi (saniye). Yoksa/bozulursa None."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(f"SELECT MAX({time_col}) FROM {table}").fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        raw = str(row[0])
        try:
            t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            # sqlite tarihi "YYYY-MM-DD HH:MM:SS" -> iso
            t = datetime.fromisoformat(raw.replace(" ", "T"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception as e:  # noqa: BLE001
        log(f"DB CHECK FAIL {db_path}.{table}: {e}")
        return None


def run_script(rel_path: str) -> bool:
    """Script'i ayri process olarak calistir (60dk timeout)."""
    target = REPO / rel_path
    try:
        r = subprocess.run(
            [PY, str(target)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=3600,
        )
        rc = r.returncode
        tail = (r.stdout or "")[-200:] + (r.stderr or "")[-200:]
        log(f"RUN {rel_path} rc={rc} :: {tail.strip()[:250]}")
        return rc == 0
    except Exception as e:  # noqa: BLE001
        log(f"RUN FAIL {rel_path}: {e}")
        return False


TASK_ENSURE_NAMES = (
    "JunboSnapshot",
    "Junbo-BackupDatabases",
    "Junbo-OrderbookCollect",
    "Junbo-ActualsCollect",
    "Junbo-SyncBacktest",
    "Junbo-ArchiveForecasts",
    "Junbo-DataCatalog",
    "Junbo-StrategySleeves",
    "JunboBotWatchdog",
    "JunboForecastRunner",
    "JunboForecastRunnerWatchdog",
    "JunboDataWatchdog",
)


def ensure_task_enabled() -> None:
    """Disabled veri gorevlerini geri ac.

    2026-11-18 fix: eski PowerShell komutu fazladan '}}' ile parse hatasi
    veriyor ve 30 sn timeout'a dusuyordu; JunboSnapshot/Junbo-BackupDatabases
    listede yoktu. Artik gecerli tek PowerShell blogu + tam liste.
    """
    try:
        names = ",".join(f"'{n}'" for n in TASK_ENSURE_NAMES)
        shell = (
            f"$names=@({names}); "
            "Get-ScheduledTask | Where-Object { $_.State -eq 'Disabled' -and "
            "$names -contains $_.TaskName } | ForEach-Object { "
            "Enable-ScheduledTask -TaskName $_.TaskName | Out-Null; "
            "Write-Output ('ENABLED ' + $_.TaskName) }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", shell],
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = (out.stdout or "") + (out.stderr or "")
        for line in lines.splitlines():
            if line.strip():
                log(f"TASK-FIX {line.strip()}")
        if out.returncode != 0:
            log(f"TASK-FIX rc={out.returncode}")
    except Exception as e:  # noqa: BLE001
        log(f"TASK-FIX FAIL: {e}")


def main() -> None:
    now_ts = time.time()
    log("=== DATA WATCHDOG TICK ===")

    # 1) snapshot (bot.db market_snapshots)
    age = db_max_age(str(DATA / "bot.db"), "market_snapshots", "snapshot_time")
    if age is None or age > SNAPSHOT_MAX_AGE:
        log(f"SNAPSHOT stale (age={age}) -> run")
        run_script("snapshot_only.py")
    else:
        log(f"SNAPSHOT ok (age={age:.0f}s)")

    # 2) orderbook
    age = db_max_age(str(DATA / "orderbook.db"), "orderbook_snapshots", "snapshot_time")
    if age is None or age > ORDERBOOK_MAX_AGE:
        log(f"ORDERBOOK stale (age={age}) -> run")
        run_script(r"scripts\collect_orderbook.py")
    else:
        log(f"ORDERBOOK ok (age={age:.0f}s)")

    # 3) actuals (tablo: actual_temperatures, zaman: fetched_at)
    try:
        conn = sqlite3.connect(str(DATA / "actuals.db"))
        tbls = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
    except Exception:
        tbls = []
    if tbls:
        age = db_max_age(str(DATA / "actuals.db"), "actual_temperatures", "fetched_at")
        if age is None or age > ACTUALS_MAX_AGE:
            log(f"ACTUALS stale (age={age}) -> run")
            run_script(r"scripts\collect_actuals.py")
        else:
            log(f"ACTUALS ok (age={age:.0f}s)")

    # 4) backtest sync
    age = db_max_age(str(DATA / "backtest.db"), "market_snapshots", "snapshot_time")
    if age is None or age > SYNC_MAX_AGE:
        log(f"SYNC stale (age={age}) -> run")
        run_script(r"scripts\sync_backtest_db.py")
    else:
        log(f"SYNC ok (age={age:.0f}s)")

    # 5) backups -- backup_databases.py'nin gunluk calisma marker'i.
    # 2026-11-18: eskiden klasordeki en genc dosyanin yasina bakiliyordu (copy2
    # kaynak mtime'ini tasir, katalog -shm dosyalari olusturur -> guvenilmez);
    # gunluk yedege gecince 7 saat esigi her tick'te bos calistirma tetiklerdi.
    marker = DATA / ".last_backup_databases_run"
    age = (now_ts - marker.stat().st_mtime) if marker.exists() else None
    if age is None or age > BACKUP_MAX_AGE:
        log(f"BACKUP stale (age={age}) -> run")
        run_script(r"scripts\backup_databases.py")
    else:
        log(f"BACKUP ok (age={age:.0f}s)")

    # 6) Task durum denetimi -- her 2 gorselde 1
    ensure_task_enabled()

    # 7) DB bakimi (ANALYZE + VACUUM) -- gunde 1 kez, 02:00-04:00 UTC penceresinde.
    # VACUUM canli bot ile 'database is locked' riski tasidigi icin bet/settle
    # sessiz penceresi secildi; marker (guncel tarih) ile gunde tek calisma garanti.
    _db_maintenance_marker = DATA / ".last_db_maintenance"
    _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _maintained_today = False
    try:
        if _db_maintenance_marker.exists():
            _maintained_today = _db_maintenance_marker.read_text(encoding="utf-8").strip() == _today
    except Exception:  # noqa: BLE001
        _maintained_today = False
    _now_hour = datetime.now(timezone.utc).hour
    if not _maintained_today and 2 <= _now_hour < 4:
        ok = run_script(r"scripts\db_maintenance.py")
        if ok:
            try:
                _db_maintenance_marker.write_text(_today, encoding="utf-8")
            except Exception:  # noqa: BLE001
                log("DBMAINT marker write failed")
        else:
            log("DBMAINT run failed -- retry next tick (marker not set)")
    else:
        reason = "ran today" if _maintained_today else f"outside 02-04 UTC window (hour={_now_hour})"
        log(f"DBMAINT ok (skipped -- {reason})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"WATCHDOG CRASH: {e}")
