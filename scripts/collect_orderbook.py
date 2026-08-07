#!/usr/bin/env python3
"""Hourly orderbook depth collector for active weather markets.

Fetches market list from Polymarket Gamma API (with proper headers to get
clobTokenIds), then fetches orderbook from CLOB API for each market,
and stores depth metrics in data/orderbook.db.

Does NOT touch bot.db — fully independent data collection.
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
ORDERBOOK_DB = ROOT / "data" / "orderbook.db"
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ORDERBOOK] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "collect_orderbook.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("collect_orderbook")

# ── Constants ─────────────────────────────────────────────────────────────
TIMEOUT = 180  # 3 minutes per HTTP request
MAX_RETRIES = 5  # retry count on failure
RETRY_DELAY = 60  # seconds between retries
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}


# ── Database setup ────────────────────────────────────────────────────────
def init_orderbook_db() -> None:
    """Create orderbook.db and orderbook_snapshots table if not exists."""
    conn = sqlite3.connect(str(ORDERBOOK_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            city TEXT,
            metric TEXT,
            target_date TEXT,
            bid_depth_usd REAL DEFAULT 0.0,
            ask_depth_usd REAL DEFAULT 0.0,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            num_bid_levels INTEGER DEFAULT 0,
            num_ask_levels INTEGER DEFAULT 0,
            snapshot_time TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_market_time
        ON orderbook_snapshots (market_id, snapshot_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_city
        ON orderbook_snapshots (city, snapshot_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_token
        ON orderbook_snapshots (token_id)
    """)
    conn.commit()
    conn.close()
    logger.info("orderbook.db initialized")


def fetch_active_markets_from_gamma() -> list[dict[str, Any]]:
    """Fetch active weather markets from Gamma API with clobTokenIds.

    Uses Gamma API directly (not bot.db) to get full market data including
    clobTokenIds needed for CLOB orderbook queries.
    """
    import urllib.request
    import urllib.error
    import json

    all_markets = []
    offset = 0
    page_size = 100
    max_pages = 20  # safety limit

    for page in range(max_pages):
        url = (
            f"{GAMMA_BASE}/markets?limit={page_size}&offset={offset}"
            f"&closed=false&active=true&order=volume&ascending=false"
        )
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    batch = json.loads(resp.read().decode("utf-8"))
                if not batch:
                    return all_markets
                all_markets.extend(batch)
                logger.info("Gamma page %d: +%d markets (total %d)", page + 1, len(batch), len(all_markets))
                if len(batch) < page_size:
                    return all_markets
                offset += page_size
                time.sleep(0.25)
                break
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                logger.warning("Gamma fetch attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error("All %d attempts failed for Gamma page %d", MAX_RETRIES, page)
                    return all_markets

    return all_markets


def extract_yes_token_id(market: dict) -> str | None:
    """Extract YES token ID from Gamma market data (clobTokenIds)."""
    clob_ids = market.get("clobTokenIds")
    if not clob_ids:
        return None
    if isinstance(clob_ids, str):
        import json

        try:
            clob_ids = json.loads(clob_ids)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(clob_ids, list) and len(clob_ids) >= 1:
        # tokens[0] = YES, tokens[1] = NO (Polymarket convention)
        return str(clob_ids[0])
    return None


def fetch_orderbook(token_id: str | None) -> dict[str, Any] | None:
    """Fetch live orderbook from CLOB API with retry logic."""
    import urllib.request
    import urllib.error
    import json

    url = f"{CLOB_BASE}/book?token_id={token_id}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning(
                "CLOB attempt %d/%d failed for token %s: %s",
                attempt,
                MAX_RETRIES,
                token_id[:16],
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    logger.error("All %d CLOB attempts failed for token %s", MAX_RETRIES, token_id[:16])
    return None


def parse_orderbook(raw: dict[str, Any]) -> dict[str, float | None]:
    """Parse raw CLOB orderbook response into depth metrics."""
    bids = raw.get("bids", [])
    asks = raw.get("asks", [])

    bid_depth = 0.0
    best_bid = None
    for level in bids:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if 0 < price < 1 and size > 0:
                bid_depth += price * size
                if best_bid is None or price > best_bid:
                    best_bid = price
        except (ValueError, TypeError):
            continue

    ask_depth = 0.0
    best_ask = None
    for level in asks:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if 0 < price < 1 and size > 0:
                ask_depth += price * size
                if best_ask is None or price < best_ask:
                    best_ask = price
        except (ValueError, TypeError):
            continue

    spread = None
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid

    return {
        "bid_depth_usd": round(bid_depth, 2),
        "ask_depth_usd": round(ask_depth, 2),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 4) if spread is not None else None,
        "num_bid_levels": len(bids),
        "num_ask_levels": len(asks),
    }


