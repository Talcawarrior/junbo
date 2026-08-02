from datetime import datetime, timezone
from types import SimpleNamespace

from engine.market_selection import passes_time_gate, select_highest_yes_candidates


def market(city, target_date, metric, yes_price):
    return SimpleNamespace(city=city, target_date=target_date, metric=metric, yes_price=yes_price)


def test_selects_highest_yes_price_per_group_and_keeps_ties():
    target = datetime(2026, 8, 5, tzinfo=timezone.utc)
    low = market(" Ankara ", target, "temperature_max", 0.40)
    high = market("ankara", target, "temperature_max", 0.55)
    tie = market("ANKARA", target, "TEMPERATURE_MAX", 0.55)
    other = market("Ankara", target, "temperature_min", 0.30)

    selected = select_highest_yes_candidates([low, high, tie, other])

    assert selected == [high, tie, other]


def test_max_entry_price_is_strict_and_does_not_fallback_to_cheaper_market():
    target = datetime(2026, 8, 5, tzinfo=timezone.utc)
    best_too_expensive = market("Ankara", target, "temperature_max", 0.99)
    cheaper = market("Ankara", target, "temperature_max", 0.80)

    assert select_highest_yes_candidates([best_too_expensive, cheaper]) == []
    assert len(select_highest_yes_candidates([market("Ankara", target, "temperature_max", 0.989)])) == 1


def test_time_gate_blocks_two_days_ahead_before_13_utc_and_allows_at_13():
    target = datetime(2026, 8, 5, tzinfo=timezone.utc)
    before = datetime(2026, 8, 3, 12, 59, tzinfo=timezone.utc)
    at_gate = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)

    assert passes_time_gate(target, before) is False
    assert passes_time_gate(target, at_gate) is True


def test_time_gate_allows_today_and_tomorrow():
    now = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    assert passes_time_gate(datetime(2026, 8, 3, tzinfo=timezone.utc), now)
    assert passes_time_gate(datetime(2026, 8, 4, tzinfo=timezone.utc), now)
