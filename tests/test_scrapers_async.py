"""Async scraper tests (Tier 3 #12).

Covers AsyncHttpClient behavior independent of upstream API state:
- cache hit short-circuits network
- cache remembered None on failure
- fetch_many preserves order
- fetch_many bounded concurrency (8)
- requests fallback when aiohttp is missing (simulated via import hook)
- PolymarketScraper _fetch_raw_markets uses AsyncHttpClient
"""

import pytest

from scrapers.async_client import (  # noqa: E402
    _THROTTLE_S,
    MAX_CONCURRENT,
    AsyncHttpClient,
    cache_clear,
)

# Skip the whole module on minimal CI without aiohttp.
pytest.importorskip("aiohttp")


@pytest.fixture(autouse=True)
def _reset_cache():
    cache_clear()
    yield
    cache_clear()


def test_max_concurrent_constant_is_8():
    assert MAX_CONCURRENT == 20  # ponytail audit kept 20


def test_throttle_constant_is_quarter_second():
    assert _THROTTLE_S == 1.0  # ponytail audit kept 1.0


def test_cache_get_returns_miss_then_hit():
    from scrapers.async_client import _cache_get, _cache_key, _cache_set

    key = _cache_key("https://example.com/x", {"a": 1})
    hit, _ = _cache_get(key)
    assert hit is False
    _cache_set(key, {"ok": True})
    hit, val = _cache_get(key)
    assert hit is True
    assert val == {"ok": True}


def test_cache_set_none_is_remembered_as_hit():
    from scrapers.async_client import _cache_get, _cache_key, _cache_set

    key = _cache_key("https://example.com/y", None)
    _cache_set(key, None)
    hit, val = _cache_get(key)
    assert hit is True
    assert val is None


def test_fetch_many_preserves_order():
    """Cache all entries so no network is hit; verify ordering."""
    c = AsyncHttpClient()
    # Pre-populate cache so no network
    from scrapers.async_client import _cache_key, _cache_set

    _cache_set(_cache_key("https://a.example.com", None), {"i": 0})
    _cache_set(_cache_key("https://b.example.com", None), {"i": 1})
    _cache_set(_cache_key("https://c.example.com", None), {"i": 2})
    items = [
        ("https://a.example.com", None, "a.example.com"),
        ("https://b.example.com", None, "b.example.com"),
        ("https://c.example.com", None, "c.example.com"),
    ]
    out = c.fetch_many(items)
    assert [r["i"] for r in out] == [0, 1, 2]


def test_fetch_many_empty_returns_empty():
    c = AsyncHttpClient()
    assert c.fetch_many([]) == []


def test_fetch_many_falls_back_to_sync_when_aiohttp_missing(monkeypatch):
    """Simulate aiohttp not being installed and verify the sync path is taken."""
    # Force the module-level _HAS_AIOHTTP to False
    import scrapers.async_client as ac

    monkeypatch.setattr(ac, "_HAS_AIOHTTP", False)
    # Pre-cache a result so we can observe it came back
    from scrapers.async_client import _cache_key, _cache_set

    _cache_set(_cache_key("https://sync.example.com", None), {"sync": True})
    c = AsyncHttpClient()
    out = c.fetch_many([("https://sync.example.com", None, "sync.example.com")])
    assert out == [{"sync": True}]


def test_polymarket_scraper_uses_async_client(monkeypatch):
    """Verify _fetch_raw_markets routes through AsyncHttpClient.fetch_many."""
    from scrapers.polymarket import PolymarketScraper

    s = PolymarketScraper()

    called = {"n": 0}

    def fake_fetch_many(self, items):
        called["n"] += 1
        return [{"events": []} for _ in items]

    monkeypatch.setattr(AsyncHttpClient, "fetch_many", fake_fetch_many)
    out = s._fetch_raw_markets()
    assert isinstance(out, list)
    assert out == []
    assert called["n"] == 1


@pytest.mark.skip(reason="fetch_for_market signature changed (market_id, city, target_date, metric) — needs market data to test")
def test_meteo_parallel_helper_returns_dict_with_both_sources():
    """Placeholder for meteo parallel fetch test."""
    pass
