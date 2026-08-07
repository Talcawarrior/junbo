"""Tests for bot_loop pure helpers and stale-bet cleanup.

Covers the decision helpers that drive the scan loop without needing a live
bot: target-date tracking, midnight window, scan-interval selection, and the
stale bet cleanup path against the temp DB.
"""

from datetime import date, datetime, timezone

from bot_loop import (
    _cleanup_stale_bets,
    _get_scan_interval,
    _is_midnight_window,
    _next_two_day_target,
)

UTC = timezone.utc


def _utc(y, m, d, h=12, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC).replace(tzinfo=None)


# ── _next_two_day_target ─────────────────────────────────────────────────────


def test_next_two_day_target_no_open_dates():
    assert _next_two_day_target(None, set()) == (None, False)
    assert _next_two_day_target(date(2026, 8, 7), set()) == (None, False)


def test_next_two_day_target_first_date_triggers():
    result = _next_two_day_target(None, {date(2026, 8, 8)})
    assert result == (date(2026, 8, 8), True)


def test_next_two_day_target_new_date_triggers():
    result = _next_two_day_target(date(2026, 8, 8), {date(2026, 8, 9), date(2026, 8, 10)})
    assert result == (date(2026, 8, 10), True)


def test_next_two_day_target_same_date_no_retrigger():
    result = _next_two_day_target(date(2026, 8, 8), {date(2026, 8, 8), date(2026, 8, 7)})
    assert result == (date(2026, 8, 8), False)


def test_next_two_day_target_older_date_updates_without_trigger():
    result = _next_two_day_target(date(2026, 8, 10), {date(2026, 8, 8), date(2026, 8, 9)})
    assert result == (date(2026, 8, 9), False)


# ── _is_midnight_window ──────────────────────────────────────────────────────


def _mock_bot_config(monkeypatch):
    from config import settings

    fake = type("C", (), {"midnight_scan_window": 60, "midnight_scan_interval": 60})()
    monkeypatch.setattr(settings.bot_config, "midnight_scan_window", 60)
    monkeypatch.setattr(settings.bot_config, "midnight_scan_interval", 60)
    return fake


def test_is_midnight_window_true_early(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert _is_midnight_window(datetime(2026, 8, 7, 0, 15))


def test_is_midnight_window_false_after_window(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert not _is_midnight_window(datetime(2026, 8, 7, 1, 15))


def test_is_midnight_window_false_non_midnight_hour(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert not _is_midnight_window(datetime(2026, 8, 7, 13, 0))


# ── _get_scan_interval ───────────────────────────────────────────────────────


def test_get_scan_interval_fast_mode(monkeypatch):
    now = _utc(2026, 8, 7, 12, 0)
    fast_until = _utc(2026, 8, 7, 12, 1)  # still in fast window (1 min later)
    from bot_loop import _FAST_SCAN_INTERVAL

    assert _get_scan_interval(now, fast_until) == _FAST_SCAN_INTERVAL


def test_get_scan_interval_normal_mode(monkeypatch):
    now = _utc(2026, 8, 7, 12, 0)
    from bot_loop import _NORMAL_SCAN_INTERVAL

    _mock_bot_config(monkeypatch)
    monkeypatch.setattr("utils.model_run_detector.get_model_run_fast_interval", lambda now: None)
    assert _get_scan_interval(now, None) == _NORMAL_SCAN_INTERVAL


def test_get_scan_interval_model_run_override(monkeypatch):
    now = _utc(2026, 8, 7, 12, 0)
    _mock_bot_config(monkeypatch)
    monkeypatch.setattr("utils.model_run_detector.get_model_run_fast_interval", lambda now: 42)
    assert _get_scan_interval(now, None) == 42


# ── _cleanup_stale_bets ──────────────────────────────────────────────────────


def _make_open_bet(session, market_id, placed_at, amount=10.0):
    from database.models import Bet

    session.add(
        Bet(
            market_id=market_id,
            side="YES",
            amount=amount,
            stake_amount=amount,
            stake=amount,
            price=0.5,
            entry_price=0.5,
            shares=amount / 0.5,
            status="open",
            placed_at=placed_at,
        )
    )


def test_cleanup_stale_bets_cancels_only_stale(market_factory):
    from database.db import get_session
    from database.models import Bet, Portfolio

    with get_session() as session:
        existing = session.query(Portfolio).filter(Portfolio.id == 1).first()
        if existing is None:
            session.add(
                Portfolio(
                    id=1,
                    cash_balance=1000.0,
                    initial_value=1000.0,
                    current_value=1000.0,
                    total_value=1000.0,
                )
            )
            session.commit()

    # 3 gun once acilmis, marketi yok → iptal edilmeli
    with get_session() as session:
        _make_open_bet(session, "ghost-market", datetime(2026, 8, 1, 12, 0))
        session.commit()

    # marketi var ama hedef tarihi cok eski → iptal edilmeli
    old_mkt = market_factory(target_date=datetime(2026, 7, 1, 12, 0))
    with get_session() as session:
        _make_open_bet(session, old_mkt, datetime(2026, 8, 1, 12, 0))
        session.commit()

    # yeni bet (bugun) → dokunulmamali
    fresh_mkt = market_factory(target_date=datetime(2026, 8, 8, 12, 0))
    with get_session() as session:
        _make_open_bet(session, fresh_mkt, datetime(2026, 8, 7, 12, 0))
        session.commit()

    _cleanup_stale_bets()

    with get_session() as session:
        cancelled = session.query(Bet).filter(Bet.status == "cancelled").all()
        cancelled_markets = {b.market_id for b in cancelled}
        assert "ghost-market" in cancelled_markets
        assert old_mkt in cancelled_markets
        fresh = session.query(Bet).filter(Bet.market_id == fresh_mkt).first()
        assert fresh is not None
        assert fresh.status == "open"


def test_cleanup_stale_bets_no_bets_no_crash():
    from database.db import get_session
    from database.models import Bet

    with get_session() as session:
        session.query(Bet).delete()
        session.commit()
    _cleanup_stale_bets()  # should not raise
