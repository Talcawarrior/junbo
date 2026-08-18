"""Backfill historical_calibrations from local forecast + actual data.

Junbo already collects per-model weather forecasts (``weather_forecasts``)
and actual temperatures. This script joins them and populates
``historical_calibrations`` so the calibration engine can compute per-city,
per-model Mean Bias Error (MBE) and correct forecasts before betting.

No external API calls — pure local DB join. Idempotent: rows are keyed by
(city_code, date, metric, model) and inserted with INSERT OR REPLACE.

Actual kaynagi (2026-08-18 kullanici karari: "bias Open-Meteo'dan yanlis,
METAR/WU'dan alalim"):
  --source archive : Open-Meteo Archive (actuals.db). GERCEK COZUM REFERANSI
                     DEGIL: round(archive) == kazanan bucket yalnizca %30.
  --source metar   : METAR/WU istasyon max/min (metar_observations). Polymarket
                     WU (NOAA METAR) verisiyle cozer: round(METAR max) ==
                     kazanan bucket %74 (backtest.py metar_vs_settlement). METAR
                     modunda METAR kapsamayan (city,date,metric) satirlari
                     tablodan temizlenir — karisik kaynakli bias olmaz.

Usage:
    python scripts/backfill_calibration.py                        # dry-run (archive)
    python scripts/backfill_calibration.py --source metar         # dry-run (metar)
    python scripts/backfill_calibration.py --apply                # write (archive)
    python scripts/backfill_calibration.py --source metar --apply # write (metar)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import func  # noqa: E402

from database.db import get_session  # noqa: E402
from database.models import HistoricalCalibration, WeatherMarket  # noqa: E402


def _load_metar_actuals(session) -> dict[tuple[str, str, str], float]:
    """(city_code, day, metric) -> METAR istasyon max/min (bugun haric).

    Polymarket weather marketleri Weather Underground (NOAA/NWS METAR) verisiyle
    cozulur; round(METAR max) == kazanan bucket %74 (backtest.py metar_vs_settlement,
    2026-08-18). Open-Meteo Archive actual yalnizca %30 — bias referansi olarak
    Archive yerine METAR kullanilir (kullanici karari 2026-08-18). Bugunku
    kismi gun (day == bugun) atlanir; yarin tam veriyle yeniden yazilir.
    """
    from database.models import MetarObservation

    today = datetime.now().strftime("%Y-%m-%d")
    metar: dict[tuple[str, str, str], float] = {}
    rows = (
        session.query(
            MetarObservation.city_code,
            MetarObservation.day,
            func.max(MetarObservation.temp_c),
            func.min(MetarObservation.temp_c),
        )
        .filter(MetarObservation.day < today)
        .group_by(MetarObservation.city_code, MetarObservation.day)
        .all()
    )
    for code, day, tmax, tmin in rows:
        if not code:
            continue
        if tmax is not None:
            metar[(code, day, "temperature_max")] = float(tmax)
        if tmin is not None:
            metar[(code, day, "temperature_min")] = float(tmin)
    return metar


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical_calibrations")
    parser.add_argument("--apply", action="store_true", help="write to DB (default: dry-run)")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument(
        "--source",
        default="archive",
        choices=["archive", "metar"],
        help="actual kaynagi: archive (Open-Meteo, %30 cozum uyumu) veya "
        "metar (WU/METAR istasyon, %74 cozum uyumu) — kullanici karari 2026-08-18",
    )
    args = parser.parse_args()

    # city_code (ICAO) -> city display name from weather_markets
    code_to_name: dict[str, str] = {}
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

        # METAR/WU istasyon actual'i — Polymarket cozum referansi (%74 uyum).
        metar_actuals = _load_metar_actuals(session) if args.source == "metar" else {}

        # load forecasts: (city_code, date_str, metric, model) -> predicted_value
        from database.models import WeatherForecast

        fc_rows = (
            session.query(
                WeatherForecast.city,
                WeatherForecast.target_date,
                WeatherForecast.metric,
                WeatherForecast.source,
                WeatherForecast.predicted_value,
            )
            .order_by(WeatherForecast.id)
            .all()
        )  # en guncel tahmin onceki kaydi UPDATE eder
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
            if args.source == "metar":
                actual = metar_actuals.get((code, date_str, metric))
            else:
                actual = actuals.get((city_name, date_str, metric))
            if actual is None:
                continue
            forecasts.append((code, city_name, date_str, metric, source, float(pval), actual))

        print(f"matched forecast->actual pairs: {len(forecasts)}")
        # breakdown by metric
        by_metric: dict[str, int] = {}
        for _c, _n, _d, metric, _m, _pv, _av in forecasts:
            by_metric[metric] = by_metric.get(metric, 0) + 1
        for metric, n in sorted(by_metric.items()):
            print(f"  {metric}: {n}")

        # per model coverage
        by_model: dict[str, int] = {}
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
                    HistoricalCalibration.date == datetime.strptime(date_str, "%Y-%m-%d"),
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

        # METAR modu: METAR kapsamayan (city,date,metric) satirlarini sil +
        # kapsanan anahtarlarda coklayan satirlari teke indir (en guncel tahmin
        # kazanir). Karisik kaynak (Archive + METAR) bias'i MBE'yi bozar; eski
        # insert-or-replace string/date eslesme hatasi duplicate biriktirmisti.
        purged = 0
        if args.apply and args.source == "metar":
            covered = set(metar_actuals.keys())
            seen: set[tuple[str, str, str, str]] = set()
            for row in session.query(HistoricalCalibration).order_by(HistoricalCalibration.id.desc()).all():
                day_str = str(row.date)[:10]
                key = (row.city_code, day_str, row.metric, row.model)
                not_covered = row.city_code is None or (row.city_code, day_str, row.metric) not in covered
                if not_covered or key in seen:
                    session.delete(row)
                    purged += 1
                else:
                    seen.add(key)
            if purged:
                session.commit()
            print(f"  metar purge+dedupe: {purged} satir silindi")
        print(f"\nAPPLIED: {inserted} rows in historical_calibrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
