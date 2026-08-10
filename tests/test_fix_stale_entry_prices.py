"""Tests for the stale-entry-price fixer (scripts/fix_stale_entry_prices.py).

2026-08-10 bug: Gamma outcomePrices lagged the executable CLOB book, so
open bets recorded entry prices that never existed on the real market
(e.g. Beijing 32C 0.18 in DB vs ~0.98 on the book). The fixer rewrites
entry_price/shares/fee/pnl to the real CLOB price at placed_at.

These tests cover the pure helpers so the correction math stays correct.
"""

from __future__ import annotations

import json

import pytest

from scripts.fix_stale_entry_prices import (
    STALE_DIVERGENCE_PCT,
    _token_for,
)

YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"
NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# _token_for
# ---------------------------------------------------------------------------


def test_token_for_parses_json_string_list():
    raw = json.dumps({"clobTokenIds": f'["{YES_TOKEN}", "{NO_TOKEN}"]'})
    assert _token_for(raw) == YES_TOKEN


def test_token_for_parses_native_list():
    raw = json.dumps({"clobTokenIds": [YES_TOKEN, NO_TOKEN]})
    assert _token_for(raw) == YES_TOKEN


def test_token_for_missing_returns_none():
    assert _token_for(None) is None
    assert _token_for("not json") is None
    assert _token_for(json.dumps({"outcomePrices": "[0.5, 0.5]"})) is None


# ---------------------------------------------------------------------------
# Correction math mirrors the live bot formulas
# ---------------------------------------------------------------------------


def test_shares_and_fee_recomputed_from_live_price():
    """entry 0.18 at $2 stake -> live 0.98: shares = 2/0.98, fee uses live price."""
    from utils.formulas import bet_shares, polymarket_fee_from_stake

    stake = 2.0
    live = 0.98
    shares = bet_shares(stake, live)
    fee = polymarket_fee_from_stake(stake, live, fee_rate=0.05)
    assert shares == pytest.approx(2.0 / 0.98, rel=1e-6)
    assert fee == pytest.approx(stake * 0.05 * ((1.0 - live) ** 0.5), rel=1e-6)


def test_unrealized_pnl_flat_when_current_equals_entry():
    """After fix current==entry -> unrealized pnl is just the (negative) fee."""
    from utils.formulas import unrealized_pnl

    shares = 2.0 / 0.98
    fee = 2.0 * 0.05 * ((1.0 - 0.98) ** 0.5)
    unreal = unrealized_pnl(shares, 0.98, 0.98) - fee
    assert unreal == pytest.approx(-fee, rel=1e-6)


def test_divergence_threshold_constant():
    """The fixer uses the same 15% staleness threshold as the live guard."""
    from utils.clob_live import _STALE_DIVERGENCE_PCT as GUARD

    assert STALE_DIVERGENCE_PCT == GUARD


# ---------------------------------------------------------------------------
# The correction only touches bets that diverge > 15%
# ---------------------------------------------------------------------------


def _divergence(entry, live):
    return abs(live - entry) / live


def test_stale_classification_16_percent():
    assert _divergence(0.50, 0.60) > STALE_DIVERGENCE_PCT


def test_fresh_classification_5_percent():
    assert _divergence(0.50, 0.525) <= STALE_DIVERGENCE_PCT


def test_beijing_case_classified_stale():
    # The exact 2026-08-10 case: 0.18 in DB, ~0.98 on the book.
    assert _divergence(0.18, 0.98) > STALE_DIVERGENCE_PCT
