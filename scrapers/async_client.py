"""Async HTTP client with bounded concurrency, per-host throttle, and in-process cache.

Tier 3 #12 from the code-review tier plan. Replaces the sequential
``requests``-based scraper paths with a small aiohttp wrapper that:
  * runs up to ``MAX_CONCURRENT = 8`` requests in parallel
  * spaces calls to the same host by ``_THROTTLE_S = 0.25`` (250 ms)
  * caches successful and failed (``None``) results in-process so a
    cache-hit short-circuits the network
  * falls back to a sequential ``requests``-based path if aiohttp is
    not importable (the production install always has aiohttp because
    ``engine/calculator.py`` imports it, but tests on minimal CI can
    run without it thanks to ``pytest.importorskip``)

The public surface is intentionally tiny so the meteo + polymarket
scrapers can be refactored one at a time:

    client = AsyncHttpClient()
    data   = client.fetch(url, params=..., host=...)            # one call
    data   = client.fetch_one_blocking(url, params=..., host=...)# sync shim
    datas  = client.fetch_many([(url, params, host), ...])      # parallel
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

try:  # aiohttp is in requirements.txt, but aiohttp-less CI must still work.
    import aiohttp  # type: ignore

    _HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - exercised only on minimal CI
    aiohttp = None  # type: ignore
    _HAS_AIOHTTP = False

import requests  # sync fallback / non-aiohttp path

logger = logging.getLogger("SCRAPER_ASYNC")


# ---- Public knobs (module-level so tests can monkeypatch) -------------
MAX_CONCURRENT = 8
_THROTTLE_S = 0.25
_TIMEOUT_S = 15.0
_USER_AGENT = "Junbo/1.0 (+tier3-12)"


# ---- Cache -------------------------------------------------------------
# (url, frozen-params) -> result-or-None. We remember failures (None)
# too so a 429-storm does not get retried by every market in the scan.
_CACHE: dict[tuple, Any] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(url: str, params: dict | None) -> tuple:
    if not params:
        return (url, ())
    return (url, tuple(sorted(params.items())))


def _cache_get(key: tuple) -> tuple[bool, Any]:
    """Return (hit, value). hit=True even when the cached value is None
    so callers can short-circuit failed fetches."""
    with _CACHE_LOCK:
        if key in _CACHE:
            return True, _CACHE[key]
        return False, None


def _cache_set(key: tuple, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value


def cache_clear() -> None:
    """Reset the in-process cache. Tests and the scheduler force-refresh hook
    both call this."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---- Throttle ----------------------------------------------------------
_LAST_CALL_AT: dict[str, float] = {}
_THROTTLE_LOCK = threading.Lock()


def _throttle(host: str) -> None:
    """Block the calling thread until at least _THROTTLE_S seconds have
    passed since the last call to ``host``. The lock is released during
    the sleep so other hosts are not blocked."""
    while True:
        with _THROTTLE_LOCK:
            now = time.monotonic()
            last = _LAST_CALL_AT.get(host, 0.0)
            wait = _THROTTLE_S - (now - last)
            if wait <= 0:
                _LAST_CALL_AT[host] = now
                return
        time.sleep(wait)


# ---- Async core --------------------------------------------------------
async def _async_fetch_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    host: str,
    url: str,
    params: dict | None,
    cache_key: tuple | None = None,
) -> Any:
    """Issue one GET, returning the parsed JSON body or None on failure.

    Acquires the semaphore *before* throttling so up to MAX_CONCURRENT
    calls can queue up per host; this preserves the per-host 250 ms
    spacing regardless of how many markets share the same upstream URL.
    """
    async with sem:
        # Per-host throttle (async-friendly version)
        while True:
            now = time.monotonic()
            with _THROTTLE_LOCK:
                last = _LAST_CALL_AT.get(host, 0.0)
                wait = _THROTTLE_S - (now - last)
                if wait <= 0:
                    _LAST_CALL_AT[host] = now
                    break
            await asyncio.sleep(wait)
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S)
            ) as resp:
                if resp.status != 200:
                    logger.warning("async fetch %s -> HTTP %s", url, resp.status)
                    if cache_key is not None:
                        _cache_set(cache_key, None)
                    return None
                return await resp.json()
        except (TimeoutError, aiohttp.ClientError) as exc:  # type: ignore
            logger.warning("async fetch %s failed: %s", url, exc)
            if cache_key is not None:
                _cache_set(cache_key, None)
            return None


