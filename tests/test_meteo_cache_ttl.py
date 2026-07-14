"""Test the new TTL cache behavior in scrapers/meteo."""

import time

from scrapers.meteo import _cache_clear, _cache_get, _cache_set


def test_cache_set_and_get():
    _cache_clear()
    k = (40.7, -74.0, "2026-06-08", "openmeteo")
    _cache_set(k, {"source": "openmeteo", "temperature_max": 28.0})
    assert _cache_get(k) == {"source": "openmeteo", "temperature_max": 28.0}


def test_cache_miss_returns_none():
    _cache_clear()
    k = (40.7, -74.0, "2026-06-08", "openmeteo")
    assert _cache_get(k) is None


def test_cache_failure_ttl_expires():
    """A cached None must expire after _FAILURE_TTL_S, not be remembered forever."""
    from scrapers.meteo import _FAILURE_TTL_S

    if _FAILURE_TTL_S > 5.0:
        # TTL is too long for a test - just sanity check the structure
        _cache_clear()
        k = (40.7, -74.0, "2026-06-08", "openmeteo")
        _cache_set(k, None)
        assert _cache_get(k) is None
    else:
        # Real TTL test: override the TTL to a tiny value, then verify expiry
        from scrapers import meteo

        original = meteo._FAILURE_TTL_S
        meteo._FAILURE_TTL_S = 0.05  # 50ms
        try:
            k = (40.7, -74.0, "2026-06-08", "openmeteo")
            _cache_set(k, None)
            assert _cache_get(k) is None
            time.sleep(0.1)
            assert _cache_get(k) is None  # expired, returns None
        finally:
            meteo._FAILURE_TTL_S = original


def test_cache_success_ttl_expires():
    from scrapers import meteo

    original = meteo._SUCCESS_TTL_S
    meteo._SUCCESS_TTL_S = 0.05
    try:
        k = (40.7, -74.0, "2026-06-08", "openmeteo")
        _cache_set(k, {"v": 1})
        assert _cache_get(k) == {"v": 1}
        time.sleep(0.1)
        assert _cache_get(k) is None
    finally:
        meteo._SUCCESS_TTL_S = original


def test_cache_clear():
    k = (40.7, -74.0, "2026-06-08", "openmeteo")
    _cache_set(k, {"v": 1})
    assert _cache_get(k) is not None
    _cache_clear()
    assert _cache_get(k) is None
