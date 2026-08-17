"""Daily data job: refresh the Brier datastore from the live DB.

The live trading loop continuously fills the SQLite DB; this job converts that
data into the unified Brier datastore once per UTC day.

Runs at most once per UTC day. A persisted marker file makes this robust to
the bot's frequent restarts (an in-memory flag would re-run on every restart).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger("EVOLUTION_JOB")

# Only run at/after this UTC hour (aligns with the "suggested cron 03:00").
EVOLUTION_UTC_HOUR = 3

_MARKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    ".last_evolution_run",
)

# Separate marker for calibration backfill so it can run independently.
_CALIBRATION_MARKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    ".last_calibration_run",
)


def _read_marker() -> str | None:
    try:
        with open(_MARKER_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_marker(day: str) -> None:
    try:
        os.makedirs(os.path.dirname(_MARKER_PATH), exist_ok=True)
        with open(_MARKER_PATH, "w", encoding="utf-8") as fh:
            fh.write(day)
    except OSError as exc:
        logger.warning("Could not write evolution marker: %s", exc)


def _read_calibration_marker() -> str | None:
    try:
        with open(_CALIBRATION_MARKER_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_calibration_marker(day: str) -> None:
    try:
        os.makedirs(os.path.dirname(_CALIBRATION_MARKER_PATH), exist_ok=True)
        with open(_CALIBRATION_MARKER_PATH, "w", encoding="utf-8") as fh:
            fh.write(day)
    except OSError as exc:
        logger.warning("Could not write calibration marker: %s", exc)


def should_run(now: datetime | None = None) -> bool:
    """True iff we haven't run yet today and it's past EVOLUTION_UTC_HOUR."""
    now = now or datetime.now(UTC)
    if now.hour < EVOLUTION_UTC_HOUR:
        return False
    return _read_marker() != now.strftime("%Y-%m-%d")


def should_run_calibration(now: datetime | None = None) -> bool:
    """True iff calibration hasn't run today (separate marker from evolution)."""
    now = now or datetime.now(UTC)
    # Allow calibration to run any hour (it is less disruptive than evolution).
    return _read_calibration_marker() != now.strftime("%Y-%m-%d")


def run_evolution_cycle(now: datetime | None = None) -> dict:
    """Backfill the Brier datastore from the live DB.

    Marks today as done even on failure to avoid restart-storm retries; the
    next attempt is the following day.
    """
    now = now or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    result: dict = {"day": today}
    try:
        from data_pipeline.backfill_from_live_db import backfill

        logger.info("Daily job: backfilling Brier datastore from live DB")
        result["backfill"] = backfill()
    except Exception as exc:  # noqa: BLE001 - loop must never die
        logger.error("Daily job failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
    finally:
        _write_marker(today)

    # ── Option A: Model bias backfill ───────────────────────────────────
    # Populate historical_calibrations (forecast vs Archive actual).
    # Runs daily alongside evolution but uses its own marker (independent
    # throttling).
    if should_run_calibration(now):
        calibration_result = _run_calibration_backfill(now)
        result["calibration"] = calibration_result
    else:
        result["calibration"] = "skipped (already ran today)"

    return result


def _run_calibration_backfill(now: datetime | None = None) -> dict:
    """Backfill historical_calibrations from local forecast + actual data.

    Uses the same local join as ``scripts/backfill_calibration.py``: per-model
    ``weather_forecasts`` rows matched against actual temperatures, keyed by
    (city_code, date, metric, model) via INSERT OR REPLACE. No external API
    calls. Actual kaynagi ``--source metar`` (2026-08-18 kullanici karari):
    Polymarket WU (NOAA METAR) verisiyle cozer; round(METAR max) == kazanan
    bucket %74 vs Open-Meteo Archive %30 — bias referansi METAR istasyonudur.
    Then refreshes the in-process calibration engine's bias map so forecasts
    are corrected from the next scan onward.
    """
    now = now or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    cal_result: dict = {"day": today}
    try:
        import subprocess
        import sys

        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "backfill_calibration.py",
        )
        logger.info("Daily calibration: backfilling historical_calibrations")
        proc = subprocess.run(
            [sys.executable, script, "--source", "metar", "--apply"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        cal_result["backfill_rows"] = proc.returncode
        if proc.returncode != 0:
            logger.warning(
                "Daily calibration: backfill exit=%d stderr=%s",
                proc.returncode,
                proc.stderr[-500:],
            )

        # Refresh the in-process calibration engine so the next analysis uses
        # the updated bias map (fresh singleton).
        from utils.calibration import CalibrationEngine

        try:
            ce = CalibrationEngine()
            cal_result["calibrated_cities"] = len(ce.bias_map)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Daily calibration: bias map refresh failed: %s", exc)
            cal_result["calibrated_cities"] = 0

        logger.info("Daily calibration complete: %s", cal_result)
    except Exception as exc:  # noqa: BLE001 - loop must never die
        logger.error("Daily calibration failed: %s", exc, exc_info=True)
        cal_result["error"] = str(exc)
    finally:
        _write_calibration_marker(today)
    return cal_result