# ---- Public client -----------------------------------------------------
class AsyncHttpClient:
    """Tiny wrapper exposing both async and sync entry points.

    Use ``fetch_many`` for parallel batch work and ``fetch_one_blocking``
    as a drop-in replacement for the legacy ``requests.get(...).json()``
    call sites that have not been refactored yet.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self.max_concurrent = max_concurrent
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()

    # ---- session management -------------------------------------------
    def _ensure_session(self) -> aiohttp.ClientSession:
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp is not installed")
        with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    headers={"User-Agent": _USER_AGENT}
                )
            return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def close(self) -> None:
        """Synchronous close for callers that do not own a running loop."""
        if self._session and not self._session.closed:
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._session.close())
                finally:
                    loop.close()
            except Exception:
                pass
            self._session = None

    # ---- async primitives ---------------------------------------------
    async def _afetch(self, items: list[tuple[str, dict | None, str]]) -> list[Any]:
        """Run all (url, params, host) items in parallel, preserving order.

        Each call gets its own ClientSession that is closed in the same
        event loop as the gather, eliminating the asyncio.run cross-loop
        teardown leaks that occur when the same session is reused across
        independent event loops.
        """
        if not _HAS_AIOHTTP:
            return [None] * len(items)
        sem = asyncio.Semaphore(self.max_concurrent)
        # force_close=True makes each connection close immediately on
        # session.close() rather than returning to a pool, eliminating
        # the cross-event-loop ResourceWarning that shows up on Windows
        # when the test process exits before the pool drains.
        connector = aiohttp.TCPConnector(force_close=True)
        session = aiohttp.ClientSession(
            headers={"User-Agent": _USER_AGENT}, connector=connector
        )
        try:
            tasks = [
                asyncio.create_task(
                    _async_fetch_one(
                        session, sem, host, url, params,
                        cache_key=_cache_key(url, params),
                    )
                )
                for url, params, host in items
            ]
            return await asyncio.gather(*tasks, return_exceptions=False)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await session.close()

    # ---- sync entry points --------------------------------------------
    def fetch_one_blocking(
        self, url: str, params: dict | None = None, host: str = ""
    ) -> Any:
        """Synchronous fetch with cache + throttle. Returns parsed JSON or None.

        Uses aiohttp when available, falling back to ``requests`` so a
        minimal-CI install without aiohttp still works (slower, no
        parallelism, but functionally correct).
        """
        key = _cache_key(url, params)
        hit, cached = _cache_get(key)
        if hit:
            return cached
        if not _HAS_AIOHTTP:
            return self._sync_fetch(url, params, host, key)
        return asyncio.run(self._afetch_one_async(url, params, host, key))

    async def _afetch_one_async(
        self, url: str, params: dict | None, host: str, key: tuple
    ) -> Any:
        results = await self._afetch([(url, params, host)])
        value = results[0] if results else None
        # Also cache here in case _afetch didn't cache (e.g. aiohttp-less path)
        _cache_set(key, value)
        return value

    def _sync_fetch(self, url: str, params: dict | None, host: str, key: tuple) -> Any:
        if host:
            _throttle(host)
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            logger.warning("sync fetch %s failed: %s", url, exc)
            _cache_set(key, None)
            return None
        if resp.status_code != 200:
            _cache_set(key, None)
            return None
        try:
            value = resp.json()
        except ValueError as exc:
            logger.warning("sync fetch %s bad json: %s", url, exc)
            value = None
        _cache_set(key, value)
        return value

    def fetch_many(self, items: list[tuple[str, dict | None, str]]) -> list[Any]:
        """Parallel batch fetch with cache + throttle, preserving order.

        ``items`` is a list of ``(url, params, host)`` tuples. The
        returned list has the same length and ordering; entries are
        the parsed JSON body, or ``None`` on any failure. Cache hits
        short-circuit and do not count against concurrency.
        """
        if not items:
            return []
        # First pass: serve cache hits, leave the rest in their original
        # index so we can rebuild the ordered result.
        pending: list[tuple[int, tuple[str, dict | None, str]]] = []
        out: list[Any] = [None] * len(items)
        for idx, (url, params, host) in enumerate(items):
            key = _cache_key(url, params)
            hit, cached = _cache_get(key)
            if hit:
                out[idx] = cached
            else:
                pending.append((idx, (url, params, host)))
        if not pending:
            return out
        if not _HAS_AIOHTTP:
            for idx, (url, params, host) in pending:
                out[idx] = self._sync_fetch(url, params, host, _cache_key(url, params))
            return out

        # aiohttp path: run all pending in one event-loop iteration.
        ordered = [t for _, t in pending]
        results = asyncio.run(self._afetch(ordered))
        for (idx, (url, params, _host)), value in zip(pending, results):
            out[idx] = value
            _cache_set(_cache_key(url, params), value)
        return out
