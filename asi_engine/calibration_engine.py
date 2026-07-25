"""Calibration Engine for ASIbot.

Calculates the Mean Bias Error (MBE) and Mean Absolute Error (MAE) for each
model per city and metric, then provides real-time "fine-tuned" temperature
calibrations to eliminate systematic bias.
"""

import json
import logging
import os
import sqlite3

from database.db import DB_PATH

logger = logging.getLogger("ASI_CALIBRATION")

CALIBRATION_JSON_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "data", "asi_calibration.json")
)


class CalibrationEngine:
    """Computes systematic model bias and applies real-time temperature calibrations."""

    def __init__(self):
        self.db_path = DB_PATH
        self.bias_map = {}
        self.load_calibration_map()

    def calculate_biases(self) -> dict:
        """Query historical calibrations and calculate the mean bias for each city-model pair.

        Computes MAE and MBE, saves them to a local JSON config, and returns it.
        """
        logger.info("ASI Calibration: Calculating systematic model biases from backfilled dataset...")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Calculate Mean Bias Error (MBE) and Mean Absolute Error (MAE)
        query = """
            SELECT city_code, city, metric, model,
                   AVG(bias) as mbe,
                   AVG(ABS(bias)) as mae,
                   COUNT(bias) as count
            FROM historical_calibrations
            GROUP BY city_code, metric, model
        """
        try:
            cursor.execute(query)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            logger.warning("ASI Calibration: historical_calibrations table is empty. Backfill is required first.")
            conn.close()
            return {}

        new_bias_map = {}
        for city_code, city, metric, model, mbe, mae, count in rows:
            if city_code not in new_bias_map:
                new_bias_map[city_code] = {"city_name": city, "metrics": {}}

            if metric not in new_bias_map[city_code]["metrics"]:
                new_bias_map[city_code]["metrics"][metric] = {}

            new_bias_map[city_code]["metrics"][metric][model] = {
                "mbe": round(mbe, 3),  # Mean Bias Error (Positive = Overpredicting, Negative = Underpredicting)
                "mae": round(mae, 3),  # Mean Absolute Error
                "sample_count": count,
            }

        conn.close()

        # Save to disk
        try:
            os.makedirs(os.path.dirname(CALIBRATION_JSON_PATH), exist_ok=True)
            with open(CALIBRATION_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(new_bias_map, f, indent=2, sort_keys=True)
            logger.info(
                "ASI Calibration: Successfully persisted calibration models to %s",
                CALIBRATION_JSON_PATH,
            )
        except Exception as e:
            logger.error("ASI Calibration: Could not save calibration map: %s", e)

        self.bias_map = new_bias_map
        return new_bias_map

    def load_calibration_map(self):
        """Load the pre-computed bias correction parameters from disk."""
        if os.path.exists(CALIBRATION_JSON_PATH):
            try:
                with open(CALIBRATION_JSON_PATH, encoding="utf-8") as f:
                    self.bias_map = json.load(f)
                logger.info(
                    "ASI Calibration: Loaded bias parameters for %d cities from disk.",
                    len(self.bias_map),
                )
            except Exception as e:
                logger.warning("ASI Calibration: Could not load calibration JSON: %s", e)

    def _city_metric_avg_mbe(self, city_code: str, metric: str) -> float:
        """Fallback: average MBE for a city/metric across all known models.

        Used when the live forecast source (e.g. ``"openmeteo"``) is not in the
        per-model bias_map.  Returns 0.0 if no calibration data exists.
        """
        clean_metric = (
            "temperature_max"
            if "temperature_max" == metric.lower() or (metric.lower().startswith("temp") and "max" in metric.lower())
            else "temperature_min"
        )
        if city_code not in self.bias_map:
            return 0.0
        metrics_map = self.bias_map[city_code].get("metrics", {})
        if clean_metric not in metrics_map:
            return 0.0
        model_mbes = [v["mbe"] for v in metrics_map[clean_metric].values() if isinstance(v, dict) and "mbe" in v]
        if not model_mbes:
            return 0.0
        return round(sum(model_mbes) / len(model_mbes), 3)

    def get_calibrated_temperature(self, city_code: str, metric: str, model: str, raw_temp: float) -> float:
        """Apply dynamic temperature bias correction (fine-tuning).

        If a model has a systematic bias for this city (e.g. overpredicts by 1.5C),
        we subtract the Mean Bias Error (MBE) to get the true, fine-tuned value.

        Falls back to the city/metric average MBE across all models when the
        specific *model* string is not in the bias map (e.g. ``"openmeteo"``
        blended forecasts).
        """
        # Strip internal suffix if any
        clean_metric = (
            "temperature_max"
            if "temperature_max" == metric.lower() or (metric.lower().startswith("temp") and "max" in metric.lower())
            else "temperature_min"
        )

        if city_code in self.bias_map:
            metrics_map = self.bias_map[city_code].get("metrics", {})
            if clean_metric in metrics_map:
                model_map = metrics_map[clean_metric].get(model, {})
                if isinstance(model_map, dict) and "mbe" in model_map:
                    mbe = model_map["mbe"]

                    # Apply Calibration: true_temp = raw_temp - MBE
                    calibrated = round(raw_temp - mbe, 2)
                    logger.debug(
                        "ASI Calibration [%s - %s]: Corrected %s raw=%.2fC -> calibrated=%.2fC (MBE=%.2fC)",
                        city_code,
                        model,
                        clean_metric,
                        raw_temp,
                        calibrated,
                        mbe,
                    )
                    return calibrated

        # Fallback: city/metric average MBE
        mbe = self._city_metric_avg_mbe(city_code, metric)
        if mbe != 0.0:
            calibrated = round(raw_temp - mbe, 2)
            logger.debug(
                "ASI Calibration [%s - %s]: Fallback MBE=%.2fC raw=%.2fC -> calibrated=%.2fC",
                city_code,
                model or "?",
                mbe,
                raw_temp,
                calibrated,
            )
            return calibrated

        return raw_temp
