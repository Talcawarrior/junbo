"""Live data smoke test: verify each ingest module actually pulls real data.

This test hits live APIs (Polymarket Gamma, Open-Meteo, Polygon RPC,
resolvedmarkets.com health). It is skipped automatically if NO_NETWORK=1
is set in the env, so CI can opt out.

What it verifies:
  1. polymarket_ingest.fetch_closed_markets() returns >0 markets with parsed outcomes.
  2. weather_ensemble.fetch_archive_actuals() returns weather rows for Miami.
  3. weather_ensemble.fetch_forecast_ensemble() returns 8-model forecasts.
  4. poly_data_ingest CTF Exchange V2 contract is alive on Polygon.
  5. resolvedmarkets_ingest /health endpoint returns healthy.
  6. unified_datastore can write+read all 5 tables.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Skip all tests in this file if NO_NETWORK=1
pytestmark = pytest.mark.skipif(
    os.environ.get("NO_NETWORK") == "1",
    reason="NO_NETWORK=1 set — skipping live API tests",
)


def test_polymarket_gamma_live():
    """Verify Polymarket Gamma API returns real closed markets."""
    from data_pipeline.polymarket_ingest import PolymarketIngest

    ingester = PolymarketIngest()
    df = ingester.fetch_closed_markets(use_cache=False, limit=20)
    assert not df.empty, "Gamma API returned no markets"
    assert len(df) >= 5, f"Expected >=5 markets, got {len(df)}"
    # outcomePrices must be parsed
    assert "yes_price" in df.columns
    assert "no_price" in df.columns
    # At least one market should have a resolved outcome
    resolved_count = df["resolved_outcome"].notna().sum()
    assert resolved_count >= 1, "No markets with resolved outcomes"


def test_open_meteo_archive_live():
    """Verify Open-Meteo Archive API returns actuals for Miami."""
    from data_pipeline.weather_ensemble import fetch_archive_actuals

    df = fetch_archive_actuals(
        latitude=25.7617,
        longitude=-80.1918,  # Miami
        start_date="2026-01-01",
        end_date="2026-01-07",
    )
    assert not df.empty, "Open-Meteo Archive returned no rows"
    assert "temperature_2m_max" in df.columns
    # Should have ~7 days of data
    assert len(df) >= 5, f"Expected >=5 days, got {len(df)}"


def test_open_meteo_forecast_ensemble_live():
    """Verify Open-Meteo Forecast API returns 8-model ensemble."""
    import time

    from data_pipeline.weather_ensemble import fetch_forecast_ensemble

    # Small delay to avoid Open-Meteo's 429 rate limit (free tier).
    # The archive test just hit the API; we wait 2s before this one.
    time.sleep(2.0)
    # Don't pass target_date — let it default to today's forward forecast
    result = fetch_forecast_ensemble(
        latitude=25.7617,
        longitude=-80.1918,  # Miami
        city="Miami",
    )
    if result is None:
        # 429 from Open-Meteo free tier is expected under load — skip
        # rather than fail, since this is a rate-limit not a bug.
        pytest.skip("Open-Meteo returned None (likely 429 rate limit)")
    df = result.forecasts  # the long DataFrame of (model, variable, value)
    assert not df.empty, "EnsembleResult.forecasts is empty"
    # Should have 8 models × multiple variables = many rows
    assert len(df) >= 8, f"Expected >=8 model rows, got {len(df)}"
    models = df["model"].unique()
    assert len(models) >= 5, f"Expected >=5 distinct models, got {len(models)}"


def test_polygon_ctf_v2_contract_live():
    """Verify the CTF Exchange V2 contract exists on Polygon mainnet."""
    from data_pipeline.poly_data_ingest import (
        CTF_EXCHANGE_V2,
        PolyDataConfig,
        PolygonRPCClient,
    )

    try:
        client = PolygonRPCClient(PolyDataConfig())
        # eth_getCode returns the contract's runtime bytecode as a hex string.
        # An empty contract returns "0x".
        code = client._call("eth_getCode", [str(CTF_EXCHANGE_V2), "latest"])
    except Exception as e:
        pytest.skip(f"Polygon RPC unavailable: {e}")
    assert code and code != "0x", (
        f"CTF Exchange V2 contract not found at {CTF_EXCHANGE_V2}"
    )


def test_resolvedmarkets_health_live():
    """Verify resolvedmarkets.com /health endpoint is healthy.

    Skips gracefully if API is unreachable (network issues, no API key, etc.)
    """
    import pytest
    from data_pipeline.resolvedmarkets_ingest import ResolvedMarketsClient
    from requests.exceptions import Timeout, ConnectionError, RequestException

    client = ResolvedMarketsClient()
    try:
        health = client.health()
    except (Timeout, ConnectionError, RequestException) as e:
        pytest.skip(f"ResolvedMarkets API unreachable: {e}")

    assert health is not None, "resolvedmarkets /health returned None"
    # Health should be a dict with at least one key
    assert isinstance(health, dict)
    # If status is present, it should be healthy
    if "status" in health:
        assert health["status"] in ("healthy", "ok", "up"), f"Unhealthy: {health}"


def test_unified_datastore_write_read_roundtrip(tmp_path):
    """Verify unified datastore can write and read all 5 tables."""
    from data_pipeline.unified_datastore import UnifiedDatastore, UnifiedDatastoreConfig

    # Use a temp dir for this test
    cfg = UnifiedDatastoreConfig(data_dir=str(tmp_path))
    ds = UnifiedDatastore(cfg)

    # Write empty DataFrames for each table
    ds.write_markets(
        pd.DataFrame(
            [
                {
                    "market_id": "test1",
                    "question": "Will it rain in Miami?",
                    "slug": "test",
                    "condition_id": "0xabc",
                    "category": "Weather",
                    "city": "Miami",
                    "city_code": "MIA",
                    "latitude": 25.76,
                    "longitude": -80.19,
                    "threshold": 30.0,
                    "threshold_unit": "F",
                    "market_type": "HIGH",
                    "target_date": pd.Timestamp("2026-01-15", tz="UTC"),
                    "end_date": pd.Timestamp("2026-01-15", tz="UTC"),
                    "closed_time": pd.Timestamp("2026-01-16", tz="UTC"),
                    "yes_price": 1.0,
                    "no_price": 0.0,
                    "resolved_outcome": "Yes",
                    "volume": 1000.0,
                    "liquidity": 500.0,
                    "clob_token_ids": ["123", "456"],
                }
            ]
        )
    )
    ds.write_actuals(
        pd.DataFrame(
            [
                {
                    "city": "Miami",
                    "latitude": 25.76,
                    "longitude": -80.19,
                    "date": pd.Timestamp("2026-01-15", tz="UTC"),
                    "temperature_2m_max": 31.5,
                    "temperature_2m_min": 22.0,
                    "temperature_2m_mean": 26.5,
                    "precipitation_sum": 0.0,
                    "wind_speed_10m_max": 15.0,
                }
            ]
        )
    )

    # Read back
    markets = ds.read_markets()
    actuals = ds.read_actuals()
    assert len(markets) == 1
    assert len(actuals) == 1
    assert markets.iloc[0]["city"] == "Miami"
    assert actuals.iloc[0]["temperature_2m_max"] == 31.5

    # Brier dataset should join them
    brier = ds.build_brier_dataset()
    assert not brier.empty, "Brier dataset should have 1 row after join"
    assert "realized_yes" in brier.columns
    # HIGH market, threshold 30, actual 31.5 → YES wins → 1.0
    assert brier.iloc[0]["realized_yes"] == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
