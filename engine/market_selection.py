"""Pure helpers for the requested YES-price market selection policy."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone


def market_group_key(market) -> tuple[str, str, str]:
    """Return the stable `(city, date, metric)` strategy group key."""
    target = getattr(market, "target_date", None)
    target_date = target.date().isoformat() if target else ""
    return (
        str(getattr(market, "city", "") or "").strip().casefold(),
        target_date,
        str(getattr(market, "metric", "") or "").strip().casefold(),
    )


def select_highest_yes_candidates(markets, min_entry_price: float = 0.10, max_entry_price: float = 0.95) -> list:
    """Select all tied maximum-YES candidates in each strategy group.

    The max-price rule is applied before the strict max-entry-price gate so a
    group whose best market is too expensive does not fall back to a cheaper
    market in the same group.
    """
    groups = defaultdict(list)
    for market in markets:
        key = market_group_key(market)
        yes_price = getattr(market, "yes_price", None)
        if key[0] and key[1] and key[2] and yes_price is not None:
            groups[key].append(market)

    selected: list = []
    for candidates in groups.values():
        best = max(float(m.yes_price) for m in candidates)
        if best < min_entry_price or best >= max_entry_price:
            continue
        selected.extend(m for m in candidates if float(m.yes_price) == best)
    return selected


def passes_time_gate(target_date, now: datetime | None = None, gate_hour_utc: int = 13) -> bool:
    """Allow 2+ day-ahead markets only from 13:00 UTC onward."""
    if target_date is None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    target = target_date
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    days_ahead = (target.date() - now.date()).days
    return days_ahead < 2 or now.hour >= int(gate_hour_utc)
