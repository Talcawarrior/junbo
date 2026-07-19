"""Slippage estimation for paper/live bet execution.

Provides three slippage models of increasing fidelity:
  1. ``flat``      — single fixed % (legacy, matches karpathy_weekly.py)
  2. ``tiered``    — 3-tier based on entry price (proxy for book depth)
  3. ``orderbook`` — real depth-based slippage from ResolvedMarkets API

The production code paths (Calculator, BetPlacer, Kelly sizing) all
call :func:`estimate_slippage` which dispatches to the active model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config.settings import bot_config

logger = logging.getLogger("UTIL_SLIPPAGE")

# ---------------------------------------------------------------------------
# Default cost constants (mirrors strategy_params.json / karpathy_weekly.py)
# ---------------------------------------------------------------------------
FEE_PCT: float = 0.05  # Polymarket Weather category taker fee rate (5 %)
# Correct formula: fee = C × feeRate × p × (1-p) = stake × feeRate × (1-p)
# See utils/formulas.py → polymarket_fee() for the canonical implementation.
GAS_COST_USD: float = 0.10  # Polygon gas per round-trip


@dataclass(frozen=True)
class SlippageEstimate:
    """Result of a slippage estimation call."""

    slippage_pct: float  # e.g. 0.015 = 1.5 % price impact
    fill_price: float | None  # None = use caller's price * (1+slippage_pct)
    spread_pct: float  # best-bid/ask spread as fraction of mid
    depth_usd: float  # available depth at or near the fill price
    model_used: str  # "flat", "tiered", or "orderbook"


def _tiered_slippage(entry_price: float) -> float:
    """3-tier adaptive slippage based on entry price (liquidity proxy).

    Mirrors the model in ``karpathy_weekly.py`` so backtest ↔ paper are
    consistent.

    * < 0.05  → 3 %  (thin book, penny markets)
    * 0.05–0.10 → 1 %  (moderate)
    * > 0.10  → 0.5 % (deep book)
    """
    if entry_price < 0.05:
        return 0.03
    if entry_price < 0.10:
        return 0.01
    return 0.005


def _vwap_from_asks(asks: list[dict], stake_usd: float, fallback_price: float) -> tuple[float, float]:
    """Walk orderbook ask levels, compute VWAP fill and total depth.

    Returns (fill_vwap, depth_usd).
    """
    cumulative = 0.0
    vwap_num = 0.0
    filled_shares = 0.0
    for level in asks:
        price = float(level.get("price", 0))
        size = float(level.get("size", 0))
        cost = price * size
        if cumulative + cost >= stake_usd:
            needed = stake_usd - cumulative
            shares_needed = needed / price if price > 0 else 0
            vwap_num += price * shares_needed
            filled_shares += shares_needed
            cumulative = stake_usd
            break
        cumulative += cost
        vwap_num += cost
        filled_shares += size
    fill_vwap = vwap_num / filled_shares if filled_shares > 0 else fallback_price
    return fill_vwap, cumulative


def _orderbook_slippage(
    entry_price: float,
    stake_usd: float,
    condition_id: str | None = None,
) -> SlippageEstimate:
    """Fetch live orderbook and estimate realistic fill price.

    Algorithm
    ---------
    1. Call ``ResolvedMarketsClient.get_live_orderbook(condition_id)``.
    2. Walk the ask ladder (if buying YES) or bid ladder (if buying NO)
       cumulatively until ``stake_usd`` worth of shares is consumed.
    3. The VWAP of that slice is the estimated fill price.
    4. ``slippage_pct = (fill_vwap / mid_price) - 1``.
    5. If the orderbook call fails, fall back to tiered model and log
       a warning.

    Returns
    -------
    SlippageEstimate with model_used="orderbook" (or "tiered" on fallback).
    """
    if not condition_id:
        return _tiered_fallback(entry_price, "orderbook: no condition_id")

    try:
        from data_pipeline.resolvedmarkets_ingest import ResolvedMarketsClient

        ob = ResolvedMarketsClient().get_live_orderbook(condition_id)
        if not ob or ("asks" not in ob and "bids" not in ob):
            logger.warning("Orderbook empty for %s, falling back to tiered", condition_id)
            return _tiered_fallback(entry_price, "orderbook: empty_book")

        asks = ob.get("asks", [])
        bids = ob.get("bids", [])
        best_ask = float(asks[0]["price"]) if asks else entry_price * 1.01
        best_bid = float(bids[0]["price"]) if bids else entry_price * 0.99
        mid = (best_ask + best_bid) / 2
        spread_pct = (best_ask - best_bid) / mid if mid > 0 else 0.01

        fill_vwap, depth_usd = _vwap_from_asks(asks, stake_usd, entry_price)
        slippage_pct = max(0.0, (fill_vwap / mid - 1)) if mid > 0 else 0.005

        return SlippageEstimate(
            slippage_pct=round(slippage_pct, 4),
            fill_price=round(fill_vwap, 4),
            spread_pct=round(spread_pct, 4),
            depth_usd=round(depth_usd, 2),
            model_used="orderbook",
        )
    except Exception as exc:
        logger.warning("Orderbook slippage fetch failed, falling back to tiered: %s", exc)
        return _tiered_fallback(entry_price, f"orderbook_error: {exc}")


def _tiered_fallback(entry_price: float, model_used: str) -> SlippageEstimate:
    """Return a tiered-model estimate tagged with the real model name."""
    slip = _tiered_slippage(entry_price)
    return SlippageEstimate(
        slippage_pct=slip,
        fill_price=None,
        spread_pct=0.0,
        depth_usd=0.0,
        model_used=model_used,
    )


def estimate_slippage(
    entry_price: float,
    stake_usd: float = 0.0,
    *,
    model: str | None = None,
    condition_id: str | None = None,
) -> SlippageEstimate:
    """Dispatch to the configured slippage model.

    Parameters
    ----------
    entry_price
        The raw market price for the chosen side (yes_price or no_price).
    stake_usd
        Proposed bet size in USD (needed for orderbook walk-through).
    model
        Override the global config. One of ``"flat"``, ``"tiered"``,
        ``"orderbook"``.  ``None`` = read from config.
    condition_id
        Polymarket condition token ID (needed for orderbook model).
    """
    cfg_model = getattr(bot_config.strategy, "slippage_model", "tiered")
    active_model = model or cfg_model

    if active_model == "orderbook":
        return _orderbook_slippage(entry_price, stake_usd, condition_id)
    if active_model == "flat":
        flat_pct = getattr(bot_config.strategy, "slippage_pct", 0.005)
        return SlippageEstimate(
            slippage_pct=flat_pct,
            fill_price=None,
            spread_pct=0.0,
            depth_usd=0.0,
            model_used="flat",
        )
    # Default: tiered
    return _tiered_fallback(entry_price, "tiered")


def adjust_edge_for_costs(
    raw_edge: float,
    entry_price: float,
    *,
    include_fee: bool = True,
    bet_amount_usd: float | None = None,
) -> float:
    """Subtract fee drag, gas cost, and estimated slippage from raw edge.

    This is called from ``Calculator.analyze_market()`` so that the
    ``should_bet`` decision uses *net* edge after all transaction costs.

    Returns
    -------
    float
        Edge after subtracting slippage, fee drag, and gas cost.
    """
    est = estimate_slippage(entry_price)
    cost = est.slippage_pct
    # Gas cost as edge percentage: $0.10 / bet_amount_usd gives return %.
    # Convert to probability edge units by multiplying by entry_price.
    # Edge = prob - price, so return% = edge/price. Gas return% = gas/price = gas edge.
    # If bet_amount_usd not provided, fall back to $30 (legacy behavior).
    gas_denominator = bet_amount_usd if bet_amount_usd and bet_amount_usd > 0 else 30.0
    gas_edge_pct = (GAS_COST_USD / gas_denominator) * entry_price
    cost += gas_edge_pct
    if include_fee:
        # Polymarket taker fee (correct formula):
        #   fee = C × feeRate × p × (1-p)   [official Polymarket formula]
        #   fee_per_share = feeRate × p × (1-p)
        # Since edge is measured in price/probability units (same as p),
        # the fee drag in edge units = feeRate × p × (1-p).
        if entry_price > 0:
            fee_drag = bot_config.strategy.current_fee_rate * entry_price * (1.0 - entry_price)
        else:
            fee_drag = 0.0
        cost += fee_drag
    return raw_edge - cost


def adjust_kelly_for_slippage(
    kelly_amount: float,
    entry_price: float,
    max_slippage_pct: float = 0.03,
) -> float:
    """Apply a small safety haircut to the Kelly bet size.

    ``adjust_edge_for_costs()`` already subtracts slippage from the edge,
    so we do NOT re-estimate slippage here to avoid double-counting.
    We apply a small fixed haircut (default 1 % of Kelly amount) as a
    safety margin for illiquid fills, without over-penalizing liquid
    markets.

    Parameters
    ----------
    kelly_amount
        Raw Kelly-recommended bet size in USD.
    entry_price
        Market price for the chosen side (unused – kept for signature compat).
    max_slippage_pct
        Not used – kept for signature compat.

    Returns
    -------
    float
        Adjusted bet size (Kelly amount minus 1 % hair cut, floored at $1).
    """
    # Small fixed safety haircut — the real slippage cost is already in edge.
    slip = 0.01
    slippage_cost = kelly_amount * slip
    adjusted = kelly_amount - slippage_cost
    # Floor at minimum bet
    min_bet = float(getattr(bot_config, "MIN_BET_SIZE", 1.0))
    return max(adjusted, min_bet)


def check_orderbook_depth(
    condition_id: str | None,
    side: str,
    fill_price: float,
    stake_usd: float,
    min_depth_usd: float = 0.0,
) -> tuple[bool, float]:
    """Check if the orderbook has enough depth near our fill price.

    Walks the relevant side of the book (asks for YES buy, bids for NO buy)
    and sums depth within ±2 ticks of fill_price.  Returns (ok, depth_usd).

    If ``min_depth_usd <= 0``, the check is disabled and returns (True, 0).
    If the API call fails, returns (True, 0) — graceful degradation.

    Parameters
    ----------
    condition_id
        Polymarket condition token ID (from market.raw_data → tokens).
    side
        "YES" or "NO".
    fill_price
        Our intended fill price.
    stake_usd
        Our bet size in USD (for sizing context).
    min_depth_usd
        Minimum required depth.  0 = disabled.

    Returns
    -------
    (depth_ok, depth_usd)
    """
    if min_depth_usd <= 0 or not condition_id:
        return True, 0.0

    try:
        from data_pipeline.resolvedmarkets_ingest import ResolvedMarketsClient

        client = ResolvedMarketsClient()
        ob = client.get_live_orderbook(condition_id)
        if not ob:
            return True, 0.0

        # Pick the relevant side: buying YES = consuming asks, buying NO = consuming bids
        if side.upper() == "YES":
            levels = ob.get("asks", [])
        else:
            levels = ob.get("bids", [])

        # Sum depth within ±2 ticks (0.02) of fill_price
        depth_usd = 0.0
        for lvl in levels:
            price = float(lvl.get("price", 0))
            size = float(lvl.get("size", 0))
            if abs(price - fill_price) <= 0.02:
                depth_usd += price * size  # price * shares = USD value

        ok = depth_usd >= min_depth_usd
        if not ok:
            logger.warning(
                "Depth filter: %.2f USD < %.2f USD min at price %.4f (side=%s)",
                depth_usd,
                min_depth_usd,
                fill_price,
                side,
            )
        return ok, depth_usd
    except Exception as exc:
        logger.warning("Depth check failed (graceful skip): %s", exc)
        return True, 0.0
