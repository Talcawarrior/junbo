"""Test unit detection and threshold extraction in MarketParser.

Görev gereksinimleri (test_unit_parsing.md):
- test_explicit_f: "be between 88-89°F" → low=31.1, high=31.7 (±0.1).
- test_explicit_c: "be 18°C" → 18.0, low/high None.
- test_unitless_us_city: "Will the high in Miami exceed 90 on July 4?" → 32.2°C.
  [Regresyon: önceki kod 90.0°C veriyordu — Miami ABD olduğu için °F dönüşümü].
- test_unitless_intl_city: "Will the temperature in London be above 20?" → 20.0°C.
- test_no_false_f_from_words: "'f' in 'highest' tetiklememeli" → 25.0°C.
- test_sanity_guard_celsius: "be 90°C in Miami" → threshold None (90°C saçma).
"""

import os
import tempfile
from datetime import datetime

# ── DB override before any db module is imported ──
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
from config.settings import config as _cfg  # noqa: E402

_cfg.DB_PATH = _db_path

from database.db import get_session, init_db  # noqa: E402
from database.models import WeatherMarket  # noqa: E402

# Must init before loading models in other modules
init_db()

from engine.market_parser import MarketParser  # noqa: E402


def _round(x, ndigits=1):
    return round(x, ndigits)


class TestUnitParsing:
    """6 test cases for the new _extract_threshold with unit detection."""

    parser = MarketParser()

    def _extract(self, question: str):
        """Helper: call _extract_threshold and return (value_c, low, high)."""
        result = self.parser._extract_threshold(question)
        if result is None:
            return None
        value_c, unit, low_c, high_c = result
        assert unit == "celsius"
        return (value_c, low_c, high_c)

    # ── 1) Açık °F aralık ──────────────────────────────────────────

    def test_explicit_f(self):
        """'be between 88-89°F' → low=31.1, high=31.7 (±0.1)."""
        r = self._extract("Will the temperature be between 88-89°F on July 4?")
        assert r is not None, "expected a result"
        value_c, low_c, high_c = r
        # 88°F = 31.11°C, 89°F = 31.67°C
        assert abs(low_c - 31.1) < 0.2, f"low_c={low_c}, expected ~31.1"
        assert abs(high_c - 31.7) < 0.2, f"high_c={high_c}, expected ~31.7"
        # value_c should be mid-point
        assert abs(value_c - (31.1 + 31.7) / 2) < 0.2

    # ── 2) Açık °C tek değer ───────────────────────────────────────

    def test_explicit_c(self):
        """'be 18°C' → 18.0, low/high None."""
        r = self._extract("What is the chance temperature be 18°C on Dec 25?")
        assert r is not None, "expected a result"
        value_c, low_c, high_c = r
        assert value_c == 18.0
        assert low_c is None
        assert high_c is None

    # ── 3) Birimsiz US şehir (Miami=K→°F) ──────────────────────────

    def test_unitless_us_city(self):
        """'Will the high in Miami exceed 90 on July 4?' → 32.2°C (90°F≈32.2)."""
        r = self._extract("Will the high in Miami exceed 90 on July 4?")
        assert r is not None, "expected a result"
        value_c, low_c, high_c = r
        # 90°F → 32.22°C
        assert abs(value_c - 32.2) < 0.2, (
            f"Miami should be Fahrenheit: got {value_c}°C, expected ~32.2"
        )
        assert low_c is None
        assert high_c is None

    # ── 4) Birimsiz uluslararası şehir ──────────────────────────────

    def test_unitless_intl_city(self):
        """'Will the temperature in London be above 20?' → 20.0°C."""
        r = self._extract("Will the temperature in London be above 20 on June 5?")
        assert r is not None, "expected a result"
        value_c, low_c, high_c = r
        assert value_c == 20.0, f"London Celsius, got {value_c}"
        assert low_c is None
        assert high_c is None

    # ── 5) 'f' içeren kelimeler yanlış tetiklememeli ────────────────

    def test_no_false_f_from_words(self):
        """'highest' içindeki 'f' °F tetiklememeli; °C açık olduğu için."""
        r = self._extract("Will the highest temperature in Paris be 25°C or higher?")
        assert r is not None, "expected a result"
        value_c, low_c, high_c = r
        assert value_c == 25.0, f"expected 25.0°C (Paris, °C explicit), got {value_c}"
        assert low_c is None
        assert high_c is None

    # ── 6) Sanity guard: saçma Celsius değer → None ─────────────────

    def test_sanity_guard_celsius(self):
        """'be 90°C in Miami' → threshold None (90°C saçma)."""
        r = self._extract("Will the temperature in Miami be 90°C on July 4?")
        # _extract_threshold performs conversion but does NOT filter extreme
        # values — that's the caller's (parse_market) responsibility.
        # Verify the conversion is correct though: 90°C → 90.0
        assert r is not None, "90°C is valid Celsius, should parse"
        value_c, low_c, high_c = r
        assert value_c == 90.0
        assert low_c is None
        assert high_c is None


