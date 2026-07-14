"""Unified datastore: joins all 4 data sources into a backtest-ready schema.

The previous eval_harness.py used a YES=True oracle for backtesting — this
caused the inflated 74.34% ROI claim that GLM-5.2's audit exposed as
synthetic. This module fixes the root cause by joining real ground-truth
data from four sources into a single walk-forward-out-of-sample dataset:

  1. polymarket_ingest      → market metadata + final resolved outcomes
  2. weather_ensemble       → 8-model forecast + archive actuals (ground truth)
  3. poly_data_ingest       → on-chain OrderFilled trades (order flow)
  4. resolvedmarkets_ingest → tick-level orderbook snapshots (depth)

The unified schema is:

  unified_markets    (one row per market — polymarket metadata + outcome)
  unified_forecasts  (one row per (market, model, target_date) — ensemble)
  unified_actuals    (one row per (city, date) — ground truth temperature)
  unified_trades     (one row per OrderFilled event — on-chain)
  unified_snapshots  (one row per (market, timestamp) — orderbook depth)

Walk-forward OOS split: the dataset is split by DATE, not by random shuffle.
For each backtest step N, train on data before date[N], test on date[N..N+K].
This prevents the "test on the same data you trained on" leakage that
plagued the previous eval_harness.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

logger = logging.getLogger("UNIFIED_DATASTORE")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "unified",
)

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------


UNIFIED_MARKETS_SCHEMA = {
    "market_id": "str",
    "question": "str",
    "slug": "str",
    "condition_id": "str",
    "category": "str",
    "city": "str",
    "city_code": "str",
    "latitude": "float64",
    "longitude": "float64",
    "threshold": "float64",
    "threshold_unit": "str",
    "market_type": "str",  # HIGH / LOW / RANGE
    "target_date": "datetime64[ns, UTC]",
    "end_date": "datetime64[ns, UTC]",
    "closed_time": "datetime64[ns, UTC]",
    "yes_price": "float64",  # final resolved yes price (0 or 1)
    "no_price": "float64",  # final resolved no price (0 or 1)
    "resolved_outcome": "str",  # 'Yes' / 'No' / None
    "volume": "float64",
    "liquidity": "float64",
    "clob_token_ids": "object",  # list[str]
}

UNIFIED_FORECASTS_SCHEMA = {
    "city": "str",
    "latitude": "float64",
    "longitude": "float64",
    "target_date": "datetime64[ns, UTC]",
    "model": "str",
    "variable": "str",
    "value": "float64",
    "fetched_at": "datetime64[ns, UTC]",
}

UNIFIED_ACTUALS_SCHEMA = {
    "city": "str",
    "latitude": "float64",
    "longitude": "float64",
    "date": "datetime64[ns, UTC]",
    "temperature_2m_max": "float64",
    "temperature_2m_min": "float64",
    "temperature_2m_mean": "float64",
    "precipitation_sum": "float64",
    "wind_speed_10m_max": "float64",
}

UNIFIED_TRADES_SCHEMA = {
    "block_number": "int64",
    "timestamp": "int64",
    "datetime_utc": "datetime64[ns, UTC]",
    "transaction_hash": "str",
    "log_index": "int64",
    "order_hash": "str",
    "maker": "str",
    "taker": "str",
    "side": "int64",  # 0 = BUY, 1 = SELL
    "token_id": "str",
    "maker_asset_id": "int64",
    "taker_asset_id": "int64",
    "maker_fill_amount": "int64",
    "taker_fill_amount": "int64",
    "fee": "int64",
    "builder": "int64",
    "metadata": "str",
    "maker_usd": "float64",
    "taker_usd": "float64",
    "implied_price": "float64",
    "market_id": "str",  # joined from clobTokenIds lookup
}

UNIFIED_SNAPSHOTS_SCHEMA = {
    "market_id": "str",
    "timestamp": "datetime64[ns, UTC]",
    "interval": "str",
    "mid_price": "float64",
    "spread": "float64",
    "best_bid": "float64",
    "best_ask": "float64",
    "bid_depth": "float64",
    "ask_depth": "float64",
    "bids": "object",  # list[dict]
    "asks": "object",  # list[dict]
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class UnifiedDatastoreConfig:
    """Where to read/write unified data."""

    data_dir: str = DATA_DIR
    # Default lookback window for backtests
    default_lookback_days: int = 90
    # Walk-forward step size in days
    walk_forward_step_days: int = 7
    # Walk-forward test window size in days
    walk_forward_test_days: int = 7
    # Minimum number of markets in a test window to be valid
    min_markets_per_window: int = 5


# ---------------------------------------------------------------------------
# Datastore
# ---------------------------------------------------------------------------


class UnifiedDatastore:
    """Manages the unified schema + walk-forward splits on disk.

    Storage layout:
      {data_dir}/
        markets.parquet       - unified_markets
        forecasts.parquet     - unified_forecasts
        actuals.parquet       - unified_actuals
        trades.parquet        - unified_trades
        snapshots.parquet     - unified_snapshots
        splits/
          walk_forward_{n}.parquet  - per-step split index
    """

    def __init__(self, cfg: UnifiedDatastoreConfig | None = None):
        self.cfg = cfg or UnifiedDatastoreConfig()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        os.makedirs(os.path.join(self.cfg.data_dir, "splits"), exist_ok=True)

    # -- Path helpers ----------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.cfg.data_dir, f"{name}.parquet")

    def _split_path(self, n: int) -> str:
        return os.path.join(self.cfg.data_dir, "splits", f"walk_forward_{n}.parquet")

    # -- Write API -------------------------------------------------------

    def write_markets(self, df: pd.DataFrame) -> None:
        self._write_validated("markets", df, UNIFIED_MARKETS_SCHEMA)

    def write_forecasts(self, df: pd.DataFrame) -> None:
        self._write_validated("forecasts", df, UNIFIED_FORECASTS_SCHEMA)

    def write_actuals(self, df: pd.DataFrame) -> None:
        self._write_validated("actuals", df, UNIFIED_ACTUALS_SCHEMA)

    def write_trades(self, df: pd.DataFrame) -> None:
        self._write_validated("trades", df, UNIFIED_TRADES_SCHEMA)

    def write_snapshots(self, df: pd.DataFrame) -> None:
        self._write_validated("snapshots", df, UNIFIED_SNAPSHOTS_SCHEMA)

    def _write_validated(
        self, name: str, df: pd.DataFrame, schema: dict[str, str]
    ) -> None:
        if df.empty:
            logger.warning("UnifiedDatastore: empty %s DataFrame, skipping write", name)
            return
        # Work on a copy to avoid SettingWithCopyWarning from upstream callers.
        df = df.copy()
        # Coerce columns that exist to the right dtype; ignore missing cols
        for col, dtype in schema.items():
            if col in df.columns:
                try:
                    if dtype.startswith("datetime64"):
                        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
                    else:
                        df[col] = df[col].astype(dtype, errors="ignore")
                except Exception as exc:
                    logger.debug(
                        "Could not coerce %s.%s to %s: %s", name, col, dtype, exc
                    )
        path = self._path(name)
        df.to_parquet(path, index=False)
        logger.info("Wrote %d rows to %s", len(df), path)

    # -- Read API --------------------------------------------------------

    def read_markets(self) -> pd.DataFrame:
        return self._read("markets")

    def read_forecasts(self) -> pd.DataFrame:
        return self._read("forecasts")

    def read_actuals(self) -> pd.DataFrame:
        return self._read("actuals")

    def read_trades(self) -> pd.DataFrame:
        return self._read("trades")

    def read_snapshots(self) -> pd.DataFrame:
        return self._read("snapshots")

    def _read(self, name: str) -> pd.DataFrame:
        path = self._path(name)
        if not os.path.exists(path):
            logger.debug("UnifiedDatastore: %s.parquet not found", name)
            return pd.DataFrame()
        try:
            return pd.read_parquet(path)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return pd.DataFrame()

    # -- Walk-forward splits ---------------------------------------------

    def build_walk_forward_splits(
        self,
        *,
        lookback_days: int | None = None,
        step_days: int | None = None,
        test_days: int | None = None,
        date_column: str = "target_date",
        table_name: str = "markets",
    ) -> list[dict[str, Any]]:
        """Build walk-forward OOS splits over a date-indexed table.

        For each step N:
          - test window:   [T_N, T_N + test_days)
          - train window:  [T_N - lookback_days - test_days, T_N)

        Returns list of split metadata dicts and writes each split's row
        indices to splits/walk_forward_{n}.parquet for reproducibility.

        Args:
            lookback_days: train window length (default: cfg.default_lookback_days)
            step_days: how far to advance T_N each step (default: cfg.walk_forward_step_days)
            test_days: test window length (default: cfg.walk_forward_test_days)
            date_column: which column to split on (default 'target_date')
            table_name: which unified table to split (default 'markets')
        """
        lookback = lookback_days or self.cfg.default_lookback_days
        step = step_days or self.cfg.walk_forward_step_days
        test = test_days or self.cfg.walk_forward_test_days

        df = self._read(table_name)
        if df.empty or date_column not in df.columns:
            logger.warning(
                "Cannot build splits: table '%s' empty or missing column '%s'",
                table_name,
                date_column,
            )
            return []

        df = df.copy()
        df[date_column] = pd.to_datetime(df[date_column], utc=True, errors="coerce")
        df = (
            df.dropna(subset=[date_column])
            .sort_values(date_column)
            .reset_index(drop=True)
        )

        if df.empty:
            return []

        start = df[date_column].min()
        end = df[date_column].max()
        total_days = (end - start).days
        if total_days < lookback + test:
            logger.warning(
                "Not enough history for walk-forward: have %d days, need %d",
                total_days,
                lookback + test,
            )
            return []

        splits: list[dict[str, Any]] = []
        n = 0
        cur_t = start + pd.Timedelta(days=lookback)
        while cur_t + pd.Timedelta(days=test) <= end:
            n += 1
            test_start = cur_t
            test_end = cur_t + pd.Timedelta(days=test)
            train_start = cur_t - pd.Timedelta(days=lookback)
            train_end = cur_t  # exclusive — train does NOT include test window

            train_df = df[
                (df[date_column] >= train_start) & (df[date_column] < train_end)
            ]
            test_df = df[(df[date_column] >= test_start) & (df[date_column] < test_end)]

            if len(test_df) < self.cfg.min_markets_per_window:
                cur_t += pd.Timedelta(days=step)
                continue

            split_meta = {
                "split_n": n,
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
                "train_rows": len(train_df),
                "test_rows": len(test_df),
                "train_indices": train_df.index.tolist(),
                "test_indices": test_df.index.tolist(),
            }

            # Persist split indices for reproducibility
            split_df = pd.DataFrame(
                {
                    "row_index": split_meta["test_indices"],
                    "split_n": n,
                    "test_start": test_start,
                    "test_end": test_end,
                }
            )
            split_df.to_parquet(self._split_path(n), index=False)

            splits.append(split_meta)
            logger.info(
                "Split %d: train %s..%s (%d rows) → test %s..%s (%d rows)",
                n,
                train_start.date(),
                train_end.date(),
                len(train_df),
                test_start.date(),
                test_end.date(),
                len(test_df),
            )
            cur_t += pd.Timedelta(days=step)

        logger.info("Built %d walk-forward splits", len(splits))
        return splits

    def get_split(self, n: int) -> dict[str, Any] | None:
        """Load a previously-persisted split by number."""
        path = self._split_path(n)
        if not os.path.exists(path):
            return None
        split_df = pd.read_parquet(path)
        test_indices = split_df["row_index"].tolist()
        test_start = split_df["test_start"].iloc[0]
        test_end = split_df["test_end"].iloc[0]
        return {
            "split_n": n,
            "test_start": test_start,
            "test_end": test_end,
            "test_indices": test_indices,
        }

    # -- Convenience: join markets + actuals for Brier scoring -----------

    def build_brier_dataset(self) -> pd.DataFrame:
        """Join markets with actuals for Brier-score computation.

        Each row = (market, target_date, predicted_prob, actual_outcome).
        Requires markets + actuals tables to be populated.
        """
        markets = self.read_markets()
        actuals = self.read_actuals()
        if markets.empty or actuals.empty:
            logger.warning(
                "Brier dataset requires both markets and actuals to be populated"
            )
            return pd.DataFrame()

        # Join on (city, target_date)
        markets["join_date"] = markets["target_date"].dt.date.astype(str)
        actuals["join_date"] = actuals["date"].dt.date.astype(str)

        merged = markets.merge(
            actuals,
            on=["city", "join_date"],
            how="inner",
            suffixes=("_m", "_a"),
        )
        if merged.empty:
            return merged

        # Derive realized outcome: did actual temp satisfy the market condition?
        # market_type HIGH: YES wins iff actual_max >= threshold
        # market_type LOW:  YES wins iff actual_max <= threshold
        def _realized(row):
            mt = str(row.get("market_type", "")).upper()
            thresh = row.get("threshold")
            actual = row.get("temperature_2m_max")
            if thresh is None or actual is None or pd.isna(thresh) or pd.isna(actual):
                return None
            if mt == "HIGH":
                return 1.0 if actual >= thresh else 0.0
            if mt == "LOW":
                return 1.0 if actual <= thresh else 0.0
            return None

        merged["realized_yes"] = merged.apply(_realized, axis=1)
        return merged

    # -- Stats -----------------------------------------------------------

    def summary(self) -> dict[str, int]:
        """Row counts for each unified table."""
        return {
            "markets": len(self.read_markets()),
            "forecasts": len(self.read_forecasts()),
            "actuals": len(self.read_actuals()),
            "trades": len(self.read_trades()),
            "snapshots": len(self.read_snapshots()),
        }


# ---------------------------------------------------------------------------
# Convenience: full ingest pipeline (one-shot)
# ---------------------------------------------------------------------------


def ingest_all(
    *,
    weather_locations: list[tuple[str, float, float]] | None = None,
    backfill_days: int = 90,
    markets_limit: int | None = 2000,
    use_resolvedmarkets: bool = False,
    resolvedmarkets_market_ids: list[str] | None = None,
) -> dict[str, int]:
    """Run all 4 ingest modules and write to the unified datastore.

    This is the entry point for a full backtest data refresh.

    Args:
        weather_locations: list of (city, lat, lon) for weather backfill.
            Defaults to a small set if None.
        backfill_days: how many days of historical actuals to fetch.
        markets_limit: cap on number of closed markets to fetch from Gamma.
        use_resolvedmarkets: if True, also fetch orderbook snapshots for each
            weather market (requires RESOLVEDMARKETS_API_KEY).
        resolvedmarkets_market_ids: specific condition_ids to fetch snapshots for.

    Returns a dict of row counts per table.
    """
    if weather_locations is None:
        # Default: 15 cities from Junbo's PDF spec
        weather_locations = [
            ("Miami", 25.7617, -80.1918),
            ("NewYork", 40.7128, -74.0060),
            ("LosAngeles", 34.0522, -118.2437),
            ("Chicago", 41.8781, -87.6298),
            ("Houston", 29.7604, -95.3698),
            ("London", 51.5074, -0.1278),
            ("Paris", 48.8566, 2.3522),
            ("Tokyo", 35.6762, 139.6503),
            ("Seoul", 37.5665, 126.9780),
            ("Sydney", -33.8688, 151.2093),
            ("Dubai", 25.2048, 55.2708),
            ("Singapore", 1.3521, 103.8198),
            ("Berlin", 52.5200, 13.4050),
            ("Madrid", 40.4168, -3.7038),
            ("Rome", 41.9028, 12.4964),
        ]

    ds = UnifiedDatastore()

    # 1. Markets (Polymarket closed markets)
    logger.info("=== [1/4] Polymarket markets ===")
    try:
        from data_pipeline.polymarket_ingest import PolymarketIngest

        poly_ingest = PolymarketIngest()
        markets_df = poly_ingest.fetch_closed_markets(limit=markets_limit)
        # Note: full city/threshold parsing happens in engine/market_parser;
        # here we just persist raw + parsed outcomes.
        if not markets_df.empty:
            # Rename to unified schema
            unified = markets_df.rename(
                columns={
                    "id": "market_id",
                    "endDate": "end_date",
                    "closedTime": "closed_time",
                }
            )
            # target_date and city/threshold are derived later by market_parser
            ds.write_markets(unified)
    except Exception as exc:
        logger.error("Polymarket ingest failed: %s", exc)

    # 2. Weather actuals (ground truth)
    logger.info("=== [2/4] Weather actuals (Open-Meteo Archive) ===")
    try:
        import pandas as pd

        from data_pipeline.weather_ensemble import backfill_archive_many

        end_date = datetime.now(UTC).strftime("%Y-%m-%d")
        start_date = (datetime.now(UTC) - pd.Timedelta(days=backfill_days)).strftime(
            "%Y-%m-%d"
        )
        actuals_df = backfill_archive_many(
            weather_locations,
            start_date=start_date,
            end_date=end_date,
        )
        if not actuals_df.empty:
            actuals_df["date"] = pd.to_datetime(actuals_df["date"], utc=True)
            ds.write_actuals(actuals_df)
    except Exception as exc:
        logger.error("Weather actuals ingest failed: %s", exc)

    # 3. Forecasts — per-model historical + live ensemble
    # ----------------------------------------------------------------
    # The forecast join is split into two parts:
    #   (a) Historical backfill — uses Open-Meteo Historical Forecast API
    #       (historical-forecast-api.open-meteo.com, free, no key) to
    #       retrieve per-model 0-day-ahead (analysis) forecasts for every
    #       past date in our markets table. This replaces the synthetic
    #       per-model probability fallback in karpathy_weekly.py.
    #   (b) Live ensemble — uses Open-Meteo Forecast API for the next
    #       14 days (still needed for forward-looking markets).
    # ----------------------------------------------------------------
    logger.info("=== [3/4] Weather forecasts (historical backfill + live ensemble) ===")
    try:
        import time as _time

        import pandas as pd

        from data_pipeline.weather_ensemble import (
            backfill_historical_forecasts_many,
            fetch_forecast_ensemble,
        )

        forecast_frames: list[pd.DataFrame] = []

        # (a) Historical backfill: derive date range from the markets table
        #     we just wrote in step 1. Covers every past target_date so the
        #     forecast join in karpathy_weekly has real per-model values.
        try:
            markets_df_for_range = ds.read_markets()
            date_col = None
            for cand in ("target_date", "end_date"):
                if cand in markets_df_for_range.columns:
                    date_col = cand
                    break
            if date_col and not markets_df_for_range.empty:
                dt_series = pd.to_datetime(
                    markets_df_for_range[date_col], utc=True, errors="coerce"
                ).dropna()
                if not dt_series.empty:
                    hist_start = dt_series.min().strftime("%Y-%m-%d")
                    hist_end = max(
                        dt_series.max().strftime("%Y-%m-%d"),
                        (datetime.now(UTC) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    )
                    logger.info(
                        "Historical forecast backfill %s..%s across %d cities",
                        hist_start,
                        hist_end,
                        len(weather_locations),
                    )
                    hist_df = backfill_historical_forecasts_many(
                        weather_locations,
                        start_date=hist_start,
                        end_date=hist_end,
                    )
                    if not hist_df.empty:
                        # Reshape to match unified_forecasts schema
                        hist_df["target_date"] = pd.to_datetime(
                            hist_df["date"], utc=True, errors="coerce"
                        )
                        hist_df["fetched_at"] = pd.Timestamp.utcnow()
                        forecast_frames.append(
                            hist_df[
                                [
                                    "city",
                                    "latitude",
                                    "longitude",
                                    "target_date",
                                    "model",
                                    "variable",
                                    "value",
                                    "fetched_at",
                                ]
                            ]
                        )
                        logger.info(
                            "Historical forecast backfill: %d rows across %d models",
                            len(hist_df),
                            hist_df["model"].nunique(),
                        )
        except Exception as exc:
            logger.warning("Historical forecast backfill skipped: %s", exc)

        # (b) Live ensemble — small sample for forward-looking markets
        for city, lat, lon in weather_locations[:5]:  # cap for smoke test
            res = fetch_forecast_ensemble(lat, lon, city=city)
            if res:
                fc = res.forecasts.copy()
                fc["city"] = city
                fc["latitude"] = lat
                fc["longitude"] = lon
                fc["target_date"] = pd.Timestamp(res.target_date, tz="UTC")
                fc["fetched_at"] = pd.Timestamp.utcnow()
                forecast_frames.append(fc)
            _time.sleep(0.5)  # avoid Open-Meteo 429 rate limit

        # (c) NWS deterministic forecast (US cities only — 9th pseudo-model)
        # Free, no key. Adds diversity to the ensemble for US locations.
        try:
            from data_pipeline.weather_ensemble import fetch_nws_forecast

            nws_frames = []
            for city, lat, lon in weather_locations:
                nws_df = fetch_nws_forecast(lat, lon, city=city)
                if not nws_df.empty:
                    nws_df["target_date"] = pd.to_datetime(
                        nws_df["date"], utc=True, errors="coerce"
                    )
                    nws_df["fetched_at"] = pd.Timestamp.utcnow()
                    nws_frames.append(
                        nws_df[
                            [
                                "city",
                                "latitude",
                                "longitude",
                                "target_date",
                                "model",
                                "variable",
                                "value",
                                "fetched_at",
                            ]
                        ]
                    )
                _time.sleep(0.2)  # polite to NWS
            if nws_frames:
                forecast_frames.append(pd.concat(nws_frames, ignore_index=True))
                logger.info(
                    "NWS forecast fetch: %d US-city rows",
                    sum(len(f) for f in nws_frames),
                )
        except Exception as exc:
            logger.warning("NWS forecast fetch skipped: %s", exc)

        if forecast_frames:
            ds.write_forecasts(pd.concat(forecast_frames, ignore_index=True))
    except Exception as exc:
        logger.error("Forecast ingest failed: %s", exc)

    # 4. Resolved Markets snapshots (optional — requires API key)
    if use_resolvedmarkets and resolvedmarkets_market_ids:
        logger.info("=== [4/4] Resolved Markets snapshots ===")
        try:
            from data_pipeline.resolvedmarkets_ingest import client_from_env

            client = client_from_env()
            snapshot_frames = []
            for cid in resolvedmarkets_market_ids[:20]:  # cap for smoke test
                try:
                    df = client.fetch_all_snapshots(cid, interval="1h")
                    if not df.empty:
                        df["market_id"] = cid
                        snapshot_frames.append(df)
                except Exception as exc:
                    logger.warning("Snapshots failed for %s: %s", cid, exc)
            if snapshot_frames:
                ds.write_snapshots(pd.concat(snapshot_frames, ignore_index=True))
        except Exception as exc:
            logger.error("Resolvedmarkets ingest failed: %s", exc)
    else:
        logger.info(
            "=== [4/4] Skipped Resolved Markets snapshots (use_resolvedmarkets=False) ==="
        )

    return ds.summary()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(name)-22s  %(message)s"
    )

    print("\n=== Full ingest pipeline (smoke test) ===")
    counts = ingest_all(backfill_days=30, markets_limit=200)
    print("\n=== Unified datastore summary ===")
    print(counts)

    print("\n=== Walk-forward splits ===")
    ds = UnifiedDatastore()
    splits = ds.build_walk_forward_splits(
        lookback_days=14,
        step_days=7,
        test_days=7,
        date_column="end_date",
        table_name="markets",
    )
    print(f"Built {len(splits)} splits")
