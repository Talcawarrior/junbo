"""Small, dependency-light client for the official Polymarket CLOB API.

Gamma is useful for discovery, but it is not an executable quote.  This
module keeps execution pricing on the CLOB book and deliberately contains no
credential handling, so it is safe to use from dry-run code paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger("SCRAPER_CLOB")


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    timestamp: str | None = None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def ask_vwap(self, stake_usd: float) -> tuple[float | None, float]:
        """Return (VWAP, filled USD) while walking the real ask depth."""
        if stake_usd <= 0:
            return None, 0.0
        remaining = float(stake_usd)
        notional = 0.0
        shares = 0.0
        for level in self.asks:
            if level.price <= 0 or level.size <= 0:
                continue
            level_notional = level.price * level.size
            take = min(remaining, level_notional)
            take_shares = take / level.price
            notional += take
            shares += take_shares
            remaining -= take
            if remaining <= 1e-9:
                break
        return (notional / shares if shares else None), notional


def _levels(raw: Any, *, reverse: bool) -> tuple[BookLevel, ...]:
    parsed: list[BookLevel] = []
    for level in raw or []:
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            parsed.append(BookLevel(price, size))
    return tuple(sorted(parsed, key=lambda item: item.price, reverse=reverse))


class CLOBClient:
    """Public CLOB market-data client; no auth is needed for `/book`."""

    def __init__(self, base_url: str = "https://clob.polymarket.com", session=None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def get_orderbook(self, token_id: str) -> OrderBook:
        response = self.session.get(f"{self.base_url}/book", params={"token_id": token_id}, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        return OrderBook(
            token_id=str(payload.get("asset_id") or token_id),
            bids=_levels(payload.get("bids"), reverse=True),
            asks=_levels(payload.get("asks"), reverse=False),
            timestamp=str(payload.get("timestamp")) if payload.get("timestamp") is not None else None,
        )
