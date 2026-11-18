"""verify_entry_price / live_exit_price — FAIL-CLOSED fiyat dogrulamasi (2026-11-18).

Olay: 2026-11-17 21:07 UTC restart sonrasi metar_peak Londra 14C betini DB'deki
bayat 0.52 fiyatindan acti; gercek piyasa bid 0.999 / ask 1.00 idi. Bu testler
o durumun artik bet acmadigini garanti eder.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import utils.clob_live as cl

pytestmark = pytest.mark.real_price_guard

_RAW = json.dumps({"clobTokenIds": json.dumps(["111", "222"])})


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _market(age_min=3.0, raw=_RAW, yes_price=0.30):
    return SimpleNamespace(last_updated=_now() - timedelta(minutes=age_min), raw_data=raw, yes_price=yes_price)


@pytest.fixture
def quote(monkeypatch):
    def _set(ask, bid):
        monkeypatch.setattr(cl, "_fetch_live_yes_quote", lambda token_id, timeout=5.0: (ask, bid))

    return _set


def test_fresh_price_fills_at_live_ask(quote):
    quote(0.31, 0.29)
    assert cl.verify_entry_price(_market(), 0.30) == (True, 0.31, "ok")


def test_nov17_incident_stale_db_and_resolved_market_refused(quote):
    # DB 0.52 (6 saat once), piyasa cozulmus: ask 1.0, bid 0.999
    quote(1.0, 0.999)
    ok, fill, why = cl.verify_entry_price(_market(age_min=360), 0.52)
    assert ok is False and fill is None
    assert "bayat" in why


def test_resolved_market_refused_even_if_db_fresh(quote):
    quote(1.0, 0.999)
    ok, _fill, why = cl.verify_entry_price(_market(age_min=1), 0.52)
    assert ok is False
    assert "gecersiz" in why


def test_clob_unreachable_blocks_bet(quote):
    quote(None, None)
    ok, _fill, why = cl.verify_entry_price(_market(), 0.30)
    assert ok is False
    assert "fail-closed" in why


def test_divergence_over_15pct_refused(quote):
    quote(0.40, 0.38)
    ok, _fill, _why = cl.verify_entry_price(_market(), 0.30)
    assert ok is False


def test_missing_token_or_timestamp_refused(quote):
    quote(0.30, 0.29)
    assert cl.verify_entry_price(_market(raw=None), 0.30)[0] is False
    m = _market()
    m.last_updated = None
    assert cl.verify_entry_price(m, 0.30)[0] is False


def test_live_exit_price_uses_bid(quote):
    quote(0.31, 0.29)
    assert cl.live_exit_price(_market()) == 0.29
    quote(None, None)
    assert cl.live_exit_price(_market()) is None
