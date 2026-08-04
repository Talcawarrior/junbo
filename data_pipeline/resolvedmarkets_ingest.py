"""Resolved Markets REST API client.

Replaces the previous mock resolved_markets_helper.py with a real client
for https://api.resolvedmarkets.com — a high-fidelity orderbook snapshot
platform that captures Polymarket CLOB depth at up to 10Hz for crypto,
sports, economics, weather, social, and equity markets.

Key capabilities:
  - List live / historical markets with category filtering
  - Fetch historical orderbook snapshots per market (time-range queries)
  - Fetch live orderbook by conditionId or slug
  - Aggregated market summaries (daily OHLCV-style stats)
  - Hyperliquid perp snapshots (for cross-market arbitrage signals)
  - Built-in rate-limit handler (300 req/min on free tier; auto-backoff)
  - Pagination via cursor + has_more

Authentication: all /v1/* data endpoints require X-API-Key header.
Generate a key at https://resolvedmarkets.com/api-keys (free tier:
crypto + weather markets, 300 req/min, 5000 credits/month).

OpenAPI spec: https://resolvedmarkets.com/openapi.json
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger("RESOLVED_MARKETS_INGEST")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.resolvedmarkets.com"
DEFAULT_TIMEOUT = 60.0

# Free tier: 300 req/min = 5 req/s. We leave a 20% safety margin.
FREE_TIER_RATE_LIMIT_PER_MIN = 300
DEFAULT_MIN_INTERVAL_S = 0.25  # 4 req/s — safe under free tier

# Persisted snapshots cache dir
SNAPSHOTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "resolved_snapshots",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ResolvedMarketsConfig:
    """Configuration for the Resolved Markets REST client."""

    api_key: str = os.environ.get("RESOLVEDMARKETS_API_KEY", "")
    base_url: str = BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    # Rate limiter: minimum seconds between requests
    min_interval_s: float = float(
        os.environ.get("RESOLVEDMARKETS_MIN_INTERVAL", str(DEFAULT_MIN_INTERVAL_S))
    )
    # Max retries on 429 / 5xx
    max_retries: int = 5
    # Where to cache snapshot responses (parquet per market_id + day)
    cache_dir: str = SNAPSHOTS_DIR


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ResolvedMarketsClient:
    """REST client for the Resolved Markets API.

    Usage:
        client = ResolvedMarketsClient(api_key="rm_...")
        markets = client.list_live_markets(category="weather")
        snapshots = client.get_market_snapshots(condition_id="0x...", start="2026-06-01", end="2026-06-15")
    """

    def __init__(self, cfg: ResolvedMarketsConfig | None = None):
        self.cfg = cfg or ResolvedMarketsConfig()
        if not self.cfg.api_key:
            logger.warning(
                "ResolvedMarkets: no API key set. All /v1/ endpoints will return 401. "
                "Get a free key at https://resolvedmarkets.com/api-keys."
            )
        self._session = requests.Session()
        self._last_request_at: float = 0.0

    # -- Low-level request helper ----------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.cfg.api_key:
            h["X-API-Key"] = self.cfg.api_key
        return h

    def _throttle(self) -> None:
        """Block until at least min_interval_s has passed since last request."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.cfg.min_interval_s:
            time.sleep(self.cfg.min_interval_s - elapsed)
        self._last_request_at = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        """Make an authenticated request with retry + rate-limit handling.

        Returns parsed JSON (dict or list). Raises on persistent failures.
        """
        url = self.cfg.base_url + path
        for attempt in range(self.cfg.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    timeout=self.cfg.timeout,
                )

                # 429 Too Many Requests — backoff exponentially
                if resp.status_code == 429:
                    backoff = min(60.0, 2.0 * (2**attempt))
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            backoff = float(retry_after)
                        except ValueError:
                            pass
                    logger.warning(
                        "ResolvedMarkets 429 on %s %s — backing off %.1fs (attempt %d/%d)",
                        method,
                        path,
                        backoff,
                        attempt + 1,
                        self.cfg.max_retries,
                    )
                    time.sleep(backoff)
                    continue

                # 5xx — retry
                if 500 <= resp.status_code < 600:
                    if attempt < self.cfg.max_retries:
                        backoff = 0.5 * (2**attempt)
                        logger.warning(
                            "ResolvedMarkets %d on %s %s — retry in %.2fs",
                            resp.status_code,
                            method,
                            path,
                            backoff,
                        )
                        time.sleep(backoff)
                        continue
                    resp.raise_for_status()

                # 401 / 403 / 404 — fail fast
                if resp.status_code in (401, 403):
                    raise PermissionError(
                        f"ResolvedMarkets auth error ({resp.status_code}): {resp.text[:200]}"
                    )
                if resp.status_code == 404:
                    raise FileNotFoundError(
                        f"ResolvedMarkets 404 on {method} {path}: {resp.text[:200]}"
                    )

                resp.raise_for_status()

                # Empty body — return empty dict
                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    return {"_raw_text": resp.text}

            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < self.cfg.max_retries:
                    backoff = 0.5 * (2**attempt)
                    logger.warning(
                        "ResolvedMarkets network error on %s %s: %s — retry in %.2fs",
                        method,
                        path,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise

        raise RuntimeError(
            f"ResolvedMarkets {method} {path} failed after {self.cfg.max_retries + 1} attempts"
        )

    # -- Public endpoints ------------------------------------------------

    def health(self) -> dict:
        """GET /health — service health (public, no auth needed)."""
        # Use a fresh session without auth header for the public health endpoint
        r = self._session.get(self.cfg.base_url + "/health", timeout=self.cfg.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def public_stats(self) -> dict:
        """GET /v1/public-stats — platform-wide stats (public)."""
        return self._request("GET", "/v1/public-stats")

    def list_categories(self) -> list[dict[str, Any]]:
        """GET /v1/categories — list enabled market categories.

        Returns a list of category descriptors. Each descriptor is a dict
        with keys: ``id``, ``category``, ``displayName``, ``activeMarkets``,
        ``captureIntervalMs``, ``refreshIntervalMs``.
        """
        result = self._request("GET", "/v1/categories")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            # Some API versions wrap the list under a "categories" key.
            return result.get("categories", [])
        return []

    # -- Markets ----------------------------------------------------------

    def list_live_markets(self, *, category: str | None = None) -> pd.DataFrame:
        """GET /v1/markets/live — list currently active markets.

        Optional category filter: 'crypto', 'sports', 'economics', 'weather',
        'social', 'equities'.
        """
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        data = self._request("GET", "/v1/markets/live", params=params)
        records = data.get("markets", data) if isinstance(data, dict) else data
        return pd.DataFrame(records or [])

    def list_recent_markets(self, *, limit: int = 100) -> pd.DataFrame:
        """GET /v1/markets/history/recent — tracked markets, most recent first."""
        params = {"limit": limit}
        data = self._request("GET", "/v1/markets/history/recent", params=params)
        records = data.get("markets", data) if isinstance(data, dict) else data
        return pd.DataFrame(records or [])

    def list_historical_markets(
        self,
        *,
        category: str | None = None,
        closed: bool | None = None,
        cursor: str = "",
        limit: int = 100,
    ) -> tuple[pd.DataFrame, str]:
        """GET /v1/markets/history — full market history with cursor pagination.

        Returns (df, next_cursor). Call again with cursor=next_cursor to get
        the next page; stop when next_cursor is empty.
        """
        params: dict[str, Any] = {"limit": limit}
        if category:
            params["category"] = category
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", "/v1/markets/history", params=params)
        if isinstance(data, dict):
            records = data.get("markets", [])
            next_cursor = data.get("next_cursor", "") or data.get("cursor", "") or ""
        else:
            records = data
            next_cursor = ""
        return pd.DataFrame(records or []), next_cursor

    def iter_all_historical_markets(
        self,
        *,
        category: str | None = None,
        max_pages: int = 100,
    ) -> Iterator[pd.DataFrame]:
        """Iterate over ALL historical markets, page by page."""
        cursor = ""
        for _page in range(max_pages):
            df, cursor = self.list_historical_markets(category=category, cursor=cursor)
            if df.empty:
                break
            yield df
            if not cursor:
                break

    def get_market_by_slug(self, slug: str) -> dict:
        """GET /v1/markets/by-slug/{slug} — lookup market by URL slug."""
        return self._request("GET", f"/v1/markets/by-slug/{slug}")

    def get_market_summary(self, condition_id: str) -> dict:
        """GET /v1/markets/{id}/summary — aggregated stats for a market."""
        return self._request("GET", f"/v1/markets/{condition_id}/summary")

    # -- Orderbooks & snapshots ------------------------------------------

    def get_live_orderbook(self, condition_id: str) -> dict:
        """GET /v1/markets/{id}/orderbook — live orderbook by conditionId."""
        return self._request("GET", f"/v1/markets/{condition_id}/orderbook")

    def get_live_orderbook_by_slug(self, slug: str) -> dict:
        """GET /v1/markets/by-slug/{slug}/orderbook — live orderbook by slug."""
        return self._request("GET", f"/v1/markets/by-slug/{slug}/orderbook")

    def get_market_snapshots(
        self,
        condition_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        interval: str = "5m",
        limit: int = 1000,
        cursor: str = "",
    ) -> tuple[pd.DataFrame, str]:
        """GET /v1/markets/{id}/snapshots — historical orderbook snapshots.

        Args:
            condition_id: Polymarket conditionId (0x-prefixed hex)
            start: ISO8601 timestamp (e.g. "2026-06-01T00:00:00Z")
            end:   ISO8601 timestamp
            interval: '5m', '15m', '1h', or '1d' (resample granularity)
            limit: page size (max 1000)
            cursor: pagination cursor

        Returns (df, next_cursor). Snapshots include bids/asks arrays,
        mid price, spread, depth totals, and timestamp.
        """
        params: dict[str, Any] = {"interval": interval, "limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if cursor:
            params["cursor"] = cursor
        data = self._request(
            "GET", f"/v1/markets/{condition_id}/snapshots", params=params
        )
        if isinstance(data, dict):
            records = data.get("snapshots", data.get("data", []))
            next_cursor = data.get("next_cursor", "") or ""
        else:
            records = data
            next_cursor = ""
        return pd.DataFrame(records or []), next_cursor

    def iter_all_snapshots(
        self,
        condition_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        interval: str = "5m",
        max_pages: int = 200,
    ) -> Iterator[pd.DataFrame]:
        """Iterate over ALL snapshots for a market, page by page."""
        cursor = ""
        for _page in range(max_pages):
            df, cursor = self.get_market_snapshots(
                condition_id,
                start=start,
                end=end,
                interval=interval,
                cursor=cursor,
            )
            if df.empty:
                break
            yield df
            if not cursor:
                break

    def fetch_all_snapshots(
        self,
        condition_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        interval: str = "5m",
    ) -> pd.DataFrame:
        """Convenience: fetch all snapshots and concatenate into one DataFrame."""
        frames = list(
            self.iter_all_snapshots(
                condition_id,
                start=start,
                end=end,
                interval=interval,
            )
        )
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        # Cache to disk for offline replay
        os.makedirs(self.cfg.cache_dir, exist_ok=True)
        cache_path = os.path.join(
            self.cfg.cache_dir, f"{condition_id}_{interval}.parquet"
        )
        try:
            df.to_parquet(cache_path, index=False)
            logger.info("Cached %d snapshots to %s", len(df), cache_path)
        except Exception as exc:
            logger.warning("Failed to cache snapshots: %s", exc)
        return df

    # -- Hyperliquid perp snapshots --------------------------------------

    def get_hyperliquid_live_orderbook(self, coin: str = "BTC") -> dict:
        """GET /v1/exchange/orderbook — Hyperliquid perp live book."""
        return self._request("GET", "/v1/exchange/orderbook", params={"coin": coin})

    def get_hyperliquid_snapshots(
        self,
        *,
        coin: str = "BTC",
        start: str | None = None,
        end: str | None = None,
        interval: str = "5m",
        limit: int = 1000,
    ) -> pd.DataFrame:
        """GET /v1/exchange/snapshots — Hyperliquid historical snapshots."""
        params: dict[str, Any] = {"coin": coin, "interval": interval, "limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request("GET", "/v1/exchange/snapshots", params=params)
        if isinstance(data, dict):
            records = data.get("snapshots", data.get("data", []))
        else:
            records = data
        return pd.DataFrame(records or [])

    # -- Backtest endpoint (server-side) ---------------------------------

    def run_backtest(self, strategy_spec: dict) -> dict:
        """POST /v1/backtest/run — enqueue a server-side backtest.

        strategy_spec must follow the Resolved Markets DSL (see API docs).
        Returns a run_id that can be polled via get_backtest_status().
        """
        return self._request("POST", "/v1/backtest/run", json_body=strategy_spec)

    def get_backtest_status(self, run_id: str) -> dict:
        """GET /v1/backtest/runs/{runId} — poll backtest run status."""
        return self._request("GET", f"/v1/backtest/runs/{run_id}")

    def list_backtest_history(self, *, limit: int = 20) -> pd.DataFrame:
        """GET /v1/backtest/history — recent backtest runs."""
        data = self._request("GET", "/v1/backtest/history", params={"limit": limit})
        if isinstance(data, dict):
            records = data.get("runs", data.get("data", []))
        else:
            records = data
        return pd.DataFrame(records or [])


# ---------------------------------------------------------------------------
# Convenience: factory that reads from env
# ---------------------------------------------------------------------------


def client_from_env() -> ResolvedMarketsClient:
    """Build a client using RESOLVEDMARKETS_API_KEY env var."""
    return ResolvedMarketsClient(ResolvedMarketsConfig())


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(name)-25s  %(message)s"
    )
    client = client_from_env()

    print("\n=== Health check ===")
    try:
        h = client.health()
        print(h)
    except Exception as e:
        print(f"Health failed: {e}")

    print("\n=== Public stats ===")
    try:
        s = client.public_stats()
        print(s)
    except Exception as e:
        print(f"Stats failed: {e}")

    print("\n=== Categories ===")
    try:
        cats = client.list_categories()
        print(cats)
    except Exception as e:
        print(f"Categories failed: {e}")

    print("\n=== Live weather markets ===")
    try:
        df = client.list_live_markets(category="weather")
        print(f"Markets: {len(df)}")
        if not df.empty:
            print(df.head(5).to_string())
    except Exception as e:
        print(f"Live markets failed: {e}")
