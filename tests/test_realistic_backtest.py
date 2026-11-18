from datetime import datetime, timedelta, timezone

from backtest.simulator import OrderBookSnapshot, simulate_order


def _snap(at, asks=((0.40, 5.0), (0.42, 10.0)), bids=((0.39, 20.0),)):
    return OrderBookSnapshot(at, bids=bids, asks=asks)


def test_latency_and_depth_aware_vwap_with_partial_fill():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = simulate_order(
        6.0,
        start,
        [_snap(start), _snap(start + timedelta(seconds=3))],
        latency_seconds=2,
        gas_cost_usd=0.1,
    )
    assert result.filled_usd == 6.0
    assert result.vwap == (2.0 + 4.0) / (5.0 + 4.0 / 0.42)
    assert result.fills[0].timestamp == start + timedelta(seconds=3)
    assert result.fee_usd > 0
    assert result.gas_usd == 0.1


def test_insufficient_depth_is_not_a_synthetic_full_fill():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = simulate_order(100.0, start, [_snap(start, asks=((0.5, 1.0),))])
    assert result.filled_usd == 0.5
    assert result.unfilled_usd == 99.5


def test_no_snapshot_after_latency_means_no_fill():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = simulate_order(10, start, [_snap(start)], latency_seconds=1)
    assert result.fills == ()
    assert result.gas_usd == 0
