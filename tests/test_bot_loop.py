"""Tests for bot_loop pure helpers.

Covers the decision helpers that drive the scan loop without needing a live
bot: target-date tracking, midnight window, scan-interval selection.
"""

from datetime import date, datetime, timezone

from bot_loop import (
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

    fake = type("C", (), {"midnight_scan_window": 13, "midnight_scan_interval": 1})()
    monkeypatch.setattr(settings.bot_config, "midnight_scan_window", 13)
    monkeypatch.setattr(settings.bot_config, "midnight_scan_interval", 1)
    return fake


def test_is_midnight_window_true_early(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert _is_midnight_window(datetime(2026, 8, 7, 0, 15))


def test_is_midnight_window_true_until_13(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert _is_midnight_window(datetime(2026, 8, 7, 12, 59))


def test_is_midnight_window_false_after_window(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert not _is_midnight_window(datetime(2026, 8, 7, 13, 1))


def test_is_midnight_window_false_non_midnight_hour(monkeypatch):
    _mock_bot_config(monkeypatch)
    assert not _is_midnight_window(datetime(2026, 8, 7, 14, 0))


# ── _get_scan_interval ───────────────────────────────────────────────────────


def test_get_scan_interval_fast_mode(monkeypatch):
    now = _utc(2026, 8, 7, 12, 0)
    fast_until = _utc(2026, 8, 7, 12, 1)  # still in fast window (1 min later)
    from bot_loop import _FAST_SCAN_INTERVAL

    assert _get_scan_interval(now, fast_until) == _FAST_SCAN_INTERVAL


def test_get_scan_interval_normal_mode(monkeypatch):
    # 13:00 sonrasi pencere disi -> normal 5 dk
    now = _utc(2026, 8, 7, 14, 0)
    from bot_loop import _NORMAL_SCAN_INTERVAL

    _mock_bot_config(monkeypatch)
    monkeypatch.setattr("utils.model_run_detector.get_model_run_fast_interval", lambda now: None)
    assert _get_scan_interval(now, None) == _NORMAL_SCAN_INTERVAL


def test_get_scan_interval_midnight_fast(monkeypatch):
    # 12:00 pencere ici (0-13) -> hizli aralik (1 sn)
    now = _utc(2026, 8, 7, 12, 0)

    _mock_bot_config(monkeypatch)
    monkeypatch.setattr("utils.model_run_detector.get_model_run_fast_interval", lambda now: None)
    assert _get_scan_interval(now, None) == 1


def test_get_scan_interval_model_run_override(monkeypatch):
    now = _utc(2026, 8, 7, 12, 0)
    _mock_bot_config(monkeypatch)
    monkeypatch.setattr("utils.model_run_detector.get_model_run_fast_interval", lambda now: 42)
    assert _get_scan_interval(now, None) == 42


# ── _probe_new_target_date ────────────────────────────────────────────────────


def test_probe_new_target_date_finds_new_day(monkeypatch):
    """Probe: Gamma'da DB'deki max tarihten ileri bir tarih gorurse True doner."""
    import bot_loop

    from datetime import date

    called = {}

    class _FakeClient:
        def fetch_one_blocking(self, url, params, host):
            called["url"] = url
            called["params"] = params
            return {
                "events": [
                    {
                        "title": "Will the highest temperature in London be 30C on August 13?",
                        "end_date_iso": "2026-08-13T20:00:00Z",
                    }
                ]
            }

    monkeypatch.setattr("scrapers.async_client.AsyncHttpClient", lambda: _FakeClient())
    new_date, trigger = bot_loop._probe_new_target_date(date(2026, 8, 12))
    assert trigger is True
    assert new_date == date(2026, 8, 13)
    assert "public-search" in called["url"]
    assert called["params"]["limit_per_type"] == 5


def test_probe_new_target_date_no_new_day(monkeypatch):
    """Probe: DB'deki max tarihten ileri tarih yoksa False doner."""
    import bot_loop

    from datetime import date

    class _FakeClient:
        def fetch_one_blocking(self, url, params, host):
            return {
                "events": [{"title": "Will London exceed 30C on August 12?", "end_date_iso": "2026-08-12T20:00:00Z"}]
            }

    monkeypatch.setattr("scrapers.async_client.AsyncHttpClient", lambda: _FakeClient())
    new_date, trigger = bot_loop._probe_new_target_date(date(2026, 8, 12))
    assert trigger is False
    assert new_date is None


def test_probe_new_target_date_api_failure(monkeypatch):
    """Probe: API hatasi loop'u oldurmez, False doner."""
    import bot_loop

    from datetime import date

    class _FakeClient:
        def fetch_one_blocking(self, url, params, host):
            raise RuntimeError("network down")

    monkeypatch.setattr("scrapers.async_client.AsyncHttpClient", lambda: _FakeClient())
    new_date, trigger = bot_loop._probe_new_target_date(date(2026, 8, 12))
    assert trigger is False
    assert new_date is None


def test_get_open_target_dates_excludes_past(monkeypatch):
    """2026-08-12 bugfix: gecmis gun (bugun oncesi) marketleri 'yeni tarih'
    sayilmaz — +['2026-08-10'] yanlis pozitif FAST mode tetikliyordu."""
    from bot_loop import _get_open_target_dates
    from database.db import get_session
    from database.models import WeatherMarket
    from datetime import timedelta

    today = (datetime.now(timezone.utc) + timedelta(hours=0)).date()
    past_day = today - timedelta(days=1)
    future_day = today + timedelta(days=2)

    with get_session() as s:
        s.query(WeatherMarket).delete()
        # gecmis + bugun + gelecek market
        for i, d in enumerate([past_day, today, future_day]):
            s.add(
                WeatherMarket(
                    id=f"past-test-{i}",
                    question="T?",
                    city="Testville",
                    city_code="AAA",
                    metric="temperature_max",
                    threshold=30.0,
                    target_date=datetime(d.year, d.month, d.day, 12, 0),
                    status="open",
                    yes_price=0.05,
                    no_price=0.95,
                )
            )
        s.commit()
        dates = _get_open_target_dates()
    assert past_day not in dates, "gecmis gun dahil edilmemeli (settlement pending marketler)"
    assert today in dates and future_day in dates
