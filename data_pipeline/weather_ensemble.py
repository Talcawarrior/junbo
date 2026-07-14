"""Standalone Open-Meteo 8-model ensemble module.

Decoupled from the DB-backed scrapers/meteo.py. This module provides pure
functions that fetch either live forecasts or historical archive data from
Open-Meteo, returning pandas DataFrames ready for backtesting.

Two modes:
  - forecast (live): 14-day forward ensemble for a (lat, lon) pair
  - archive (historical): daily actuals for a date range — used as ground
    truth labels for backtest / Brier scoring
  - historical-forecast (backfill): per-model 0-day-ahead analysis
    forecasts for past dates — replaces synthetic per-model probabilities
    in the karpathy_weekly forecast join.

Models supported (matching Junbo's ensemble):
  gfs_seamless, ecmwf_ifs025, gem_global, icon_global,
  jma_seamless, cma_grapes_global, ukmo_seamless, meteofrance_seamless

Plus an optional NWS source (api.weather.gov) for US locations — free,
no API key. Returns NWS's local-office deterministic forecast for the
next 7 days. Adds a 9th pseudo-model "nws_deterministic" to the ensemble
when called from US lat/lon pairs.

References:
  https://open-meteo.com/en/docs  (forecast ensemble)
  https://open-meteo.com/en/docs/historical-weather-api  (archive)
  https://open-meteo.com/en/docs/historical-forecast-api  (historical forecast)
  https://www.weather.gov/documentation/services-web-api  (NWS API)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
import requests

logger = logging.getLogger("WEATHER_ENSEMBLE")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Historical Forecast API: returns per-model 0-day-ahead (analysis) forecasts
# for past dates. Free, no API key. Docs:
# https://open-meteo.com/en/docs/historical-forecast-api
OPEN_METEO_HISTORICAL_FORECAST_URL = (
    "https://historical-forecast-api.open-meteo.com/v1/forecast"
)

DEFAULT_MODELS = (
    "gfs_seamless",
    "ecmwf_ifs025",
    "gem_global",
    "icon_global",
    "jma_seamless",
    "cma_grapes_global",
    "ukmo_seamless",
    "meteofrance_seamless",
)

DEFAULT_VARIABLES = (
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_speed_10m_max",
)

# Rate-limit guard: Open-Meteo free tier allows ~10k calls/day but punishes
# bursts. We sleep a small amount between calls when iterating over cities.
_INTER_CALL_DELAY_S = 0.15


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EnsembleResult:
    """Container for one (lat, lon, target_date) forecast pull."""

    city: str
    latitude: float
    longitude: float
    target_date: str  # ISO yyyy-mm-dd
    forecasts: pd.DataFrame  # columns: model, variable, value
    weighted_mean: dict[str, float]  # variable -> weighted mean
    weighted_std: dict[str, float]  # variable -> weighted std


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_forecast_ensemble(
    latitude: float,
    longitude: float,
    *,
    city: str = "",
    target_date: str | None = None,
    models: Iterable[str] = DEFAULT_MODELS,
    variables: Iterable[str] = DEFAULT_VARIABLES,
    weights: dict[str, float] | None = None,
    forecast_days: int = 14,
    timeout: float = 30.0,
) -> EnsembleResult | None:
    """Fetch a forward-looking ensemble forecast for a single location.

    Returns an EnsembleResult with per-model daily values and weighted
    ensemble statistics. If the API call fails or returns no data, returns
    None (caller decides how to handle).
    """
    models = tuple(models)
    variables = tuple(variables)

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join(variables),
        "timezone": "auto",
        "models": ",".join(models),
        "forecast_days": forecast_days,
    }

    try:
        resp = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(
            "Ensemble fetch failed for (%s, %s): %s", latitude, longitude, exc
        )
        return None

    # Open-Meteo returns a SINGLE `daily` dict. Per-model values are encoded
    # as "<variable>_<model>" keys (e.g. temperature_2m_max_gfs_seamless).
    daily = payload.get("daily", {})
    if not isinstance(daily, dict) or "time" not in daily:
        logger.warning(
            "Empty daily block from Open-Meteo for (%s, %s)", latitude, longitude
        )
        return None

    times = daily["time"]
    if not times:
        return None

    # Pick target_date = first available day (local "today") if not specified
    if target_date is None:
        target_date = times[0]
    if target_date not in times:
        logger.warning(
            "target_date=%s not in forecast range (%s..%s)",
            target_date,
            times[0],
            times[-1],
        )
        return None

    target_idx = times.index(target_date)

    # Build long DataFrame: one row per (model, variable)
    rows = []
    for var in variables:
        for model in models:
            key = f"{var}_{model}" if len(models) > 1 else var
            series = daily.get(key)
            if series is None or target_idx >= len(series):
                continue
            val = series[target_idx]
            rows.append(
                {
                    "model": model,
                    "variable": var,
                    "value": float(val) if val is not None else None,
                }
            )

    if not rows:
        logger.warning("No rows matched target_date=%s for (%s)", target_date, latitude)
        return None

    df = pd.DataFrame(rows).dropna(subset=["value"])

    # Weighted ensemble stats
    weights = weights or {m: 1.0 / len(models) for m in models}
    weights = _normalize_weights(weights, models)

    wmean, wstd = _weighted_stats(df, weights)

    return EnsembleResult(
        city=city or f"{latitude},{longitude}",
        latitude=latitude,
        longitude=longitude,
        target_date=target_date,
        forecasts=df,
        weighted_mean=wmean,
        weighted_std=wstd,
    )


def fetch_archive_actuals(
    latitude: float,
    longitude: float,
    *,
    start_date: str,
    end_date: str,
    city: str = "",
    variables: Iterable[str] = DEFAULT_VARIABLES,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Fetch historical actuals from Open-Meteo Archive API.

    Used as ground-truth labels for backtest Brier scoring. Returns a
    DataFrame with columns: city, latitude, longitude, date, <variable>...
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "daily": ",".join(variables),
        "timezone": "auto",
    }

    try:
        resp = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(
            "Archive fetch failed for (%s, %s): %s", latitude, longitude, exc
        )
        return pd.DataFrame()

    daily = payload.get("daily", {})
    if not daily or "time" not in daily:
        return pd.DataFrame()

    n = len(daily["time"])
    out = pd.DataFrame({"date": daily["time"]})
    out["city"] = city
    out["latitude"] = latitude
    out["longitude"] = longitude
    for var in variables:
        if var in daily and len(daily[var]) == n:
            out[var] = daily[var]
        else:
            out[var] = [None] * n
    return out


def backfill_archive_many(
    locations: Iterable[tuple[str, float, float]],
    *,
    start_date: str,
    end_date: str,
    variables: Iterable[str] = DEFAULT_VARIABLES,
) -> pd.DataFrame:
    """Backfill archive data for many (city, lat, lon) tuples.

    Concatenates results into a single long DataFrame. Honors a small
    inter-call delay to avoid burst-rate-limiting.
    """
    frames = []
    for city, lat, lon in locations:
        df = fetch_archive_actuals(
            lat,
            lon,
            start_date=start_date,
            end_date=end_date,
            city=city,
            variables=variables,
        )
        if not df.empty:
            frames.append(df)
        time.sleep(_INTER_CALL_DELAY_S)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Historical Forecast API (per-model analysis forecasts for past dates)
# ---------------------------------------------------------------------------


def fetch_historical_forecast_ensemble(
    latitude: float,
    longitude: float,
    *,
    start_date: str,
    end_date: str,
    city: str = "",
    models: Iterable[str] = DEFAULT_MODELS,
    variables: Iterable[str] = ("temperature_2m_max", "temperature_2m_min"),
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Fetch per-model historical forecasts for a date range.

    Uses Open-Meteo's Historical Forecast API (free, no key) to retrieve
    0-day-ahead (analysis) forecasts from each ensemble model. Each row in
    the returned DataFrame is one (date, model, variable) tuple.

    Args:
        latitude, longitude: location
        start_date, end_date: ISO yyyy-mm-dd range (inclusive). Max 365 days.
        city: optional city label for downstream joins
        models: ensemble model names from DEFAULT_MODELS
        variables: which daily variables to fetch. Defaults to max + min
            temperature only (most relevant for Polymarket markets).
        timeout: HTTP timeout seconds

    Returns:
        DataFrame with columns: city, latitude, longitude, date, model,
        variable, value. Empty on failure.
    """
    models = tuple(models)
    variables = tuple(variables)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join(variables),
        "models": ",".join(models),
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "auto",
    }

    try:
        resp = requests.get(
            OPEN_METEO_HISTORICAL_FORECAST_URL, params=params, timeout=timeout
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(
            "Historical forecast fetch failed for (%s, %s) %s..%s: %s",
            latitude,
            longitude,
            start_date,
            end_date,
            exc,
        )
        return pd.DataFrame()

    daily = payload.get("daily", {})
    if not isinstance(daily, dict) or "time" not in daily:
        logger.warning(
            "Empty daily block from Historical Forecast API for (%s, %s)",
            latitude,
            longitude,
        )
        return pd.DataFrame()

    times = daily["time"]
    if not times:
        return pd.DataFrame()

    # Each variable x model has a key like "temperature_2m_max_gfs_seamless"
    # When only one model is requested, the key is just the variable name.
    rows = []
    for var in variables:
        for model in models:
            key = f"{var}_{model}" if len(models) > 1 else var
            series = daily.get(key)
            if series is None:
                continue
            for dt, val in zip(times, series):
                if val is None:
                    continue
                rows.append(
                    {
                        "city": city,
                        "latitude": latitude,
                        "longitude": longitude,
                        "date": dt,
                        "model": model,
                        "variable": var,
                        "value": float(val),
                    }
                )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def backfill_historical_forecasts_many(
    locations: Iterable[tuple[str, float, float]],
    *,
    start_date: str,
    end_date: str,
    models: Iterable[str] = DEFAULT_MODELS,
    variables: Iterable[str] = ("temperature_2m_max", "temperature_2m_min"),
) -> pd.DataFrame:
    """Backfill per-model historical forecasts for many (city, lat, lon) tuples.

    Concatenates results into a single long DataFrame. Honors a small
    inter-call delay to avoid burst-rate-limiting.
    """
    frames = []
    for city, lat, lon in locations:
        df = fetch_historical_forecast_ensemble(
            lat,
            lon,
            start_date=start_date,
            end_date=end_date,
            city=city,
            models=models,
            variables=variables,
        )
        if not df.empty:
            frames.append(df)
        time.sleep(_INTER_CALL_DELAY_S)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# NWS api.weather.gov (US-only, free, no key) — 9th pseudo-model
# ---------------------------------------------------------------------------

NWS_API_BASE = "https://api.weather.gov"
NWS_MODEL_NAME = "nws_deterministic"
# Continental US bounding box (rough): lat 24..50, lon -125..-66
# Plus Alaska/Hawaii bbox approximation:
NWS_US_BBOX = (
    (24.0, 50.0, -125.0, -66.0),  # continental US
    (51.0, 71.0, -180.0, -130.0),  # Alaska
    (18.0, 22.0, -160.0, -154.0),  # Hawaii
)


def _is_us_latlon(lat: float, lon: float) -> bool:
    """Rough check whether a (lat, lon) is within the US (NWS coverage)."""
    for lat_min, lat_max, lon_min, lon_max in NWS_US_BBOX:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return True
    return False


def fetch_nws_forecast(
    latitude: float,
    longitude: float,
    *,
    city: str = "",
    timeout: float = 15.0,
    user_agent: str = "junbo-research/1.0",
) -> pd.DataFrame:
    """Fetch NWS deterministic forecast for a US location.

    Two-step:
      1. GET /points/{lat},{lon} → returns metadata with a `forecast` URL
         for the local NWS office's grid cell.
      2. GET that forecast URL → returns 14 periods (7 days × day/night)
         with temperature + precipitation.

    Returns DataFrame with columns: city, latitude, longitude, date, model,
    variable, value. Empty if (lat, lon) is outside the US or fetch fails.
    """
    if not _is_us_latlon(latitude, longitude):
        return pd.DataFrame()

    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    try:
        r = requests.get(
            f"{NWS_API_BASE}/points/{latitude:.4f},{longitude:.4f}",
            headers=headers,
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.debug(
                "NWS /points returned %s for (%s,%s)",
                r.status_code,
                latitude,
                longitude,
            )
            return pd.DataFrame()
        props = r.json().get("properties", {})
        forecast_url = props.get("forecast")
        if not forecast_url:
            return pd.DataFrame()
        r2 = requests.get(forecast_url, headers=headers, timeout=timeout)
        if r2.status_code != 200:
            return pd.DataFrame()
        periods = r2.json().get("properties", {}).get("periods", [])
    except Exception as exc:
        logger.debug("NWS fetch failed for (%s,%s): %s", latitude, longitude, exc)
        return pd.DataFrame()

    # Collapse day/night periods into daily max + min temps
    by_date: dict[str, dict[str, float]] = {}
    for p in periods:
        iso = p.get("startTime", "")
        if not iso:
            continue
        date = iso[:10]
        temp_f = p.get("temperature")
        if temp_f is None:
            continue
        temp_c = (temp_f - 32.0) * 5.0 / 9.0
        d = by_date.setdefault(date, {"max": temp_c, "min": temp_c})
        if p.get("isDaytime"):
            d["max"] = max(d["max"], temp_c)
        else:
            d["min"] = min(d["min"], temp_c)

    rows = []
    for date, temps in by_date.items():
        rows.append(
            {
                "city": city,
                "latitude": latitude,
                "longitude": longitude,
                "date": date,
                "model": NWS_MODEL_NAME,
                "variable": "temperature_2m_max",
                "value": temps["max"],
            }
        )
        rows.append(
            {
                "city": city,
                "latitude": latitude,
                "longitude": longitude,
                "date": date,
                "model": NWS_MODEL_NAME,
                "variable": "temperature_2m_min",
                "value": temps["min"],
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def brier_score_per_model(
    forecasts_df: pd.DataFrame,
    actuals_df: pd.DataFrame,
    *,
    threshold: float,
    market_type: str = "HIGH",
) -> dict[str, float]:
    """Compute per-model Brier score against realized outcomes.

    forecasts_df: long format with columns [city, date, model, variable, value]
    actuals_df:   wide format with columns [city, date, <variable>...]
    threshold:    strike price in °C
    market_type:  HIGH (T >= threshold) or LOW (T <= threshold)

    Returns {model: brier_score} where 0.0 = perfect, 0.25 = random.
    """
    if forecasts_df.empty or actuals_df.empty:
        return {}

    # Pivot forecasts to wide
    var_col = (
        forecasts_df["variable"].iloc[0]
        if "variable" in forecasts_df.columns
        else "temperature_2m_max"
    )
    fc_wide = (
        forecasts_df[forecasts_df["variable"] == var_col]
        .pivot_table(
            index=["city", "date"], columns="model", values="value", aggfunc="first"
        )
        .reset_index()
    )

    merged = fc_wide.merge(
        actuals_df[["city", "date", var_col]].rename(columns={var_col: "actual"}),
        on=["city", "date"],
        how="inner",
    )
    if merged.empty:
        return {}

    # Realized outcome (1 if YES condition met, 0 if NO)
    if market_type.upper() == "HIGH":
        merged["outcome"] = (merged["actual"] >= threshold).astype(int)
    else:
        merged["outcome"] = (merged["actual"] <= threshold).astype(int)

    # Brier per model: treat each model's "P(YES)" as a rough CDF eval
    # (this is a simplified scorer; full calibration uses normal CDF in
    # utils/probability.py — here we just measure raw model spread)
    scores: dict[str, float] = {}
    model_cols = [
        c for c in merged.columns if c not in {"city", "date", "actual", "outcome"}
    ]
    for m in model_cols:
        series = merged[m]
        if series.isna().all():
            continue
        # Crude P(YES) per model: fraction of ensemble that satisfies condition
        if market_type.upper() == "HIGH":
            p_yes = (series >= threshold).astype(float)
        else:
            p_yes = (series <= threshold).astype(float)
        brier = float(((p_yes - merged["outcome"]) ** 2).mean())
        scores[m] = brier
    return scores


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_weights(
    weights: dict[str, float], models: Iterable[str]
) -> dict[str, float]:
    """Ensure weights are non-negative and sum to 1, covering all models."""
    out = {m: max(0.0, weights.get(m, 0.0)) for m in models}
    total = sum(out.values())
    if total <= 0:
        n = len(out)
        return {m: 1.0 / n for m in out}
    return {m: v / total for m, v in out.items()}


def _weighted_stats(
    df: pd.DataFrame, weights: dict[str, float]
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute weighted mean and std per variable across models."""
    wmean: dict[str, float] = {}
    wstd: dict[str, float] = {}
    for var, grp in df.groupby("variable"):
        models = grp["model"].tolist()
        values = grp["value"].tolist()
        ws = [weights.get(m, 0.0) for m in models]
        total_w = sum(ws)
        if total_w <= 0 or not values:
            continue
        mean = sum(v * w for v, w in zip(values, ws)) / total_w
        var_w = sum(w * (v - mean) ** 2 for v, w in zip(values, ws)) / total_w
        wmean[var] = float(mean)
        wstd[var] = float(var_w) ** 0.5
    return wmean, wstd


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(name)-22s  %(message)s"
    )

    # Quick smoke test: 1 city, 7 days backfill
    print("\n=== Live ensemble forecast (Miami) ===")
    res = fetch_forecast_ensemble(25.7617, -80.1918, city="Miami")
    if res:
        print(f"city={res.city} target={res.target_date}")
        print("weighted_mean:", res.weighted_mean)
        print("weighted_std :", res.weighted_std)

    print("\n=== Archive actuals (Miami, last 7 days) ===")
    end = datetime.now(UTC).strftime("%Y-%m-%d")
    start = pd.Timestamp(end) - pd.Timedelta(days=7)
    df = fetch_archive_actuals(
        25.7617,
        -80.1918,
        city="Miami",
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end,
    )
    print(df.head(10) if not df.empty else "(empty)")

    print("\n=== Historical forecast ensemble (Miami, last 14 days, per-model) ===")
    hist = fetch_historical_forecast_ensemble(
        25.7617,
        -80.1918,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end,
        city="Miami",
    )
    if not hist.empty:
        print(f"rows={len(hist)} models={sorted(hist['model'].unique())}")
        print(
            hist.pivot_table(
                index="date", columns="model", values="value", aggfunc="first"
            ).head(15)
        )
    else:
        print("(empty)")
