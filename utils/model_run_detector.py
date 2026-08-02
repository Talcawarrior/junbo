"""Forecast Latency Arbitrage — detect NWP model run completion windows.

When GFS or ECMWF publishes a new run, Polymarket prices may not yet
reflect the updated forecast. This module detects those windows so the
bot can scan faster and capture latency arbitrage opportunities.

GFS run schedule:    00, 06, 12, 18 UTC  → data available T+3.5h
ECMWF run schedule:  00, 12 UTC          → data available T+7.0h
ICON run schedule:   00, 12 UTC          → data available T+5.0h
GEM run schedule:    00, 12 UTC          → data available T+5.5h

The "window" starts when data becomes available and lasts for 1 hour
— after that, market prices have typically repriced.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ── Model run schedules (UTC hours) ─────────────────────────────────
# Each entry: (model_name, run_hours_utc, latency_hours, window_hours)
MODEL_RUNS = [
    ("gfs_seamless", [0, 6, 12, 18], 3.5, 1.0),
    ("ecmwf_ifs025", [0, 12], 7.0, 1.0),
    ("ecmwf_ifs04", [0, 12], 7.0, 1.0),
    ("icon_global", [0, 12], 5.0, 1.0),
    ("icon_seamless", [0, 12], 5.0, 1.0),
    ("gem_global", [0, 12], 5.5, 1.0),
    ("gem_seamless", [0, 12], 5.5, 1.0),
    ("jma_seamless", [0, 12], 8.0, 1.0),
    ("jma_msm", [0, 12], 8.0, 1.0),
    ("cma_grapes_global", [0, 12], 6.0, 1.0),
    ("ukmo_seamless", [0, 12], 6.0, 1.0),
    ("meteofrance_seamless", [0, 12], 5.0, 1.0),
]

# Fast scan settings when in a model run window
MODEL_RUN_FAST_INTERVAL = 60  # 60 seconds between scans
MODEL_RUN_FAST_WINDOW = 3600  # 1 hour window


class ModelRunWindow(NamedTuple):
    """A detected model run data availability window."""

    model: str
    window_start: datetime  # UTC
    window_end: datetime  # UTC
    run_hour_utc: int


def _last_completed_run_hour(now_utc: datetime) -> tuple[int, int]:
    """Return (run_hour_utc, hours_ago) for the most recent completed run.

    GFS runs at 00,06,12,18. If now is 15:30 UTC, the last completed
    run was 12:00 (3.5 hours ago).
    """
    all_run_hours = set()
    for _, hours, _, _ in MODEL_RUNS:
        all_run_hours.update(hours)
    sorted_hours = sorted(all_run_hours)

    current_hour = now_utc.hour + now_utc.minute / 60.0

    # Find the most recent run hour that is <= current_hour
    for h in reversed(sorted_hours):
        if current_hour >= h:
            return h, current_hour - h

    # All run hours are in the future today; the last run was 18 UTC yesterday
    return 18, current_hour + (24 - 18)


def get_active_model_windows(now_utc: datetime | None = None) -> list[ModelRunWindow]:
    """Return model run windows that are currently active.

    A window is "active" if:
    1. A model run completed at some hour H
    2. Data is available at H + latency
    3. We're within the window [H + latency, H + latency + window_hours]
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    active = []
    for model_name, run_hours, latency_hours, window_hours in MODEL_RUNS:
        for run_h in run_hours:
            window_start_h = run_h + latency_hours
            window_end_h = window_start_h + window_hours

            # Normalize to 0-24 range
            window_start_hour = window_start_h % 24
            window_end_hour = window_end_h % 24

            current_hour = now_utc.hour + now_utc.minute / 60.0

            # Check if we're in the window
            if window_start_h <= 24:
                # Window doesn't cross midnight
                if window_start_hour <= current_hour <= window_end_hour:
                    window_start = now_utc.replace(
                        hour=int(window_start_h) % 24,
                        minute=int((window_start_h % 1) * 60),
                        second=0,
                        microsecond=0,
                    )
                    # Handle day rollover
                    if window_start_h >= 24:
                        window_start += timedelta(days=1)
                    window_end = window_start + timedelta(hours=window_hours)
                    active.append(
                        ModelRunWindow(
                            model=model_name,
                            window_start=window_start,
                            window_end=window_end,
                            run_hour_utc=run_h,
                        )
                    )
            else:
                # Window crosses midnight — check both sides
                pass  # Simplified: skip cross-midnight windows for now

    return active


def is_in_model_run_window(now_utc: datetime | None = None) -> bool:
    """Returns True if any model run data window is currently active."""
    return len(get_active_model_windows(now_utc)) > 0


def get_model_run_fast_interval(now_utc: datetime | None = None) -> int:
    """Returns the scan interval based on model run windows.

    Returns MODEL_RUN_FAST_INTERVAL (60s) if in a window,
    otherwise None (caller should use default interval).
    """
    if is_in_model_run_window(now_utc):
        return MODEL_RUN_FAST_INTERVAL
    return None


def log_model_run_status(now_utc: datetime | None = None):
    """Log the current model run window status for debugging."""
    windows = get_active_model_windows(now_utc)
    if windows:
        for w in windows:
            logger.info(
                "MODEL RUN WINDOW: %s (run=%02dUTC, window=%s–%s)",
                w.model,
                w.run_hour_utc,
                w.window_start.strftime("%H:%M"),
                w.window_end.strftime("%H:%M"),
            )
    else:
        last_run_h, hours_ago = _last_completed_run_hour(now_utc or datetime.now(timezone.utc))
        logger.debug(
            "No active model run windows. Last run: %02d UTC (%.1fh ago)",
            last_run_h,
            hours_ago,
        )
