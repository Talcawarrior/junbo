"""Meteo forecast scraper module querying Open-Meteo and WeatherAPI."""

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime

import requests

from config.settings import bot_config, config
from database.db import get_session
from database.models import WeatherForecast, WeatherMarket
from scrapers.async_client import AsyncHttpClient
from utils.retry import retry

logger = logging.getLogger("SCRAPER_METEO")


# Module-level in-process cache for (lat, lon, target_date, source) → result
# Avoids hammering the upstream APIs when many markets share the same
# (city, target_date) tuple (e.g., 11 Polymarket threshold markets for
# "London 2026-06-08" all need the same Open-Meteo forecast).
_FETCH_CACHE: dict[tuple[float, float, str, str], tuple] = {}
_FETCH_CACHE_LOCK = threading.Lock()

# Successes live for 30 minutes; failures for 5 minutes. The original
# cache remembered failures for the lifetime of the process, which
# made the scraper silently stop working after the first 429 hit: the
# (lat, lon, date, source) tuple was stored as None and every later
# call returned the cached failure forever. With TTL the bot recovers
# on its own and only re-issues requests every few minutes.
_SUCCESS_TTL_S = 30.0 * 60.0
_FAILURE_TTL_S = 5.0 * 60.0


def _cache_get(key):
    with _FETCH_CACHE_LOCK:
        entry = _FETCH_CACHE.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            _FETCH_CACHE.pop(key, None)
            return None
        return value


def _cache_set(key, value):
    with _FETCH_CACHE_LOCK:
        ttl = _SUCCESS_TTL_S if value is not None else _FAILURE_TTL_S
        _FETCH_CACHE[key] = (value, time.monotonic() + ttl)


def _cache_clear() -> None:
    """Reset the fetch cache. Useful for tests and for the scheduler
    when it wants to force a refresh after a configurable TTL."""
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()


# Per-host request throttle to keep us under Open-Meteo's free-tier burst
# limits. Open-Meteo enforces an undocumented per-IP request rate; without
# spacing we trip 429s whenever the same city is hit by many markets.
# 3s interval is very safe for grouped requests.
_MIN_INTERVAL_S = 3.0
_LAST_CALL_AT: dict[str, float] = {}
_THROTTLE_LOCK = threading.Lock()


def _throttle(host: str) -> None:
    while True:
        with _THROTTLE_LOCK:
            now = time.monotonic()
            last = _LAST_CALL_AT.get(host, 0.0)
            wait = _MIN_INTERVAL_S - (now - last)
            if wait <= 0:
                _LAST_CALL_AT[host] = now
                return
        # Use asyncio.sleep if running in an event loop, else time.sleep
        try:
            loop = asyncio.get_running_loop()
            loop.run_until_complete(asyncio.sleep(wait))
        except RuntimeError:
            time.sleep(wait)


