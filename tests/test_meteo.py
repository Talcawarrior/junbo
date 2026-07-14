"""Test cases for MeteoFetcher — see test_meteo_cache_ttl.py for cache
behaviour tests.
"""

import pytest

from scrapers.meteo import MeteoFetcher


@pytest.mark.asyncio
async def test_meteo_fetcher_has_required_methods():
    """Smoke test: fetcher exposes the public methods the rest of the
    codebase calls."""
    fetcher = MeteoFetcher()
    assert hasattr(fetcher, "fetch_weather_data")