def save_snapshot(
    conn: sqlite3.Connection,
    market_id: str,
    token_id: str,
    city: str | None,
    metric: str | None,
    target_date: str | None,
    metrics: dict[str, float | None],
    snapshot_time: str,
) -> None:
    """Insert orderbook snapshot into orderbook.db."""
    conn.execute(
        """
        INSERT INTO orderbook_snapshots
            (market_id, token_id, city, metric, target_date,
             bid_depth_usd, ask_depth_usd, best_bid, best_ask, spread,
             num_bid_levels, num_ask_levels, snapshot_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            market_id,
            token_id,
            city,
            metric,
            target_date,
            metrics["bid_depth_usd"],
            metrics["ask_depth_usd"],
            metrics["best_bid"],
            metrics["best_ask"],
            metrics["spread"],
            metrics["num_bid_levels"],
            metrics["num_ask_levels"],
            snapshot_time,
        ),
    )


def extract_city_from_question(question: str) -> str | None:
    """Extract city name from market question text."""
    import re

    # Common patterns: "Will the temperature in CITY..." or "CITY temperature..."
    match = re.search(r"(?:in|at|for)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", question)
    if match:
        return match.group(1)
    return None


def collect_once() -> int:
    """Run one collection cycle. Returns number of markets processed."""
    markets = fetch_active_markets_from_gamma()
    if not markets:
        logger.info("No active markets found from Gamma")
        return 0

    # Filter to weather markets only (temperature-related)
    weather_markets = []
    weather_keywords = ["temperature", "°", "fahrenheit", "celsius", "high", "low", "hot", "cold", "warm"]
    for m in markets:
        question = m.get("question", "").lower()
        if any(kw in question for kw in weather_keywords):
            token_id = extract_yes_token_id(m)
            if token_id:
                city = extract_city_from_question(m.get("question", ""))
                weather_markets.append(
                    {
                        "id": m.get("id", ""),
                        "token_id": token_id,
                        "city": city,
                        "question": m.get("question", ""),
                    }
                )

    if not weather_markets:
        logger.info("No weather markets with token IDs found")
        return 0

    logger.info("Found %d weather markets with token IDs, fetching orderbooks...", len(weather_markets))
    snapshot_time = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(ORDERBOOK_DB))
    processed = 0
    errors = 0

    for market in weather_markets:
        market_id = market["id"]
        token_id = market["token_id"]
        city = market.get("city")

        raw = fetch_orderbook(token_id)
        if raw is None:
            errors += 1
            continue

        metrics = parse_orderbook(raw)
        save_snapshot(conn, market_id, token_id, city, None, None, metrics, snapshot_time)
        processed += 1

        if processed <= 10 or processed % 50 == 0:
            logger.info(
                "  [%d/%d] %s: bid=$%.1f ask=$%.1f spread=%.4f",
                processed,
                len(weather_markets),
                city or market_id[:12],
                metrics["bid_depth_usd"],
                metrics["ask_depth_usd"],
                metrics["spread"] or 0,
            )

        # Small delay to be nice to the API
        time.sleep(0.15)

    conn.commit()
    conn.close()
    logger.info("Collection complete: %d/%d markets processed, %d errors", processed, len(weather_markets), errors)
    return processed


def main() -> None:
    """Main entry point."""
    logger.info("=== Orderbook collection started ===")
    init_orderbook_db()
    count = collect_once()
    logger.info("=== Done: %d markets ===", count)


if __name__ == "__main__":
    main()
