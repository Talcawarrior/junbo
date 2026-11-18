"""HEAT deposunun kanonik semasi (tek sema).

Kanonik tanim, canli yazici olan ``Heat/windows-runner/db.mjs``
(``CREATE TABLE prediction_log``) ile birebir aynidir - 2026-10-27'de
C: deposundan dogrulandi: 72.663 satir, son kosu ``run_at_utc`` dolu.

``ensure_schema`` once **salt-okunur** bir kontrol yapar: tablolar ve
kolonlar yerindeyse hicbir yazma islemi yapmaz (canli depo 3,34 GB ve
Node runner tarafindan surekli yaziliyor; gereksiz kilit alinmaz).
Eksik varsa read-write acar, ekler, eski semadan backfill eder.
Idempotenttir.
"""

import logging
import os
import sqlite3

from heat.config import heat_db_path

logger = logging.getLogger("HEAT_SCHEMA")

#: Canli sema (Heat/windows-runner/db.mjs ile ayni).
PREDICTION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    target_date TEXT NOT NULL,
    run_at_utc TEXT NOT NULL,
    horizon_days INTEGER,
    raw_prediction REAL,
    final_prediction REAL,
    confidence_lower REAL,
    confidence_upper REAL,
    trained_count INTEGER,
    total_count INTEGER,
    model_version TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
)
"""

PREDICTION_LOG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_prediction_log_lookup ON prediction_log(station_id, target_date, run_at_utc)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_log_key ON prediction_log(station_id, target_date, run_at_utc)",
)

STATIONS_DDL = """
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    icao TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    elevation_m REAL NOT NULL,
    timezone TEXT NOT NULL,
    settlement_source TEXT NOT NULL,
    market_slug TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)
"""

#: prediction_log'da bulunmasi gereken kolonlar (id haric - PK degistirilemez).
PREDICTION_LOG_COLUMNS: dict[str, str] = {
    "station_id": "TEXT",
    "target_date": "TEXT",
    "run_at_utc": "TEXT",
    "horizon_days": "INTEGER",
    "raw_prediction": "REAL",
    "final_prediction": "REAL",
    "confidence_lower": "REAL",
    "confidence_upper": "REAL",
    "trained_count": "INTEGER",
    "total_count": "INTEGER",
    "model_version": "TEXT",
    "created_at": "TEXT",
}

#: stations'ta bulunmasi gereken kolonlar (id haric).
STATIONS_COLUMNS: dict[str, str] = {
    "icao": "TEXT",
    "city": "TEXT",
    "country": "TEXT",
    "latitude": "REAL",
    "longitude": "REAL",
    "elevation_m": "REAL",
    "timezone": "TEXT",
    "settlement_source": "TEXT",
    "market_slug": "TEXT",
    "enabled": "INTEGER",
    "created_at": "TEXT",
}

#: Eski (Python) semadan kanonik semaya veri tasima kurallari.
_HISTORY_PREDICTION = ("final_prediction", "quantile_50")
_HISTORY_RUN_AT = ("run_at_utc", "created_at")


def _columns(cur: sqlite3.Cursor, table: str) -> dict[str, str]:
    """Tablodaki kolonlar -> {ad: tip}. Tablo yoksa bos sozluk."""
    try:
        cur.execute(f"PRAGMA table_info({table})")
    except sqlite3.DatabaseError:
        return {}
    return {str(row[1]): str(row[2] or "") for row in cur.fetchall()}


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _connect(path: str, read_only: bool) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    return sqlite3.connect(path, timeout=30)


def _is_current(cur: sqlite3.Cursor) -> bool:
    if not (_table_exists(cur, "prediction_log") and _table_exists(cur, "stations")):
        return False
    pred = _columns(cur, "prediction_log")
    stations = _columns(cur, "stations")
    return set(PREDICTION_LOG_COLUMNS) <= set(pred) and set(STATIONS_COLUMNS) <= set(stations)


def ensure_schema(db_path: str | None = None) -> dict:
    """HEAT deposunu kanonik semaya getir (idempotent).

    Donen sozluk: ``{"db", "created", "added", "backfilled", "ok"}``.
    DB dosyasi yoksa ``error`` anahtari eklenir ve hicbir sey yapilmaz.
    """
    path = db_path or heat_db_path()
    out: dict = {"db": path, "created": [], "added": [], "backfilled": 0, "ok": False}
    if not os.path.exists(path):
        logger.warning("HEAT DB bulunamadi, sema uygulanmadi: %s", path)
        out["error"] = "db_missing"
        return out

    # Hizli yol: sema zaten guncelse canli depoya hic yazma.
    try:
        con = _connect(path, read_only=True)
    except sqlite3.Error as exc:
        logger.warning("HEAT DB salt-okunur acilamadi (%s): %s", path, exc)
        out["error"] = f"open_failed: {exc}"
        return out
    try:
        if _is_current(con.cursor()):
            out["ok"] = True
            return out
    except sqlite3.DatabaseError as exc:
        logger.warning("HEAT sema kontrolu basarisiz (%s): %s", path, exc)
    finally:
        con.close()

    con = _connect(path, read_only=False)
    try:
        cur = con.cursor()
        for table, ddl in (("prediction_log", PREDICTION_LOG_DDL), ("stations", STATIONS_DDL)):
            if not _table_exists(cur, table):
                cur.execute(ddl)
                out["created"].append(table)

        for table, columns in (
            ("prediction_log", PREDICTION_LOG_COLUMNS),
            ("stations", STATIONS_COLUMNS),
        ):
            existing = _columns(cur, table)
            for name, sql_type in columns.items():
                if name not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    out["added"].append(f"{table}.{name}")

        if _table_exists(cur, "prediction_log"):
            for ddl in PREDICTION_LOG_INDEXES:
                try:
                    cur.execute(ddl)
                except sqlite3.OperationalError as exc:
                    logger.warning("HEAT index olusturulamadi: %s", exc)

        pred_cols = _columns(cur, "prediction_log")
        if all(c in pred_cols for c in _HISTORY_PREDICTION):
            cur.execute(
                "UPDATE prediction_log SET final_prediction = quantile_50 "
                "WHERE final_prediction IS NULL AND quantile_50 IS NOT NULL"
            )
            out["backfilled"] += cur.rowcount or 0
        if all(c in pred_cols for c in _HISTORY_RUN_AT):
            cur.execute(
                "UPDATE prediction_log SET run_at_utc = created_at WHERE run_at_utc IS NULL AND created_at IS NOT NULL"
            )
            out["backfilled"] += cur.rowcount or 0

        con.commit()
    finally:
        con.close()

    out["ok"] = True
    if out["created"] or out["added"] or out["backfilled"]:
        logger.info(
            "HEAT semasi guncellendi: created=%s added=%s backfilled=%s",
            out["created"],
            out["added"],
            out["backfilled"],
        )
    return out


if __name__ == "__main__":  # pragma: no cover - elle calistirilan arac
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(ensure_schema(), indent=2, ensure_ascii=False))
