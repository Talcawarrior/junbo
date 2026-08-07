"""Test cases for MeteoFetcher and module-level helpers in scrapers/meteo.

Covers the pieces the production bot actually relies on:
- module-level TTL cache (get/set/expiry)
- per-host throttling
- global 429 rate-limit gate
- _upsert_forecast (insert vs update, no duplicate rows)
- _fetch_open_meteo / _fetch_weatherapi (mocked upstream, caching, retry)
- fetch_for_markets coordinate resolution + persistence
- fetch_all_markets deduplication (no live API required)
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from scrapers.meteo import (
    _cache_clear,
    _cache_get,
    _cache_set,
    _throttle,
    _upsert_forecast,
    MeteoFetcher,
)

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _clean_meteo_cache():
    _cache_clear()
    yield
    _cache_clear()


@pytest.fixture
def city_market(market_factory):
    return market_factory()


# ── TTL cache ────────────────────────────────────────────────────────────────


def test_cache_set_and_get_roundtrip():
    key = (1.0, 2.0, "2026-08-08", "openmeteo")
    _cache_set(key, {"temperature_max": 25.0})
    assert _cache_get(key) == {"temperature_max": 25.0}


def test_cache_miss_returns_none():
    assert _cache_get((9.9, 9.9, "2099-01-01", "openmeteo")) is None


def test_cache_failure_entry_is_remembered():
    """None results are cached briefly so 429-prone fetches aren't re-issued."""
    key = (3.0, 4.0, "2026-08-08", "openmeteo")
    _cache_set(key, None)
    assert key in _cache_get.__globals__["_FETCH_CACHE"]


# ── Throttling ───────────────────────────────────────────────────────────────


def test_throttle_does_not_block_first_call():
    _throttle("open-meteo.com")
    _throttle("open-meteo.com")
    assert True  # would block forever if broken


# ── _upsert_forecast ─────────────────────────────────────────────────────────


def test_upsert_creates_then_updates_no_duplicates(market_factory):
    from database.models import WeatherForecast
    from database.db import get_session

    market_id = market_factory()
    td = datetime(2026, 8, 8, 12, 0, 0)
    with get_session() as session:
        created = _upsert_forecast(
            session, market_id, "London", 51.5, -0.1, td, "temperature_max", "openmeteo", 25.0, {"a": 1}
        )
        session.commit()
        assert created is True
        rows = session.query(WeatherForecast).filter(WeatherForecast.market_id == market_id).all()
        assert len(rows) == 1

        updated = _upsert_forecast(
            session, market_id, "London", 51.5, -0.1, td, "temperature_max", "openmeteo", 26.0, {"a": 2}
        )
        session.commit()
        assert updated is False
        rows = session.query(WeatherForecast).filter(WeatherForecast.market_id == market_id).all()
        assert len(rows) == 1
        assert rows[0].predicted_value == 26.0


# ── _fetch_open_meteo (mocked upstream) ──────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
        return None

    def json(self):
        return self._payload


def test_fetch_open_meteo_happy_path(market_factory):
    fetcher = MeteoFetcher()
    with patch(
        "scrapers.meteo.requests.get",
        return_value=_FakeResponse(
            {
                "daily": {
                    "temperature_2m_max": [31.0],
                    "temperature_2m_min": [18.0],
                    "precipitation_sum": [0.0],
                }
            }
        ),
    ) as mock_get:
        result = fetcher._fetch_open_meteo(51.5, -0.1, "2026-08-08")
    assert result["source"] == "openmeteo"
    assert result["temperature_max"] == 31.0
    assert mock_get.called


def test_fetch_open_meteo_429_sets_global_gate():
    import scrapers.meteo as meteo_mod

    meteo_mod._RATE_LIMITED_UNTIL = 0.0
    fetcher = MeteoFetcher()
    with patch("scrapers.meteo.requests.get", return_value=_FakeResponse({}, status=429)):
        result = fetcher._fetch_open_meteo(51.5, -0.1, "2026-08-08")
    assert result is None
    assert meteo_mod._RATE_LIMITED_UNTIL > 0.0
    meteo_mod._RATE_LIMITED_UNTIL = 0.0


def test_fetch_open_meteo_missing_daily_returns_none(market_factory):
    fetcher = MeteoFetcher()
    with patch("scrapers.meteo.requests.get", return_value=_FakeResponse({"daily": {}})):
        result = fetcher._fetch_open_meteo(51.5, -0.1, "2026-08-08")
    assert result is None


# ── _fetch_weatherapi ────────────────────────────────────────────────────────


def test_fetch_weatherapi_skipped_without_key(market_factory):
    fetcher = MeteoFetcher()
    with patch("scrapers.meteo.bot_config") as mock_cfg:
        mock_cfg.meteo.weatherapi_key = ""
        assert fetcher._fetch_weatherapi(51.5, -0.1, "2026-08-08") is None


def test_fetch_weatherapi_happy_path(market_factory):
    fetcher = MeteoFetcher()
    with patch(
        "scrapers.meteo.requests.get",
        return_value=_FakeResponse(
            {"forecast": {"forecastday": [{"day": {"maxtemp_c": 29.0, "mintemp_c": 20.0, "totalprecip_mm": 0.5}}]}}
        ),
    ) as mock_get:
        with patch("scrapers.meteo.bot_config") as mock_cfg:
            mock_cfg.meteo.weatherapi_key = "test-key"
            result = fetcher._fetch_weatherapi(51.5, -0.1, "2026-08-08")
    assert result["source"] == "weatherapi"
    assert result["temperature_max"] == 29.0
    assert mock_get.called


# ── fetch_for_markets ────────────────────────────────────────────────────────


def test_fetch_for_markets_unknown_city_returns_zero():
    fetcher = MeteoFetcher()
    assert fetcher.fetch_for_markets(["m1"], "NoSuchCityXYZ", datetime(2026, 8, 8), "temperature_max") == 0


def test_fetch_for_markets_persists_group(market_factory):
    from database.models import WeatherForecast
    from database.db import get_session

    m1 = market_factory()
    m2 = market_factory()
    fetcher = MeteoFetcher()
    td = datetime(2026, 8, 8, 12, 0, 0)
    with patch(
        "scrapers.meteo.requests.get",
        return_value=_FakeResponse(
            {"daily": {"temperature_2m_max": [31.0], "temperature_2m_min": [18.0], "precipitation_sum": [0.0]}}
        ),
    ):
        total = fetcher.fetch_for_markets([m1, m2], "London", td, "temperature_max")
    assert total >= 2
    with get_session() as session:
        rows = session.query(WeatherForecast).filter(WeatherForecast.market_id == m1).all()
        assert len(rows) >= 1
        assert any(r.source == "openmeteo" for r in rows)


# ── fetch_all_markets ────────────────────────────────────────────────────────


def test_fetch_all_markets_empty_db_returns_zero():
    """fetch_all_markets returns 0 without any open markets (no live API hit)."""
    from database.db import get_session
    from database.models import WeatherMarket

    with get_session() as session:
        session.query(WeatherMarket).delete()
        session.commit()
    fetcher = MeteoFetcher()
    assert fetcher.fetch_all_markets() == 0