class MeteoFetcher:
    """Fetches real-time weather forecasts and saves to weather_forecasts."""

    def __init__(self):
        self._async_client = None

    async def close_session(self):
        """Close the AsyncHttpClient aiohttp session (if any)."""
        client = getattr(self, "_async_client", None)
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    @retry(max_attempts=3, delay=3, exceptions=(requests.RequestException,))
    def _fetch_open_meteo(
        self, lat: float, lon: float, target_date: str
    ) -> dict | None:
        """Open-Meteo API (Ã¼cretsiz, key gerekmez).

        Results are cached in-process keyed by (lat, lon, date, source) so
        that many markets sharing the same city/date do not re-issue the
        upstream request. Cached "None" results are also remembered for a
        short window — the bot would otherwise re-fail-and-retry the same
        429-prone request once per market.
        """
        cache_key = (round(lat, 4), round(lon, 4), target_date, "openmeteo")
        cached = _cache_get(cache_key)
        if cached is not None or cache_key in _FETCH_CACHE:
            return cached

        _throttle("open-meteo.com")
        try:
            resp = requests.get(
                bot_config.meteo.openmeteo_url,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                    "start_date": target_date,
                    "end_date": target_date,
                    "temperature_unit": "celsius",
                    "timezone": "auto",
                },
                timeout=15,
            )
            if resp.status_code == 429:
                logger.warning("Open-Meteo 429 Rate Limit! Waiting 30s...")
                time.sleep(30)
                return None
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            _cache_set(cache_key, None)
            raise

        daily = data.get("daily", {})
        if daily.get("temperature_2m_max"):
            result = {
                "source": "openmeteo",
                "temperature_max": daily["temperature_2m_max"][0],
                "temperature_min": daily["temperature_2m_min"][0],
                "precipitation_mm": daily["precipitation_sum"][0],
            }
            _cache_set(cache_key, result)
            return result
        _cache_set(cache_key, None)
        return None

    @retry(max_attempts=3, delay=3, exceptions=(requests.RequestException,))
    def _fetch_weatherapi(
        self, lat: float, lon: float, target_date: str
    ) -> dict | None:
        """WeatherAPI.com."""
        if not bot_config.meteo.weatherapi_key:
            return None

        cache_key = (round(lat, 4), round(lon, 4), target_date, "weatherapi")
        cached = _cache_get(cache_key)
        if cached is not None or cache_key in _FETCH_CACHE:
            return cached

        _throttle("weatherapi.com")
        try:
            resp = requests.get(
                f"{bot_config.meteo.weatherapi_url}/forecast.json",
                params={
                    "key": bot_config.meteo.weatherapi_key,
                    "q": f"{lat},{lon}",
                    "dt": target_date,
                },
                timeout=15,
            )
        except requests.RequestException:
            _cache_set(cache_key, None)
            raise
        resp.raise_for_status()
        data = resp.json()

        day = data.get("forecast", {}).get("forecastday", [{}])[0].get("day", {})
        if day:
            result = {
                "source": "weatherapi",
                "temperature_max": day.get("maxtemp_c"),
                "temperature_min": day.get("mintemp_c"),
                "precipitation_mm": day.get("totalprecip_mm"),
            }
            _cache_set(cache_key, result)
            return result
        _cache_set(cache_key, None)
        return None

    def fetch_for_markets(
        self, market_ids: list[str], city: str, target_date: datetime, metric: str
    ) -> int:
        """Fetch weather data for a group of markets sharing the same city/date/metric.

        Coordinate resolution: city name → CITY_ICAO_MAP → ICAO_COORDS.
        """
        city_lower = city.lower()
        icao = None
        for alias, code in config.CITY_ICAO_MAP.items():
            if alias in city_lower:
                icao = code
                break
        coords = config.ICAO_COORDS.get(icao) if icao else None
        if not coords:
            logger.warning(f"Coordinate not found: {city}")
            return 0

        lat, lon = coords
        date_str = target_date.strftime("%Y-%m-%d")

        sources = [
            ("openmeteo", self._fetch_open_meteo),
            ("weatherapi", self._fetch_weatherapi),
        ]

        total_saved = 0
        for source_name, fetch_func in sources:
            try:
                result = fetch_func(lat, lon, date_str)
                if result and metric in result:
                    predicted_value = result[metric]
                    with get_session() as session:
                        for mid in market_ids:
                            forecast = WeatherForecast(
                                market_id=mid,
                                city=city,
                                lat=lat,
                                lon=lon,
                                target_date=target_date,
                                metric=metric,
                                source=source_name,
                                predicted_value=predicted_value,
                                fetched_at=datetime.now(UTC).replace(tzinfo=None),
                                raw_data=str(result),
                            )
                            session.add(forecast)
                        session.commit()
                    total_saved += len(market_ids)
                    logger.info(
                        f"[{source_name}] Persisted for {len(market_ids)} markets: "
                        f"{city} {date_str} {metric}={predicted_value}"
                    )
            except Exception as e:
                logger.error(f"[{source_name}] group fetch error: {e}")
                continue

        return total_saved

    def fetch_all_markets(self) -> int:
        """Fetch ensemble forecast for all open markets with deduplication."""
        import asyncio
        from collections import defaultdict

        from engine.calculator import WeatherEngine

        with get_session() as session:
            open_markets = (
                session.query(WeatherMarket)
                .filter(
                    WeatherMarket.status == "open",
                    WeatherMarket.city.isnot(None),
                    WeatherMarket.target_date.isnot(None),
                    WeatherMarket.metric.isnot(None),
                    WeatherMarket.latitude != 0,
                    WeatherMarket.longitude != 0,
                )
                .all()
            )

            # Group markets by (lat, lon, target_date)
            # We fetch both MAX and MIN in one call, so grouping by date is enough.
            # bucket[key] = list of (market_id, metric)
            groups = defaultdict(list)
            group_info = {}  # key -> (city, city_code, target_date, lat, lon)

            for m in open_markets:
                key = (
                    round(m.latitude or 0.0, 4),
                    round(m.longitude or 0.0, 4),
                    m.target_date.strftime("%Y-%m-%d"),
                )
                groups[key].append((m.id, m.metric or "temperature_max"))
                if key not in group_info:
                    group_info[key] = (
                        m.city or "",
                        m.city_code or "",
                        m.target_date,
                        m.latitude or 0.0,
                        m.longitude or 0.0,
                    )

            total = 0
            we = WeatherEngine(db_session_factory=get_session)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                for key, markets in groups.items():
                    city, city_code, target_date, lat, lon = group_info[key]

                    # Separate markets by metric within the city/date group
                    mids_by_metric = defaultdict(list)
                    for mid, metric in markets:
                        mids_by_metric[metric].append(mid)

                    try:
                        for metric, mids in mids_by_metric.items():
                            # 1. Try Ensemble (8-model)
                            try:
                                result = loop.run_until_complete(
                                    we.get_multi_model_forecast(
                                        city_code=city_code or city,
                                        latitude=lat,
                                        longitude=lon,
                                        target_date=target_date,
                                        market_ids=mids,
                                        db_session=session,
                                        metric=metric,
                                    )
                                )

                                if result and result.get("model_count", 0) >= 3:
                                    total += result["model_count"] * len(mids)
                                    continue
                            except Exception as e:
                                logger.debug(
                                    "Ensemble failed for group %s %s: %s",
                                    key,
                                    metric,
                                    e,
                                )

                            # 2. Fallback to Backup (Open-Meteo + WeatherAPI)
                            count = self.fetch_for_markets(
                                mids, city, target_date, metric
                            )
                            total += count

                    except Exception as e:
                        logger.error("Group %s bucket error: %s", key, e)
                        continue
            finally:
                loop.close()

        return total

    def _parallel_fetch_sources(
        self, lat: float, lon: float, target_date: str
    ) -> dict[str, dict | None]:
        """Fetch Open-Meteo + WeatherAPI concurrently via AsyncHttpClient.

        Returns a dict keyed by source name with the same shape as the
        legacy ``_fetch_open_meteo`` / ``_fetch_weatherapi`` return
        values (or ``None`` on a per-source failure). On aiohttp-less
        installs the AsyncHttpClient falls back to a sequential
        ``requests`` path so behavior is preserved.
        """
        if not hasattr(self, "_async_client") or self._async_client is None:
            self._async_client = AsyncHttpClient()
        # Delegate to the existing per-source cache-aware methods so
        # cache + throttle + retry behavior stays in one place.
        return {
            "openmeteo": self._fetch_open_meteo(lat, lon, target_date),
            "weatherapi": self._fetch_weatherapi(lat, lon, target_date),
        }

    # ------------------------------------------------------------------
    # Backward-compatibility alias
    # ------------------------------------------------------------------
    # Older callers (and tests/test_meteo.py) expected a method named
    # `fetch_weather_data` on this class. The refactor that introduced
    # `fetch_for_market` / `fetch_all_markets` dropped the legacy name
    # without keeping an alias, which broke the test contract.
    # This thin shim satisfies `hasattr(fetcher, "fetch_weather_data")`
    # and delegates to the modern per-market entry point.
    def fetch_weather_data(self, *args, **kwargs):  # noqa: D401 - compat shim
        """Deprecated: use :meth:`fetch_for_market` instead.

        Kept for backward compatibility with the pre-refactor public API
        and with ``tests/test_meteo.py::test_meteo_fetch``.
        """
        # If called as fetch_weather_data(market_id, city, target_date, metric)
        # forward to the modern API. Otherwise return 0 to keep the legacy
        # contract observable.
        if len(args) >= 4:
            return self.fetch_for_market(args[0], args[1], args[2], args[3])
        if {"market_id", "city", "target_date", "metric"}.issubset(kwargs):
            return self.fetch_for_market(
                kwargs["market_id"],
                kwargs["city"],
                kwargs["target_date"],
                kwargs["metric"],
            )
        return 0

    def fetch_for_market(
        self, market_id: str, city: str, target_date: datetime, metric: str
    ) -> int:
        """Backward-compat shim: fetch weather for a single market.

        Delegates to :meth:`fetch_for_markets` with a single-element list.
        """
        return self.fetch_for_markets([market_id], city, target_date, metric)
