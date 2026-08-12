"""Tests for utils/market_outcome.py — Polymarket outcome parse (JSON string bug)."""

from __future__ import annotations

from utils.market_outcome import parse_resolved_outcome, market_is_resolved


class TestParseResolvedOutcome:
    def test_yes_won_json_string(self):
        """outcomePrices JSON STRING — bug 2026-08-12: list gibi davranilinca
        prices[0]='[' oluyordu. Dogru parse True (YES kazandi) dondurur."""
        raw = '{"closed": true, "umaResolutionStatus": "resolved", "outcomePrices": "[\\"1\\", \\"0\\"]"}'
        assert parse_resolved_outcome(raw) is True

    def test_no_won_json_string(self):
        raw = '{"closed": true, "umaResolutionStatus": "resolved", "outcomePrices": "[\\"0\\", \\"1\\"]"}'
        assert parse_resolved_outcome(raw) is False

    def test_yes_won_list(self):
        """outcomePrices zaten list olarak da gelebilir."""
        raw = {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": ["1", "0"]}
        assert parse_resolved_outcome(raw) is True

    def test_unresolved_returns_none(self):
        """umaResolutionStatus != resolved -> None (resmi cozum yok)."""
        raw = '{"closed": false, "umaResolutionStatus": null, "outcomePrices": "[\\"1\\", \\"0\\"]"}'
        assert parse_resolved_outcome(raw) is None

    def test_missing_prices_returns_none(self):
        raw = '{"closed": true, "umaResolutionStatus": "resolved"}'
        assert parse_resolved_outcome(raw) is None

    def test_none_input(self):
        assert parse_resolved_outcome(None) is None
        assert parse_resolved_outcome("") is None

    def test_malformed_json(self):
        assert parse_resolved_outcome("{not json") is None

    def test_partial_prices_returns_none(self):
        raw = '{"closed": true, "umaResolutionStatus": "resolved", "outcomePrices": "[\\"1\\"]"}'
        assert parse_resolved_outcome(raw) is None

    def test_non_numeric_price_returns_none(self):
        raw = '{"closed": true, "umaResolutionStatus": "resolved", "outcomePrices": "[\\"x\\", \\"0\\"]"}'
        assert parse_resolved_outcome(raw) is None


class TestMarketIsResolved:
    def test_resolved_closed(self):
        raw = '{"closed": true, "umaResolutionStatus": "resolved"}'
        assert market_is_resolved(raw) is True

    def test_not_closed(self):
        raw = '{"closed": false, "umaResolutionStatus": "resolved"}'
        assert market_is_resolved(raw) is False

    def test_none(self):
        assert market_is_resolved(None) is False
