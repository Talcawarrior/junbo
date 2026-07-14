"""Data Backfiller for ASIbot.

Downloads as much historical meteorological forecast and actual temperature data
as possible from Open-Meteo's APIs for mapped cities to build a massive backtest
and calibration dataset.
"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import requests

from config.settings import config
from database.db import DB_PATH

logger = logging.getLogger("ASI_BACKFILLER")


class DataBackfiller:
    """Manages deep backfills of historical predictions vs actual temperature measurements."""

    def __init__(self):
        self.db_path = DB_PATH
        self._init_table()

    def _init_table(self):
        """Initialize historical calibrations and bias tracking table in SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_calibrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_code TEXT,
                city TEXT,
                date TEXT,
                metric TEXT,
                model TEXT,
                predicted_value REAL,
                actual_value REAL,
                bias REAL,
                UNIQUE(city_code, date, metric, model)
            )
        """
        )
        conn.commit()
        conn.close()

    def run_deep_backfill(self, past_days: int = 90, max_cities: int = 10) -> int:
        """Fetch historical GFS, ECMWF, and other forecasts along with actual ground-truth observations.

        Loops back through `past_days` for a subset of major cities, matches
        the predictions vs actuals, and saves them to the DB.
        """
        logger.info(
            "ASI Backfiller: Starting deep backfill for past %d days...", past_days
        )

        # Select a representative set of cities from ICAO map
        all_cities = list(config.CITY_ICAO_MAP.items())[:max_cities]

        now = datetime.now(UTC)
        start_date_dt = now - timedelta(days=past_days + 1)
        end_date_dt = now - timedelta(
            days=2
        )  # Archive is fully complete up to 2 days ago

        start_str = start_date_dt.strftime("%Y-%m-%d")
        end_str = end_date_dt.strftime("%Y-%m-%d")

        records_inserted = 0

        # Define internal to API model mapping
        api_models = (
            "gfs_seamless,ecmwf_ifs04,gem_global,icon_global,"
            "jma_seamless,cma_grapes_global,ukmo_seamless,"
            "meteofrance_seamless"
        )
        # Map API model names to internal names matching Config.MODEL_WEIGHTS.
        # Left = API parameter name, right = internal name used in calibrations table.
        model_names_mapping = {
            "gfs_seamless": "gfs_seamless",
            "ecmwf_ifs04": "ecmwf_ifs04",
            "gem_global": "gem_global",
            "icon_global": "icon_global",
            "jma_seamless": "jma_seamless",
            "cma_grapes_global": "cma_grapes_global",
            "ukmo_seamless": "ukmo_seamless",
            "meteofrance_seamless": "meteofrance_seamless",
        }

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        for city_name, icao_code in all_cities:
            coords = config.ICAO_COORDS.get(icao_code)
            if not coords:
                continue

            lat, lon = coords
            logger.info(
                "ASI Backfiller: Querying %s (%s) coordinates=(%.4f, %.4f)...",
                city_name,
                icao_code,
                lat,
                lon,
            )

            # 1. Fetch historical forecasts from Open-Meteo Historical Forecast API
            forecast_url = "https://historical-forecast-api.open-meteo.com/v1/forecast"
            f_params = {
                "latitude": lat,
                "longitude": lon,
                "start_date": start_str,
                "end_date": end_str,
                "models": api_models,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            }

            # 2. Fetch actual observed temperatures from Open-Meteo Archive API
            archive_url = "https://archive-api.open-meteo.com/v1/archive"
            a_params = {
                "latitude": lat,
                "longitude": lon,
                "start_date": start_str,
                "end_date": end_str,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            }

            try:
                f_resp = requests.get(forecast_url, params=f_params, timeout=15)
                a_resp = requests.get(archive_url, params=a_params, timeout=15)

                if f_resp.status_code != 200 or a_resp.status_code != 200:
                    logger.warning(
                        "ASI Backfiller: Failed API response for city %s. Skipping.",
                        city_name,
                    )
                    continue

                f_data = f_resp.json().get("daily", {})
                a_data = a_resp.json().get("daily", {})

                dates = f_data.get("time", [])
                if not dates:
                    continue

                actual_maxs = a_data.get("temperature_2m_max", [])
                actual_mins = a_data.get("temperature_2m_min", [])

                for idx, dt_str in enumerate(dates):
                    act_max = actual_maxs[idx] if idx < len(actual_maxs) else None
                    act_min = actual_mins[idx] if idx < len(actual_mins) else None

                    # Iterate over models
                    for api_m, internal_m in model_names_mapping.items():
                        # Maximum temperature
                        pred_max_key = f"temperature_2m_max_{api_m}"
                        pred_max = (
                            f_data.get(pred_max_key, [])[idx]
                            if pred_max_key in f_data
                            else None
                        )

                        if pred_max is not None and act_max is not None:
                            bias_max = round(pred_max - act_max, 3)
                            cursor.execute(
                                """
                                INSERT OR REPLACE INTO historical_calibrations
                                (city_code, city, date, metric, model, predicted_value, actual_value, bias)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                                (
                                    icao_code,
                                    city_name.title(),
                                    dt_str,
                                    "temperature_max",
                                    internal_m,
                                    pred_max,
                                    act_max,
                                    bias_max,
                                ),
                            )
                            records_inserted += 1

                        # Minimum temperature
                        pred_min_key = f"temperature_2m_min_{api_m}"
                        pred_min = (
                            f_data.get(pred_min_key, [])[idx]
                            if pred_min_key in f_data
                            else None
                        )

                        if pred_min is not None and act_min is not None:
                            bias_min = round(pred_min - act_min, 3)
                            cursor.execute(
                                """
                                INSERT OR REPLACE INTO historical_calibrations
                                (city_code, city, date, metric, model, predicted_value, actual_value, bias)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                                (
                                    icao_code,
                                    city_name.title(),
                                    dt_str,
                                    "temperature_min",
                                    internal_m,
                                    pred_min,
                                    act_min,
                                    bias_min,
                                ),
                            )
                            records_inserted += 1

                conn.commit()

            except Exception as e:
                logger.error(
                    "ASI Backfiller: Error backfilling city %s: %s", city_name, e
                )
                continue

        conn.close()
        logger.info(
            "ASI Backfiller: Deep backfill completed! Loaded %d calibration data points.",
            records_inserted,
        )
        return records_inserted
