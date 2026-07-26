"""Daily self-evolution job: refresh the Brier datastore + run the 3-tier loop.

This wires the previously-orphaned evolution system into the always-on bot.
The live trading loop continuously fills the SQLite DB; this job converts that
data into the unified Brier datastore and then runs the LLM loop orchestrator
(karpathy_weekly + asi_evolve_daily + sia_hourly), which deploys the best
strategy params / model weights when (and only when) they pass the gates.

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
    """Backfill the Brier datastore from the live DB, then run the 3-tier loop.

    Marks today as done even on failure to avoid restart-storm retries; the
    next attempt is the following day.
    """
    now = now or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    result: dict = {"day": today}
    try:
        from data_pipeline.backfill_from_live_db import backfill

        logger.info("Daily evolution: backfilling Brier datastore from live DB")
        result["backfill"] = backfill()

        from asi_engine.llm_loop_orchestrator import run_full_cycle

        logger.info("Daily evolution: running 3-tier orchestrator (use_llm=False)")
        summary = run_full_cycle(use_llm=False)
        result["deployed"] = summary.get("final_deploy", {}).get("deployed")
        result["deploy_reason"] = summary.get("final_deploy", {}).get("reason")
        logger.info(
            "Daily evolution complete: deployed=%s reason=%s",
            result.get("deployed"),
            result.get("deploy_reason"),
        )
    except Exception as exc:  # noqa: BLE001 - loop must never die
        logger.error("Daily evolution failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
    finally:
        _write_marker(today)

    # ── Option A: Model bias backfill ───────────────────────────────────
    # Populate historical_calibrations (forecast vs Archive actual) then
    # recompute the MBE/MAE bias map.  Runs daily alongside evolution but
    # uses its own marker (independent throttling).
    if should_run_calibration(now):
        calibration_result = _run_calibration_backfill(now)
        result["calibration"] = calibration_result
    else:
        result["calibration"] = "skipped (already ran today)"

    return result


def _run_calibration_backfill(now: datetime | None = None) -> dict:
    """Fetch recent per-model forecasts + Archive actuals, compute bias map.

    Runs a backfill (120 days, up to 50 cities) then regenerates
    ``data/asi_calibration.json`` so ``Calculator.analyze_market`` can apply
    city/model bias correction via ``get_calibrated_temperature``.
    """
    now = now or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    cal_result: dict = {"day": today}
    try:
        # ── Step 1: Backfill historical_per-model forecasts vs actuals ──
        # Even though backfill_from_live_db already fetches Archive actuals
        # for the unified datastore, the per-model historical_forecast API
        # rows (gfs_seamless, ecmwf_ifs04, …) only land here.
        from asi_engine.data_backfiller import DataBackfiller

        logger.info("Daily calibration: running DataBackfiller (past 120 days)")
        inserted = DataBackfiller().run_deep_backfill(past_days=120, max_cities=50)
        cal_result["backfill_rows"] = inserted

        # ── Step 2: Regenerate bias map from historical_calibrations ───
        from asi_engine.calibration_engine import CalibrationEngine

        logger.info("Daily calibration: computing MBE/MAE bias map")
        bias_map = CalibrationEngine().calculate_biases()
        cal_result["calibrated_cities"] = len(bias_map)
        logger.info(
            "Daily calibration complete: backfill=%d rows, bias_map=%d cities",
            inserted,
            len(bias_map),
        )

        # ── Option B: Per-side calibration audit from settled bets ──
        try:
            from database.db import get_session
            from database.models import Analysis, Bet
            from asi_engine.calibration_audit import audit_calibration

            with get_session() as sess:
                settled_bets = sess.query(Bet).filter(Bet.status.in_(("won", "lost", "settled", "closed_early"))).all()
                forecasts: list[dict] = []
                for b in settled_bets:
                    analysis = (
                        sess.query(Analysis)
                        .filter(Analysis.market_id == b.market_id)
                        .order_by(Analysis.analyzed_at.desc())
                        .first()
                    )
                    if analysis is not None and analysis.estimated_probability is not None:
                        prob = float(analysis.estimated_probability)
                        side = (b.side or "YES").upper()
                        if side == "NO":
                            prob = 1.0 - prob
                        outcome = 1.0 if b.status == "won" else 0.0
                        forecasts.append({"side": side, "probability": prob, "outcome": outcome})

                if forecasts:
                    report = audit_calibration(forecasts)
                    cal_result["brier_samples"] = (report.yes.n if report.yes else 0) + (
                        report.no.n if report.no else 0
                    )
                    cal_result["brier_score"] = round(report.combined_brier, 4)
                    cal_result["overconfident"] = report.overconfident
                    cal_result["calibration_notes"] = report.notes
                    cal_result["yes_brier"] = round(report.yes.brier, 4) if report.yes else None
                    cal_result["no_brier"] = round(report.no.brier, 4) if report.no else None
                    logger.info(
                        "Brier calibration: samples=%d score=%s overconfident=%s notes=%s",
                        cal_result["brier_samples"],
                        cal_result["brier_score"],
                        cal_result["overconfident"],
                        cal_result["calibration_notes"],
                    )
        except Exception as brier_exc:
            logger.warning("Brier computation skipped: %s", brier_exc)
            cal_result["brier_error"] = str(brier_exc)

    except Exception as exc:  # noqa: BLE001 - loop must never die
        logger.error("Daily calibration failed: %s", exc, exc_info=True)
        cal_result["error"] = str(exc)
    finally:
        _write_calibration_marker(today)
    return cal_result
