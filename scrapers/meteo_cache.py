"""Per-process forecast cache with TTL.

The original cache remembered successes AND failures forever, which
caused the scraper to silently stop working after the first 429:
the (lat, lon, date, source) tuple was stored as None and every
subsequent call returned the cached failure for the lifetime of
the bot process. We now store the value alongside a wall-clock
expiry and check it on read.

Defaults:
  - Successes live for 30 minutes (forecasts don't change every
    minute; this cuts the Open-Meteo call volume to ~1/30).
  - Failures live for 5 minutes (long enough to ride out a
    burst of 429s, short enough to retry the next scan cycle).
"""

import time as _time

_FETCH_CACHE: dict[tuple[float, float, str, str], tuple] = {}
_FETCH_CACHE_LOCK = __import__("threading").Lock()

_SUCCESS_TTL_S = 30.0 * 60.0
_FAILURE_TTL_S = 5.0 * 60.0


def _cache_get(key):
    with _FETCH_CACHE_LOCK:
        entry = _FETCH_CACHE.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if _time.monotonic() > expires_at:
            _FETCH_CACHE.pop(key, None)
            return None
        return value


def _cache_set(key, value):
    with _FETCH_CACHE_LOCK:
        ttl = _SUCCESS_TTL_S if value is not None else _FAILURE_TTL_S
        _FETCH_CACHE[key] = (value, _time.monotonic() + ttl)


def _cache_clear() -> None:
    """Reset the fetch cache. Useful for tests and for the scheduler
    when it wants to force a refresh after a configurable TTL."""
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()