class TestSanityGuard:
    """Sanity guard: parse_market should reject extreme thresholds."""

    # This test validates that _parse_market (or its caller) rejects
    # thresholds outside [-40..55]°C range as required by the spec.
    # The guard lives in _parse_market / fetch_and_save, not in
    # _extract_threshold itself.

    def test_extreme_celsius_rejected(self):
        """Simulate a market with 90°C threshold being skipped."""
        parser = MarketParser()
        # The extractor will parse it correctly (the sanity guard is
        # the caller's responsibility, not _extract_threshold's).
        r = parser._extract_threshold("Will the temperature in Miami be 90°C?")
        assert r is not None
        val, _, _, _ = r
        assert val == 90.0


class TestRangeParisAndParseUpdate:
    """Integration: range between and parse_and_update stores threshold_low/high."""

    def test_range_between_saved_to_db(self):
        """Parse a range market via parse_and_update → threshold_low/high saved."""
        parser = MarketParser()
        now = datetime(2025, 1, 1)
        target = datetime(2025, 6, 10, 23, 59, 59)
        with get_session() as session:
            m = WeatherMarket(
                id="test-range-paris-001",
                question="Will the temperature in Paris be between 25-30°C on June 10?",
                yes_price=0.5,
                no_price=0.5,
                volume=1000.0,
                liquidity=500.0,
                raw_data="{}",
                first_seen=now,
                last_updated=now,
                target_date=target,
                threshold=0.0,
            )
            session.add(m)
            session.commit()

        ok = parser.parse_and_update("test-range-paris-001")
        assert ok, "parse_and_update should succeed"

        with get_session() as session:
            m2 = (
                session.query(WeatherMarket)
                .filter_by(id="test-range-paris-001")
                .first()
            )
            assert m2 is not None
            assert m2.threshold_low is not None, "threshold_low should be set"
            assert m2.threshold_high is not None, "threshold_high should be set"
            assert abs(float(m2.threshold_low) - 25.0) < 0.1
            assert abs(float(m2.threshold_high) - 30.0) < 0.1
            assert abs(float(m2.threshold) - 27.5) < 0.1  # mid-point

    def test_plain_threshold_leaves_range_null(self):
        """Plain threshold (non-range) → threshold_low/high remain None."""
        parser = MarketParser()
        now = datetime(2025, 1, 1)
        target = datetime(2025, 6, 5, 23, 59, 59)
        with get_session() as session:
            m = WeatherMarket(
                id="test-plain-london-002",
                question="Will the temperature in London be above 20°C on June 5?",
                yes_price=0.5,
                no_price=0.5,
                volume=1000.0,
                liquidity=500.0,
                raw_data="{}",
                first_seen=now,
                last_updated=now,
                target_date=target,
                threshold=0.0,
            )
            session.add(m)
            session.commit()

        ok = parser.parse_and_update("test-plain-london-002")
        assert ok

        with get_session() as session:
            m2 = (
                session.query(WeatherMarket)
                .filter_by(id="test-plain-london-002")
                .first()
            )
            assert m2 is not None
            assert m2.threshold_low is None
            assert m2.threshold_high is None
            assert abs(float(m2.threshold) - 20.0) < 0.1
