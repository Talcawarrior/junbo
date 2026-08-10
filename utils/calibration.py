"""Calibration engine: per-city, per-model Mean Bias Error (MBE) correction.

Backfilled rows in ``historical_calibrations`` (see
``scripts/backfill_calibration.py``) hold each model's predicted vs actual
temperature. This engine aggregates those into a bias map and corrects raw
forecasts before they are turned into probabilities, so a model that
systematically over-predicts a city (e.g. Busan max, MBE=-2.9C) has its
forecast shifted up before the ensemble is computed.

Lazy singleton: if the bias map is empty or the file is missing, ``None``-like
behavior preserves the old (uncalibrated) pipeline. No crash, no network.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from config.settings import bot_config
from database.db import DB_PATH

logger = logging.getLogger("UTILS_CALIBRATION")

# Internal model name used for ensemble sources in junbo (weather_forecasts.source)
_MODEL_ALIASES = {
    "ecmwf_ifs04": "ecmwf_ifs025",
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gfs_seamless": "gfs_seamless",
    "gem_global": "gem_global",
    "icon_global": "icon_global",
    "jma_seamless": "jma_seamless",
    "cma_grapes_global": "cma_grapes_global",
    "ukmo_seamless": "ukmo_seamless",
    "meteofrance_seamless": "meteofrance_seamless",
}


class CalibrationEngine:
    """Computes systematic model bias and applies real-time temperature calibrations."""

    def __init__(self, db_path: str | None = None, bias_map: dict | None = None):
        self.db_path = db_path or DB_PATH
        self.bias_map: dict[str, Any] = bias_map or {}
        self.load_calibration_map()

    # ------------------------------------------------------------------
    # Bias map construction (from historical_calibrations)
    # ------------------------------------------------------------------
    def calculate_biases(self) -> dict:
        """Query historical calibrations and compute per-city-model MBE/MAE.

        Returns the bias map and persists it on ``self.bias_map``. Empty table
        -> empty map (caller falls back to uncalibrated forecasts).
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT city_code, city, metric, model,
                       AVG(bias) as mbe,
                       AVG(ABS(bias)) as mae,
                       COUNT(bias) as cnt
                FROM historical_calibrations
                GROUP BY city_code, metric, model
                """
            ).fetchall()
            conn.close()
        except sqlite3.OperationalError as exc:
            logger.warning("Calibration: historical_calibrations unavailable: %s", exc)
            return {}

        new_map: dict[str, Any] = {}
        for city_code, city, metric, model, mbe, mae, cnt in rows:
            if mbe is None:
                continue
            cm = new_map.setdefault(city_code, {"city_name": city, "metrics": {}})
            mm = cm["metrics"].setdefault(metric, {})
            mm[_MODEL_ALIASES.get(model, model)] = {
                "mbe": round(float(mbe), 3),
                "mae": round(float(mae), 3),
                "sample_count": int(cnt),
            }
        self.bias_map = new_map
        logger.info("Calibration: bias map for %d cities", len(new_map))
        return new_map

    def load_calibration_map(self) -> None:
        """Build the bias map from the DB (no external file dependency)."""
        self.calculate_biases()

    # ------------------------------------------------------------------
    # Calibration application
    # ------------------------------------------------------------------
    def _clean_metric(self, metric: str) -> str:
        m = (metric or "").lower()
        if "max" in m or m.startswith("temp") and "max" in m:
            return "temperature_max"
        return "temperature_min"

    def get_calibrated_temperature(self, city_code: str, metric: str, model: str, raw_temp: float) -> float:
        """Return ``raw_temp - MBE`` when a bias exists for this city/model.

        Falls back to the raw value when no calibration data exists, so the
        uncalibrated pipeline is preserved for unknown cities/models.
        """
        if raw_temp is None:
            return raw_temp
        city = self.bias_map.get(city_code or "")
        if not city:
            return raw_temp
        metrics = city.get("metrics", {})
        clean_metric = self._clean_metric(metric)
        model_map = metrics.get(clean_metric, {})
        entry = model_map.get(model)
        if not entry:
            return raw_temp
        mbe = float(entry.get("mbe", 0.0))
        calibrated = round(raw_temp - mbe, 2)
        logger.debug(
            "Calibration [%s-%s]: raw=%.2fC -> %.2fC (MBE=%.2fC)",
            city_code,
            model,
            raw_temp,
            calibrated,
            mbe,
        )
        return calibrated


# ---------------------------------------------------------------------------
# Lazy singleton (AGENTS.md pattern)
# ---------------------------------------------------------------------------
_CALIBRATION_ENGINE: CalibrationEngine | None = None


def _get_calibration() -> CalibrationEngine | None:
    """Return the shared calibration engine, or None when no data exists."""
    global _CALIBRATION_ENGINE
    if _CALIBRATION_ENGINE is None:
        ce = CalibrationEngine()
        if ce.bias_map:
            _CALIBRATION_ENGINE = ce
    return _CALIBRATION_ENGINE


def calibrate_forecast(city_code: str, metric: str, model: str, raw_temp: float) -> float:
    """Public helper: apply calibration if available, else return raw unchanged."""
    if bot_config is None:
        return raw_temp
    ce = _get_calibration()
    if ce is None:
        return raw_temp
    return ce.get_calibrated_temperature(city_code, metric, model, raw_temp)
