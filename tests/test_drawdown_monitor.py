"""Drawdown high-water-mark + progressive risk tier tests.

The monitor must de-risk automatically as the bankroll falls from its peak and
halt trading past the critical drawdown. It must also force yellow on a cold
streak of high-confidence misses (model degradation signal).
"""

from utils.drawdown_monitor import DrawdownMonitor, YELLOW, RED, CRITICAL, GREEN


def test_starts_green_at_peak():
    m = DrawdownMonitor(peak=1000.0)
    assert m.tier() is GREEN
    assert m.halt() is False
    assert m.drawdown() == 0.0


def test_yellow_at_10pct_drawdown():
    m = DrawdownMonitor(peak=1000.0)
    m.update(900.0)  # -10%
    assert m.tier() is YELLOW
    assert m.alpha_multiplier() == 0.5
    assert m.halt() is False


def test_red_at_20pct_drawdown():
    m = DrawdownMonitor(peak=1000.0)
    m.update(800.0)  # -20%
    assert m.tier() is RED
    assert m.alpha_multiplier() == 0.0


def test_critical_halts_at_30pct():
    m = DrawdownMonitor(peak=1000.0)
    m.update(690.0)  # -31%
    assert m.tier() is CRITICAL
    assert m.halt() is True


def test_high_water_mark_recovers_without_false_alarm():
    m = DrawdownMonitor(peak=1000.0)
    m.update(700.0)  # -30% -> critical
    assert m.tier() is CRITICAL
    m.update(1050.0)  # new peak
    assert m.drawdown() == 0.0
    assert m.tier() is GREEN


def test_cold_streak_forces_yellow():
    m = DrawdownMonitor(peak=1000.0)
    assert m.tier() is GREEN
    for _ in range(5):  # 5 consecutive high-confidence misses
        m.record_outcome(confidence=0.80, won=False)
    assert m.tier() is YELLOW


def test_low_confidence_miss_does_not_count_as_cold_streak():
    m = DrawdownMonitor(peak=1000.0)
    for _ in range(5):
        m.record_outcome(confidence=0.55, won=False)  # expected noise
    assert m.tier() is GREEN


def test_win_resets_cold_streak():
    m = DrawdownMonitor(peak=1000.0)
    m.record_outcome(confidence=0.80, won=False)
    m.record_outcome(confidence=0.80, won=False)
    assert m._cold_streak == 2
    m.record_outcome(confidence=0.80, won=True)  # resets
    assert m._cold_streak == 0
    m.record_outcome(confidence=0.80, won=False)
    m.record_outcome(confidence=0.80, won=False)
    assert m._cold_streak == 2  # rebuilds only after the win
