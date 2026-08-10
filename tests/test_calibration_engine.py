"""Tests for the calibration engine (utils/calibration.py).

Verifies:
  1. bias map construction from historical_calibrations rows
  2. per-city/model temperature correction (raw - MBE)
  3. graceful fallback when no calibration data exists
"""

from __future__ import annotations

import pytest

from utils.calibration import CalibrationEngine, _MODEL_ALIASES, calibrate_forecast


def _make_bias_map():
    return {
        "RKSS": {
            "city_name": "Seoul",
            "metrics": {
                "temperature_max": {
                    "gfs_seamless": {"mbe": -1.52, "mae": 2.29, "sample_count": 125},
                }
            },
        }
    }


def test_calculate_biases_empty_returns_empty_map(tmp_path):
    # A DB with no historical_calibrations table -> empty map, no crash
    import sqlite3

    dbp = tmp_path / "empty.db"
    conn = sqlite3.connect(str(dbp))
    conn.execute("CREATE TABLE historical_calibrations (id INTEGER PRIMARY KEY)")
    conn.close()
    ce = CalibrationEngine(db_path=str(dbp))
    assert ce.bias_map == {}


def test_model_alias_normalisation():
    assert _MODEL_ALIASES["ecmwf_ifs04"] == "ecmwf_ifs025"
    assert _MODEL_ALIASES["gfs_seamless"] == "gfs_seamless"


def test_get_calibrated_temperature_applies_mbe():
    ce = CalibrationEngine(db_path=":memory:", bias_map=_make_bias_map())
    # Seoul max, gfs_seamless MBE=-1.52 -> raw - (-1.52) = raw + 1.52
    corrected = ce.get_calibrated_temperature("RKSS", "temperature_max", "gfs_seamless", 31.0)
    assert corrected == pytest.approx(32.52, abs=0.01)


def test_get_calibrated_temperature_unknown_city_unchanged():
    ce = CalibrationEngine(db_path=":memory:", bias_map=_make_bias_map())
    assert ce.get_calibrated_temperature("XXXX", "temperature_max", "gfs_seamless", 31.0) == 31.0


def test_get_calibrated_temperature_unknown_model_unchanged():
    ce = CalibrationEngine(db_path=":memory:", bias_map=_make_bias_map())
    assert ce.get_calibrated_temperature("RKSS", "temperature_max", "nonexistent_model", 31.0) == 31.0


def test_calibrate_forecast_falls_back_without_data():
    # No singleton data in a fresh process -> returns raw unchanged
    assert calibrate_forecast("RKSS", "temperature_max", "gfs_seamless", 31.0) == 31.0


def test_metric_cleanup():
    ce = CalibrationEngine(db_path=":memory:", bias_map=_make_bias_map())
    assert ce._clean_metric("temperature_max") == "temperature_max"
    assert ce._clean_metric("temp_max") == "temperature_max"
    assert ce._clean_metric("temperature_min") == "temperature_min"
