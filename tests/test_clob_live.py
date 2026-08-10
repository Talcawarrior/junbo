"""Tests for the live CLOB price staleness guard (2026-08-10 bug).

The bug: Gamma's stored ``yes_price`` lagged the executable CLOB book
(Beijing 32C was 0.18 in the DB while the book quoted ~0.98), so the bot
opened paper bets at prices that never existed on the real market.

These tests verify:
  1. token extraction from raw_data (clobTokenIds[0] == YES)
  2. the staleness predicate (divergence beyond tolerance -> stale)
  3. the bet_placer refuses stale-price openings and allows fresh ones
"""

from __future__ import annotations

import json
from unittest.mock import patch


from utils.clob_live import (
    extract_yes_token_id,
    live_quote_for_market,
    price_is_stale,
)
from executor.bet_placer import BetPlacer

YES_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"
NO_TOKEN = "2222222222222222222222222222222222222222222222222222222222222222"


def _raw_data(yes: float = 0.5) -> str:
    return json.dumps(
        {
            "clobTokenIds": [YES_TOKEN, NO_TOKEN],
            "outcomePrices": f"[{yes}, {1.0 - yes}]",
        }
    )


# ---------------------------------------------------------------------------
# 1. token extraction
# ---------------------------------------------------------------------------


def test_extract_yes_token_id_parses_string_json():
    raw = json.dumps({"clobTokenIds": f'["{YES_TOKEN}", "{NO_TOKEN}"]'})
    assert extract_yes_token_id(raw) == YES_TOKEN


def test_extract_yes_token_id_parses_list():
    raw = json.dumps({"clobTokenIds": [YES_TOKEN, NO_TOKEN]})
    assert extract_yes_token_id(raw) == YES_TOKEN


def test_extract_yes_token_id_missing_returns_none():
    assert extract_yes_token_id(json.dumps({"outcomePrices": "[0.5, 0.5]"})) is None
    assert extract_yes_token_id(None) is None
    assert extract_yes_token_id("not json") is None


# ---------------------------------------------------------------------------
# 2. staleness predicate
# ---------------------------------------------------------------------------


def test_price_is_stale_large_divergence():
    # DB 0.18 vs live ask 0.98 -> 444% divergence -> stale
    assert price_is_stale(0.18, 0.98, None) is True


def test_price_is_stale_small_divergence():
    # DB 0.50 vs live ask 0.51 -> ~2% -> not stale
    assert price_is_stale(0.50, 0.51, None) is False


def test_price_is_stale_missing_quote_not_stale():
    # No live quote -> no evidence -> not stale (preserve old behavior)
    assert price_is_stale(0.18, None, None) is False


def test_price_is_stale_nonpositive_db_price():
    assert price_is_stale(0.0, 0.5, None) is False


# ---------------------------------------------------------------------------
# 3. live_quote_for_market error handling (never raises)
# ---------------------------------------------------------------------------


def test_live_quote_for_market_returns_none_on_failure():
    with patch("scrapers.clob.CLOBClient.get_orderbook", side_effect=RuntimeError("boom")):
        tok, ask, bid = live_quote_for_market(_raw_data())
    assert tok == YES_TOKEN
    assert (ask, bid) == (None, None)


def test_live_quote_for_market_returns_book_quote():
    class FakeBook:
        best_ask = 0.98
        best_bid = 0.97

    with patch("scrapers.clob.CLOBClient.get_orderbook", return_value=FakeBook()):
        tok, ask, bid = live_quote_for_market(_raw_data())
    assert tok == YES_TOKEN
    assert (ask, bid) == (0.98, 0.97)


def test_live_quote_for_market_no_token_returns_none():
    tok, ask, bid = live_quote_for_market(json.dumps({"outcomePrices": "[0.5, 0.5]"}))
    assert (tok, ask, bid) == (None, None, None)


# ---------------------------------------------------------------------------
# 4. bet_placer refuses stale-price openings
# ---------------------------------------------------------------------------


def _add_portfolio(session, cash=5000.0):
    from database.models import Portfolio

    if session.query(Portfolio).filter_by(id=1).first() is None:
        session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
        session.commit()


def _make_market(market_factory, yes=0.18, target_hours_ahead=10):
    """Create a Beijing 32C market with a future target_date."""
    from datetime import datetime, timedelta, timezone

    target = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=target_hours_ahead)
    mid = market_factory(
        city="Beijing",
        city_code="ZBAA",
        threshold=32.0,
        target_date=target,
        yes_price=yes,
        no_price=round(1.0 - yes, 4),
        raw_data=_raw_data(yes),
    )
    return mid


def _load_market(session, mid):
    from database.models import WeatherMarket

    return session.query(WeatherMarket).filter_by(id=mid).first()


def test_open_bet_refused_when_db_price_stale(market_factory):
    """DB 0.18 vs live CLOB ask ~0.98 -> bet refused."""
    from database.db import get_session

    mid = _make_market(market_factory, yes=0.18)
    placer = BetPlacer()

    class FakeBook:
        best_ask = 0.98
        best_bid = 0.97

    with get_session() as session:
        _add_portfolio(session)
        market = _load_market(session, mid)
        with patch("scrapers.clob.CLOBClient.get_orderbook", return_value=FakeBook()):
            bet = placer.open_bet_on_market(market, session)
    assert bet is None


def test_open_bet_allowed_when_db_price_fresh(market_factory):
    """DB 0.50 vs live CLOB ask 0.51 -> bet opens."""
    from database.db import get_session

    mid = _make_market(market_factory, yes=0.50)
    placer = BetPlacer()

    class FakeBook:
        best_ask = 0.51
        best_bid = 0.49

    with get_session() as session:
        _add_portfolio(session)
        market = _load_market(session, mid)
        with patch("scrapers.clob.CLOBClient.get_orderbook", return_value=FakeBook()):
            bet = placer.open_bet_on_market(market, session)
        bet_market_id = bet.market_id if bet is not None else None
    assert bet is not None
    assert bet_market_id == mid


def test_open_bet_allowed_when_clob_unreachable(market_factory):
    """CLOB outage -> guard silently passes (never blocks betting on network)."""
    from database.db import get_session

    mid = _make_market(market_factory, yes=0.50)
    placer = BetPlacer()

    with get_session() as session:
        _add_portfolio(session)
        market = _load_market(session, mid)
        with patch("scrapers.clob.CLOBClient.get_orderbook", side_effect=RuntimeError("net down")):
            bet = placer.open_bet_on_market(market, session)
    assert bet is not None
