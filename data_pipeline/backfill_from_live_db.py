"""Backfill the unified datastore from the live SQLite WeatherMarket DB.

The live bot's SQLite DB already contains fully-parsed, resolved weather
markets (city / target_date / metric / threshold / market_type) plus
hundreds of thousands of per-source forecasts. This module converts that
data into the unified parquet schema so the 3-tier evolution system's
``build_brier_dataset`` has real ground-truth to score against.

Data flow:
    live WeatherMarket (status expired/settled_*)  ->  unified markets
    Open-Meteo Archive actuals (per city/date)     ->  unified actuals

Run as a module::

    python -m data_pipeline.backfill_from_live_db            # full
    python -m data_pipeline.backfill_from_live_db --cities Miami London
"""

from __future__ import annotations

import argparse
import logging

import pandas as pd

from data_pipeline.unified_datastore import UnifiedDatastore
from data_pipeline.weather_ensemble import backfill_archive_many
from database.db import get_session
from database.models import WeatherMarket

logger = logging.getLogger("BACKFILL_LIVE_DB")

# Live-DB status values that represent a market whose outcome is knowable.
RESOLVED_STATUSES = ("expired", "settled_win", "settled_loss")

# Live-DB metric -> unified market_type for HIGH/LOW single-threshold markets.
_METRIC_TO_TYPE = {
    "temperature_max": "HIGH",
    "temperature_min": "LOW",
}


def _markets_from_live_db(cities: list[str] | None) -> pd.DataFrame:
    """Read resolved WeatherMarket rows into a unified-markets DataFrame."""
    with get_session() as s:
        q = s.query(WeatherMarket).filter(
            WeatherMarket.status.in_(RESOLVED_STATUSES),
            WeatherMarket.city.isnot(None),
            WeatherMarket.target_date.isnot(None),
            WeatherMarket.threshold.isnot(None),
        )
        if cities:
            q = q.filter(WeatherMarket.city.in_(cities))
        rows = q.all()
        records = []
        for m in rows:
            mt = m.market_type or _METRIC_TO_TYPE.get(m.metric or "", "")
            records.append(
                {
                    "market_id": str(m.id),
                    "question": m.question,
                    "city": m.city,
                    "latitude": m.latitude,
                    "longitude": m.longitude,
                    "threshold": m.threshold,
                    "threshold_unit": m.threshold_unit,
                    "threshold_low": m.threshold_low,
                    "threshold_high": m.threshold_high,
                    "market_type": mt,
                    "metric": m.metric,
                    "target_date": m.target_date,
                    "end_date": m.target_date,
                    "yes_price": m.yes_price,
                    "no_price": m.no_price,
                    "resolved_outcome": (
                        "Yes" if m.status == "settled_win" else ("No" if m.status == "settled_loss" else None)
                    ),
                    "volume": m.volume,
                    "liquidity": m.liquidity,
                }
            )
    df = pd.DataFrame.from_records(records)
    if not df.empty:
        df["target_date"] = pd.to_datetime(df["target_date"], utc=True, errors="coerce")
        df["end_date"] = pd.to_datetime(df["end_date"], utc=True, errors="coerce")
    return df


def _locations_from_markets(
    markets: pd.DataFrame,
) -> list[tuple[str, float, float]]:
    """One representative (city, lat, lon) per city name (most frequent)."""
    locs: list[tuple[str, float, float]] = []
    for city, grp in markets.groupby("city"):
        row = grp.dropna(subset=["latitude", "longitude"]).groupby(["latitude", "longitude"]).size().idxmax()
        lat, lon = row
        locs.append((str(city), float(lat), float(lon)))
    return locs


def backfill(cities: list[str] | None = None, pad_days: int = 2) -> dict[str, int]:
    """Populate unified markets + actuals from the live DB.

    Args:
        cities: restrict to these city names (None = all resolved markets).
        pad_days: extra days on each side of the market date range when
            fetching actuals (guards against tz/boundary misses).
    """
    ds = UnifiedDatastore()

    logger.info("Reading resolved markets from live SQLite DB...")
    markets = _markets_from_live_db(cities)
    if markets.empty:
        logger.warning("No resolved markets found in live DB (cities=%s)", cities)
        return ds.summary()
    logger.info(
        "Loaded %d resolved markets across %d cities",
        len(markets),
        markets["city"].nunique(),
    )
    ds.write_markets(markets)

    dmin = markets["target_date"].min()
    dmax = markets["target_date"].max()
    # Open-Meteo Archive rejects future dates (400); clamp end to today.
    today = pd.Timestamp.now(tz="UTC").normalize()
    end_ts = min(dmax + pd.Timedelta(days=pad_days), today)
    start = (dmin - pd.Timedelta(days=pad_days)).strftime("%Y-%m-%d")
    end = end_ts.strftime("%Y-%m-%d")
    locations = _locations_from_markets(markets)
    logger.info("Fetching Open-Meteo actuals %s..%s for %d cities", start, end, len(locations))
    actuals = backfill_archive_many(locations, start_date=start, end_date=end)
    if not actuals.empty:
        actuals["date"] = pd.to_datetime(actuals["date"], utc=True, errors="coerce")
        ds.write_actuals(actuals)
    else:
        logger.warning("No actuals returned from Open-Meteo archive")

    summary = ds.summary()
    brier = ds.build_brier_dataset()
    realized = int(brier["realized_yes"].notna().sum()) if not brier.empty and "realized_yes" in brier.columns else 0
    logger.info(
        "Brier dataset: %d joined rows, %d with realized outcome",
        len(brier),
        realized,
    )
    summary["brier_rows"] = len(brier)
    summary["brier_realized"] = realized
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)-18s  %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--cities",
        nargs="*",
        default=None,
        help="Restrict to these city names (default: all resolved markets).",
    )
    args = ap.parse_args()
    summary = backfill(cities=args.cities)
    print("\n=== Unified datastore summary ===")
    for k, v in summary.items():
        print(f"  {k:16} {v}")


if __name__ == "__main__":
    main()
