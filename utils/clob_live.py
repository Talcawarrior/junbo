"""Live CLOB price verification before opening a bet.

The Gamma API's ``outcomePrices`` (persisted as ``WeatherMarket.yes_price``)
can lag the executable orderbook for minutes or hours (observed 2026-08-10:
Beijing 32C YES was 0.18 in Gamma/DB while the CLOB book already quoted
~0.98). Opening a bet on the stale DB price produces paper fills that never
existed on the real market.

This module pulls the live CLOB book for a market's YES token so the bet
placer can refuse stale-price openings and use the executable price instead.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("UTILS_CLOB_LIVE")

# Relative divergence (db_price vs live CLOB) beyond which the DB price is
# treated as stale and the bet is refused. 15% is far outside any reasonable
# spread/volatility on these markets, yet far below the 0.18 vs 0.98 case
# (444%) that motivated this guard.
_STALE_DIVERGENCE_PCT = 0.15


def extract_yes_token_id(raw_data: str | None) -> str | None:
    """Pull the YES ``clobTokenIds[0]`` from a market's stored ``raw_data``.

    Returns ``None`` when the token cannot be found (e.g. legacy rows without
    the field) so callers can fall back to the DB price silently.
    """
    if not raw_data:
        return None
    try:
        raw = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    token_ids = raw.get("clobTokenIds")
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except (json.JSONDecodeError, TypeError):
            token_ids = None
    if isinstance(token_ids, list) and token_ids:
        return str(token_ids[0])
    return None


def _fetch_live_yes_quote(token_id: str, timeout: float = 5.0) -> tuple[float | None, float | None]:
    """Return (best_ask, best_bid) for the YES token from the CLOB book.

    ``None`` on any failure (network, no book, no levels) — never raises.
    """
    try:
        from scrapers.clob import CLOBClient

        client = CLOBClient(timeout=timeout)
        book = client.get_orderbook(token_id)
        return book.best_ask, book.best_bid
    except Exception as exc:  # network / parse / HTTP errors
        logger.warning("CLOB live quote failed for token %s: %s", token_id, exc)
        return None, None


def price_is_stale(db_price: float, live_ask: float | None, live_bid: float | None) -> bool:
    """True when the stored DB price diverges wildly from the live CLOB book.

    Uses the ask side (what a YES buyer pays). If the live quote is missing,
    conservatively treat as *not* stale (no evidence) so existing behavior is
    preserved on CLOB outages.
    """
    if live_ask is None or db_price <= 0:
        return False
    reference = live_ask if live_ask > 0 else 0.0
    if reference <= 0:
        return False
    divergence = abs(db_price - reference) / reference
    return divergence > _STALE_DIVERGENCE_PCT


def live_quote_for_market(raw_data: str | None, timeout: float = 5.0) -> tuple[str | None, float | None, float | None]:
    """Convenience: token_id + live (ask, bid) for a market's raw_data."""
    token_id = extract_yes_token_id(raw_data)
    if token_id is None:
        return None, None, None
    ask, bid = _fetch_live_yes_quote(token_id, timeout=timeout)
    return token_id, ask, bid
