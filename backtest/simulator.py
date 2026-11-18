"""Depth-aware, latency-aware orderbook replay primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class OrderBookSnapshot:
    timestamp: datetime
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class SimulatedFill:
    timestamp: datetime
    price: float
    shares: float
    notional: float


@dataclass(frozen=True)
class SimulationResult:
    fills: tuple[SimulatedFill, ...]
    requested_usd: float
    filled_usd: float
    unfilled_usd: float
    vwap: float | None
    fee_usd: float
    gas_usd: float
    mark_price: float | None


def _valid_levels(levels: Iterable[tuple[float, float]], *, reverse: bool = False) -> list[tuple[float, float]]:
    clean = [(float(price), float(size)) for price, size in levels if 0 < float(price) < 1 and float(size) > 0]
    return sorted(clean, key=lambda item: item[0], reverse=reverse)


def simulate_order(
    requested_usd: float,
    submitted_at: datetime,
    snapshots: Iterable[OrderBookSnapshot],
    *,
    fee_rate: float = 0.05,
    gas_cost_usd: float = 0.10,
    latency_seconds: float = 0.0,
) -> SimulationResult:
    """Simulate a YES taker order against the first eligible live book.

    The order consumes asks in ascending price order.  It may be partially
    filled; no synthetic fill is created when depth is unavailable.
    """
    requested_usd = max(0.0, float(requested_usd))
    eligible_at = submitted_at + timedelta(seconds=max(0.0, latency_seconds))
    snapshot = next(
        (item for item in sorted(snapshots, key=lambda row: row.timestamp) if item.timestamp >= eligible_at), None
    )
    if requested_usd <= 0 or snapshot is None:
        return SimulationResult((), requested_usd, 0.0, requested_usd, None, 0.0, 0.0, None)

    remaining = requested_usd
    fills: list[SimulatedFill] = []
    asks = _valid_levels(snapshot.asks)
    for price, size in asks:
        take_usd = min(remaining, price * size)
        shares = take_usd / price
        fills.append(SimulatedFill(snapshot.timestamp, price, shares, take_usd))
        remaining -= take_usd
        if remaining <= 1e-9:
            break

    filled_usd = requested_usd - remaining
    total_shares = sum(fill.shares for fill in fills)
    vwap = filled_usd / total_shares if total_shares else None
    fee_usd = filled_usd * max(0.0, fee_rate) * (1.0 - vwap) if vwap is not None else 0.0
    bids = _valid_levels(snapshot.bids, reverse=True)
    mark_price = bids[0][0] if bids else None
    return SimulationResult(
        tuple(fills),
        requested_usd,
        filled_usd,
        remaining,
        vwap,
        fee_usd,
        gas_cost_usd if filled_usd > 0 else 0.0,
        mark_price,
    )
