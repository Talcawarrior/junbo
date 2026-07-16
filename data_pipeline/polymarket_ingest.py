"""Polymarket Gamma API bulk market + outcome ingester.

While scrapers/polymarket.py is optimized for *live* weather market search
(targeted at the bot's runtime needs), this module provides bulk historical
ingestion: pull ALL closed markets (or a category subset), extract outcome
labels, resolution dates, and final prices — the ground truth needed for
backtest Brier scoring.

Endpoints used (all public, no auth):
  GET /markets                - paginated list with filters (closed, active, category)
  GET /markets/{id}           - single market detail
  GET /markets/keyset         - cursor-paginated full history (also used by
                                poly_data_ingest for trade joins)
  GET /events                 - market group metadata
  GET /events/{id}            - single event with nested markets

For backtest we care about:
  - question / outcomes / outcomePrices (final resolved prices)
  - endDate (resolution date)
  - closedTime (when market actually closed)
  - volume / liquidity (for filtering to "real" markets)
  - clobTokenIds (to join with on-chain OrderFilled events)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger("POLYMARKET_INGEST")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_PAGE_LIMIT = 100  # Gamma API caps at 100 per page
DEFAULT_TIMEOUT = 30.0
INTER_PAGE_DELAY_S = 0.05  # be polite; Gamma is rate-limit-tolerant but not unlimited

# Local cache (CSV)
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
)
CLOSED_MARKETS_CACHE = os.path.join(DATA_DIR, "polymarket_closed_markets.csv")
ACTIVE_MARKETS_CACHE = os.path.join(DATA_DIR, "polymarket_active_markets.csv")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PolymarketIngestConfig:
    base_url: str = GAMMA_BASE
    page_limit: int = DEFAULT_PAGE_LIMIT
    timeout: float = DEFAULT_TIMEOUT
    inter_page_delay_s: float = INTER_PAGE_DELAY_S
    max_retries: int = 3
    cache_dir: str = DATA_DIR


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PolymarketIngest:
    """Bulk-fetch markets from Gamma API with outcome parsing."""

    def __init__(self, cfg: PolymarketIngestConfig | None = None):
        self.cfg = cfg or PolymarketIngestConfig()
        self._session = requests.Session()

    # -- Low-level --------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        """GET with retry on transient failures."""
        url = self.cfg.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self.cfg.timeout)
                if resp.status_code == 429:
                    backoff = min(60.0, 2.0 * (2**attempt))
                    logger.warning("Gamma 429 on %s — backing off %.1fs", path, backoff)
                    time.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (
                requests.Timeout,
                requests.ConnectionError,
                requests.HTTPError,
            ) as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries:
                    backoff = 0.5 * (2**attempt)
                    logger.debug("Gamma retry %d on %s: %s", attempt + 1, path, exc)
                    time.sleep(backoff)
        raise RuntimeError(
            f"Gamma GET {path} failed after {self.cfg.max_retries + 1} attempts: {last_exc}"
        )

    # -- Public: bulk market fetch ---------------------------------------

    def fetch_markets(
        self,
        *,
        closed: bool | None = None,
        active: bool | None = None,
        category: str | None = None,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        order: str = "endDate",
        ascending: bool = False,
        limit: int | None = None,
        max_pages: int = 1000,
    ) -> pd.DataFrame:
        """Fetch markets with filters, paginating until exhausted or limit hit.

        Args:
            closed: filter by closed=true/false
            active: filter by active=true/false
            category: market category (e.g. 'Crypto', 'Politics', 'Weather')
            end_date_min: ISO date string (e.g. '2026-01-01')
            end_date_max: ISO date string
            order: sort field (default 'endDate')
            ascending: sort direction
            limit: max total markets to return (None = all)
            max_pages: safety bound on pagination

        Returns a DataFrame with all Gamma market fields, plus parsed
        outcomes / outcomePrices / clobTokenIds as Python lists.
        """
        all_markets: list[dict] = []
        offset = 0
        page_size = self.cfg.page_limit

        for page in range(max_pages):
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": offset,
                "order": order,
                "ascending": "true" if ascending else "false",
            }
            if closed is not None:
                params["closed"] = "true" if closed else "false"
            if active is not None:
                params["active"] = "true" if active else "false"
            if category:
                params["category"] = category
            if end_date_min:
                params["end_date_min"] = end_date_min
            if end_date_max:
                params["end_date_max"] = end_date_max

            batch = self._get("/markets", params=params)
            if not isinstance(batch, list) or not batch:
                break

            all_markets.extend(batch)
            logger.info(
                "Gamma page %d: +%d markets (total %d, offset=%d)",
                page + 1,
                len(batch),
                len(all_markets),
                offset,
            )

            if limit is not None and len(all_markets) >= limit:
                all_markets = all_markets[:limit]
                break
            if len(batch) < page_size:
                break  # last page

            offset += page_size
            time.sleep(self.cfg.inter_page_delay_s)

        df = pd.DataFrame(all_markets)
        if df.empty:
            return df

        # Parse JSON-encoded fields
        for col in ("outcomes", "outcomePrices", "clobTokenIds", "events"):
            if col in df.columns:
                df[col] = df[col].apply(_safe_json_loads)

        # Derive YES/NO outcome prices (final resolved prices for closed markets)
        df["yes_price"] = df.apply(_extract_outcome_price, axis=1, args=("yes",))
        df["no_price"] = df.apply(_extract_outcome_price, axis=1, args=("no",))
        # Final outcome string ("Yes" / "No" / null if not resolved)
        df["resolved_outcome"] = df.apply(_extract_resolved_outcome, axis=1)
        return df

    def fetch_closed_markets(
        self,
        *,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        category: str | None = None,
        limit: int | None = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Convenience: fetch all closed markets (resolved = ground truth).

        Caches to CLOSED_MARKETS_CACHE so repeat calls are instant.
        Pass use_cache=False to force re-fetch.
        """
        if use_cache and os.path.exists(CLOSED_MARKETS_CACHE):
            try:
                df = pd.read_csv(CLOSED_MARKETS_CACHE)
                # Re-parse list columns (CSV flattens them to strings)
                for col in ("outcomes", "outcomePrices", "clobTokenIds"):
                    if col in df.columns:
                        df[col] = df[col].apply(_safe_json_loads)
                logger.info(
                    "Loaded %d closed markets from cache %s",
                    len(df),
                    CLOSED_MARKETS_CACHE,
                )
                return df
            except Exception as exc:
                logger.warning("Cache load failed: %s — refetching", exc)

        df = self.fetch_markets(
            closed=True,
            active=False,
            category=category,
            end_date_min=end_date_min,
            end_date_max=end_date_max,
            limit=limit,
            order="endDate",
            ascending=False,
        )
        os.makedirs(self.cfg.cache_dir, exist_ok=True)
        # L11: Atomic CSV write — temp file then os.replace()
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", dir=self.cfg.cache_dir)
        try:
            with os.fdopen(tmp_fd, "w") as tmp_f:
                df.to_csv(tmp_f, index=False)
            os.replace(tmp_path, CLOSED_MARKETS_CACHE)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        logger.info("Cached %d closed markets to %s", len(df), CLOSED_MARKETS_CACHE)
        return df

    def fetch_active_markets(
        self, *, category: str | None = None, limit: int | None = None
    ) -> pd.DataFrame:
        """Fetch all currently active (not yet resolved) markets."""
        df = self.fetch_markets(
            closed=False,
            active=True,
            category=category,
            limit=limit,
            order="volume",
            ascending=False,
        )
        return df

    def fetch_market_detail(self, market_id: str) -> dict:
        """GET /markets/{id} — single market detail."""
        return self._get(f"/markets/{market_id}")

    # -- Events (market groups) ------------------------------------------

    def fetch_events(
        self,
        *,
        closed: bool | None = None,
        limit: int = 100,
        max_pages: int = 100,
    ) -> pd.DataFrame:
        """GET /events — market group metadata."""
        all_events: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if closed is not None:
                params["closed"] = "true" if closed else "false"
            batch = self._get("/events", params=params)
            if not isinstance(batch, list) or not batch:
                break
            all_events.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            time.sleep(self.cfg.inter_page_delay_s)
        return pd.DataFrame(all_events)

    # -- Weather-specific helper -----------------------------------------

    def fetch_weather_markets(
        self,
        *,
        end_date_min: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch weather prediction markets from Gamma.

        Polymarket doesn't have a strict 'Weather' category; weather markets
        are detected by question text matching (temperature, rain, snow,
        hurricane, etc.). We fetch closed markets with no category filter and
        then filter locally.
        """
        df = self.fetch_closed_markets(end_date_min=end_date_min, limit=limit)
        if df.empty:
            return df
        weather_keywords = (
            "temperature",
            "temp ",
            "°c",
            "°f",
            "celsius",
            "fahrenheit",
            "rain",
            "snow",
            "precipitation",
            "hurricane",
            "weather",
            "highest",
            "lowest",
            "warmest",
            "coldest",
        )
        mask = (
            df["question"]
            .fillna("")
            .str.lower()
            .str.contains(
                "|".join(weather_keywords),
                regex=True,
                na=False,
            )
        )
        weather_df: pd.DataFrame = df[mask].copy()  # type: ignore[assignment]
        logger.info(
            "Filtered %d weather markets out of %d closed", len(weather_df), len(df)
        )
        return weather_df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_loads(val: Any) -> Any:
    """Parse a JSON-encoded string; return original if not parseable."""
    if val is None or val == "":
        return None
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return val
    return val


def _extract_outcome_price(row: pd.Series, side: str) -> float | None:
    """Extract the YES or NO final outcome price from a market row.

    For closed markets, outcomePrices contains the resolved prices
    (e.g. ["0", "1"] means YES=0, NO=1 — NO won).
    Returns None if not parseable.
    """
    prices = row.get("outcomePrices")
    outcomes = row.get("outcomes")
    if not isinstance(prices, list) or not isinstance(outcomes, list):
        return None
    if len(prices) != len(outcomes):
        return None
    target = "yes" if side.lower() == "yes" else "no"
    for label, price in zip(outcomes, prices):
        if str(label).strip().lower() == target:
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
    # Fallback: if labels aren't Yes/No, assume first=YES, second=NO
    if side.lower() == "yes" and len(prices) >= 1:
        try:
            return float(prices[0])
        except (TypeError, ValueError):
            return None
    if side.lower() == "no" and len(prices) >= 2:
        try:
            return float(prices[1])
        except (TypeError, ValueError):
            return None
    return None


def _extract_resolved_outcome(row: pd.Series) -> str | None:
    """Return 'Yes' / 'No' / None based on outcomePrices for a closed market."""
    yes_p = row.get("yes_price")
    no_p = row.get("no_price")
    if yes_p is None or no_p is None:
        return None
    if yes_p == 1.0 and no_p == 0.0:
        return "Yes"
    if no_p == 1.0 and yes_p == 0.0:
        return "No"
    return None  # ambiguous — not cleanly resolved


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(name)-22s  %(message)s"
    )
    ingest = PolymarketIngest()

    print("\n=== Recent closed markets (last 50) ===")
    df = ingest.fetch_markets(
        closed=True,
        active=False,
        limit=50,
        order="endDate",
        ascending=False,
    )
    print(f"Rows: {len(df)}")
    if not df.empty:
        cols = [
            "id",
            "question",
            "endDate",
            "yes_price",
            "no_price",
            "resolved_outcome",
            "volume",
        ]
        cols = [c for c in cols if c in df.columns]
        print(df[cols].head(10).to_string())

    print("\n=== Weather markets (closed, last 30 days) ===")
    from datetime import datetime, timedelta

    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    weather_df = ingest.fetch_weather_markets(end_date_min=cutoff, limit=500)
    print(f"Rows: {len(weather_df)}")
    if not weather_df.empty:
        cols = [
            "id",
            "question",
            "endDate",
            "yes_price",
            "no_price",
            "resolved_outcome",
        ]
        cols = [c for c in cols if c in weather_df.columns]
        print(weather_df[cols].head(10).to_string())
