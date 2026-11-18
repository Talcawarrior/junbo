"""HEAT kanonik sema testleri (2026-10-27, S15).

Tek sema karari: ``heat.schema`` depoyu kanonik hale getirir. Eski HEAT
depolarinda ``final_prediction`` yerine ``quantile_50``, ``run_at_utc``
yerine ``created_at`` vardi. Bu testler yukseltmenin (ALTER + backfill)
dogru ve idempotent oldugunu dogrular.
"""

import sqlite3

from heat.schema import PREDICTION_LOG_COLUMNS, STATIONS_COLUMNS, ensure_schema

_LEGACY_PREDICTION_LOG = """
CREATE TABLE prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT,
    provider TEXT,
    target_date TEXT,
    predicted_high_c REAL,
    predicted_low_c REAL,
    actual_high_c REAL,
    actual_low_c REAL,
    residual REAL,
    quantile_10 REAL,
    quantile_50 REAL,
    quantile_90 REAL,
    model_id TEXT,
    created_at TEXT
)
"""


def _columns(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        con.close()


def test_creates_tables_from_scratch(tmp_path):
    db = tmp_path / "heat.db"
    sqlite3.connect(db).close()

    out = ensure_schema(str(db))

    assert "error" not in out
    assert "prediction_log" in out["created"]
    assert "stations" in out["created"]
    assert set(PREDICTION_LOG_COLUMNS) <= _columns(db, "prediction_log")
    assert set(STATIONS_COLUMNS) <= _columns(db, "stations")


def test_upgrades_legacy_columns_and_backfills(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute(_LEGACY_PREDICTION_LOG)
    con.execute(
        "INSERT INTO prediction_log (station_id, target_date, quantile_50, created_at) "
        "VALUES ('egll','2026-10-28',17.5,'2026-10-27T02:00:00Z')"
    )
    con.execute(
        "INSERT INTO prediction_log (station_id, target_date, quantile_50, created_at) "
        "VALUES ('egll','2026-10-29',18.5,'2026-10-27T03:00:00Z')"
    )
    con.commit()
    con.close()

    out = ensure_schema(str(db))

    assert "prediction_log.final_prediction" in out["added"]
    assert "prediction_log.run_at_utc" in out["added"]
    assert out["backfilled"] == 4  # 2 satir x (final_prediction + run_at_utc)

    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT final_prediction, run_at_utc FROM prediction_log ORDER BY id").fetchall()
    finally:
        con.close()
    assert rows == [(17.5, "2026-10-27T02:00:00Z"), (18.5, "2026-10-27T03:00:00Z")]


def test_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute(_LEGACY_PREDICTION_LOG)
    con.commit()
    con.close()

    first = ensure_schema(str(db))
    second = ensure_schema(str(db))

    assert first["added"]  # ilk kosuda kolonlar eklendi
    assert second["added"] == []  # ikinci kosuda hicbir sey eklenmedi
    assert second["created"] == []
    assert second["backfilled"] == 0


def test_missing_db_returns_error(tmp_path):
    out = ensure_schema(str(tmp_path / "yok.db"))

    assert out["error"] == "db_missing"
    assert out["created"] == []
