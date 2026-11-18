"""Heat MOS job testleri (2026-09-18, yeniden yazim 2026-10-27 S15).

ONCEKI HAL: testler canli ``D:\\HEAT data`` deposuna bagliydi ve
``len(preds) > 40`` bekliyordu. Depo semasi Junbo'nun bekledigi
``run_at_utc``/``final_prediction`` kolonlarini kaybedince bu testler
surekli kirmizi kaldi; ustelik gercek veri gerektirdigi icin nedeni
anlasilamiyordu.

YENI HAL: her test kendi gecici HEAT deposunu kurar ve ``JUNBO_HEAT_DB``
ortam degiskeni ile job'a gosterir. Boylece test sema sozlesmesini
dogrular, dis veriye bagli kalmaz.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from heat.schema import PREDICTION_LOG_DDL, STATIONS_DDL


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _tomorrow():
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


def _make_heat_db(path, rows, legacy=False):
    """Gecici HEAT deposu kur. ``rows``: (station_id, target_date, run, pred)."""
    con = sqlite3.connect(path)
    con.execute(STATIONS_DDL)
    if legacy:
        con.execute(
            "CREATE TABLE prediction_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT, provider TEXT, "
            "target_date TEXT, predicted_high_c REAL, predicted_low_c REAL, "
            "actual_high_c REAL, actual_low_c REAL, residual REAL, "
            "quantile_10 REAL, quantile_50 REAL, quantile_90 REAL, "
            "model_id TEXT, created_at TEXT)"
        )
        insert = "INSERT INTO prediction_log (station_id, target_date, quantile_50, created_at) VALUES (?,?,?,?)"
    else:
        con.execute(PREDICTION_LOG_DDL)
        insert = "INSERT INTO prediction_log (station_id, target_date, final_prediction, run_at_utc) VALUES (?,?,?,?)"
    station_sql = (
        "INSERT INTO stations (id, icao, city, country, latitude, longitude, "
        "elevation_m, timezone, settlement_source, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    con.execute(station_sql, ("s1", "KTST", "Testville", "TR", 41.0, 29.0, 10.0, "UTC", "metar", 1, "2026-01-01"))
    con.execute(station_sql, ("s2", "KTSU", "Otherton", "TR", 40.0, 28.0, 20.0, "UTC", "metar", 1, "2026-01-01"))
    for sid, tgt, run, pred in rows:
        con.execute(insert, (sid, tgt, pred, run))
    con.commit()
    con.close()


@pytest.fixture
def heat_db(tmp_path, monkeypatch):
    path = tmp_path / "heat.db"
    _make_heat_db(
        path,
        [
            ("s1", _tomorrow(), "2026-10-27T00:00:00Z", 20.0),
            ("s1", _tomorrow(), "2026-10-27T06:00:00Z", 21.5),  # daha yeni kosu kazanir
            ("s2", _today(), "2026-10-27T06:00:00Z", 15.0),
        ],
    )
    monkeypatch.setenv("JUNBO_HEAT_DB", str(path))
    return path


def test_fetch_live_predictions_shape(heat_db):
    from jobs.heat_mos_job import fetch_live_predictions

    preds = fetch_live_predictions()

    assert len(preds) == 2  # 2 istasyon-hedef cifti, s1 tekrari tekillesti
    by_icao = {p["icao"]: p for p in preds}
    assert set(by_icao) == {"KTST", "KTSU"}
    assert by_icao["KTST"]["pred"] == 21.5  # en son kosu
    assert by_icao["KTST"]["run"] == "2026-10-27T06:00:00Z"
    assert by_icao["KTST"]["city"] == "Testville"
    assert by_icao["KTST"]["lat"] == 41.0
    assert len(by_icao["KTST"]["target"]) == 10


def test_fetch_reads_legacy_schema(tmp_path, monkeypatch):
    """Sema yukseltilmemis depo da okunabilmeli (quantile_50 / created_at)."""
    path = tmp_path / "legacy.db"
    _make_heat_db(path, [("s1", _tomorrow(), "2026-10-27T00:00:00Z", 19.0)], legacy=True)
    monkeypatch.setenv("JUNBO_HEAT_DB", str(path))

    from jobs.heat_mos_job import fetch_live_predictions

    preds = fetch_live_predictions()

    assert len(preds) == 1
    assert preds[0]["pred"] == 19.0
    assert preds[0]["run"] == "2026-10-27T00:00:00Z"


def test_run_writes_forecasts_idempotent(heat_db):
    from database.db import get_session
    from database.models import WeatherForecast
    from jobs.heat_mos_job import run

    r1 = run()
    assert r1["fetched"] == 2
    assert r1["added"] == 2

    r2 = run()
    assert r2["fetched"] == 2
    assert r2["added"] == 0
    assert r2["skipped"] == 2

    with get_session() as s:
        rows = (
            s.query(WeatherForecast)
            .filter(WeatherForecast.source == "heat_mos")
            .filter(WeatherForecast.city.in_(["KTST", "KTSU"]))
            .all()
        )
        assert len(rows) == 2


def test_run_noops_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNBO_HEAT_DB", str(tmp_path / "yok.db"))

    from jobs.heat_mos_job import run

    out = run()

    assert out == {"fetched": 0, "added": 0, "skipped": 0}
