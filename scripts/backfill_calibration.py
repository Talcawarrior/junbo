"""Backfill historical_calibrations from local forecast + actual data.

Junbo already collects per-model weather forecasts (``weather_forecasts``)
and Archive actuals (``actuals.db``). This script joins them and populates
``historical_calibrations`` so the calibration engine can compute per-city,
per-model Mean Bias Error (MBE) and correct forecasts before betting.

No external API calls — pure local DB join. Idempotent: rows are keyed by
(city_code, date, metric, model) and inserted with INSERT OR REPLACE.

Usage:
    python scripts/backfill_calibration.py          # dry-run summary
    python scripts/backfill_calibration.py --apply   # write to DB
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.db import get_session  # noqa: E402
from database.models import HistoricalCalibration, WeatherMarket  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical_calibrations")
    parser.add_argument("--apply", action="store_true", help="write to DB (default: dry-run)")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD, inclusive")
    args = parser.parse_args()

    # city_code (ICAO) -> city display name from weather_markets
    code_to_name = {}
    with get_session() as session:
        rows = (
            session.query(WeatherMarket.city, WeatherMarket.city_code)
            .filter(WeatherMarket.city_code.isnot(None))
            .distinct()
            .all()
        )
        for city_name, code in rows:
            if code and city_name:
                code_to_name.setdefault(code, city_name)

        # load actuals from actuals.db (separate DB file)
        actuals_path = os.path.join(_REPO_ROOT, "data", "actuals.db")
        import sqlite3

        adb = sqlite3.connect(actuals_path)
        actuals = {}  # (city_name, date_str, metric) -> value
        for r in adb.execute("SELECT city, date, temperature_2m_max, temperature_2m_min FROM actual_temperatures"):
            city, date_str, tmax, tmin = r
            date_str = str(date_str)[:10]
            if tmax is not None:
                actuals[(city, date_str, "temperature_max")] = float(tmax)
            if tmin is not None:
                actuals[(city, date_str, "temperature_min")] = float(tmin)
        adb.close()

        # load forecasts: (city_code, date_str, metric, model) -> predicted_value
        from database.models import WeatherForecast

        fc_rows = session.query(
            WeatherForecast.city,
            WeatherForecast.target_date,
            WeatherForecast.metric,
            WeatherForecast.source,
            WeatherForecast.predicted_value,
        ).all()
        forecasts = []
        for code, tdate, metric, source, pval in fc_rows:
            if not code or not metric or not source or pval is None:
                continue
            date_str = str(tdate)[:10] if tdate else None
            if not date_str:
                continue
            if args.start_date and date_str < args.start_date:
                continue
            if args.end_date and date_str > args.end_date:
                continue
            city_name = code_to_name.get(code)
            if not city_name:
                continue
            actual = actuals.get((city_name, date_str, metric))
            if actual is None:
                continue
            forecasts.append((code, city_name, date_str, metric, source, float(pval), actual))

        print(f"matched forecast->actual pairs: {len(forecasts)}")
        # breakdown by metric
        by_metric = {}
        for _c, _n, _d, metric, _m, _pv, _av in forecasts:
            by_metric[metric] = by_metric.get(metric, 0) + 1
        for metric, n in sorted(by_metric.items()):
            print(f"  {metric}: {n}")

        # per model coverage
        by_model = {}
        for _c, _n, _d, _m, model, _pv, _av in forecasts:
            by_model[model] = by_model.get(model, 0) + 1
        print("  models:", ", ".join(f"{m}({by_model[m]})" for m in sorted(by_model)))

        if not args.apply:
            print("\nDRY-RUN: no rows written (use --apply)")
            return 0

        inserted = 0
        for code, city_name, date_str, metric, model, pval, actual in forecasts:
            existing = (
                session.query(HistoricalCalibration)
                .filter(
                    HistoricalCalibration.city_code == code,
                    HistoricalCalibration.date == date_str,
                    HistoricalCalibration.metric == metric,
                    HistoricalCalibration.model == model,
                )
                .first()
            )
            bias = round(pval - actual, 3)
            if existing:
                existing.predicted_value = pval
                existing.actual_value = actual
                existing.bias = bias
            else:
                session.add(
                    HistoricalCalibration(
                        city_code=code,
                        city=city_name,
                        date=datetime.strptime(date_str, "%Y-%m-%d"),
                        metric=metric,
                        model=model,
                        predicted_value=pval,
                        actual_value=actual,
                        bias=bias,
                    )
                )
            inserted += 1
        session.commit()
        print(f"\nAPPLIED: {inserted} rows in historical_calibrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
