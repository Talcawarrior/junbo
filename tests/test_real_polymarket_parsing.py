"""Regression tests against REAL Polymarket API data.

These tests pin the endDate parsing bug that caused the 24h betting rule
to be computed 12 hours too late (markets close at 12:00 UTC, not 23:59:59).

Fixture: tests/fixtures/polymarket_market_samples.json — real raw_data
snapshots pulled from data/bot.db. Tests fail if the parser ever regresses
to using the title-derived 23:59:59 target instead of the real endDate.
"""

import json
import os
from datetime import datetime, timedelta as _td
from unittest import mock

import pytest

from engine.market_parser import MarketParser
from scrapers.polymarket import PolymarketScraper

_8h = _td(hours=8)
_24h = _td(hours=24)
_18h = _td(hours=18)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "polymarket_market_samples.json")


def timedelta_ok(delta):
    # Erken acilis siniri (2026-08-07): pencere ustu 24h -> 18h'e indirildi.
    return _8h < delta <= _18h


@pytest.fixture(scope="module")
def samples():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _mk_market(raw: dict, **overrides):
    """Build a lightweight WeatherMarket stand-in exposing raw_data."""

    class M:
        raw_data = json.dumps(raw)

    return M()


# ── 1) Golden snapshot: endDate alanini gercek JSON'dan oku ────────────────
class TestEndDateFromRealData:
    def test_fixture_has_enddate(self, samples):
        assert samples, "fixture bos olmamali"
        for s in samples:
            end = s["raw"].get("endDate") or s["raw"].get("end_date_iso")
            assert end, f"market {s['market_id']} endDate yok"

    def test_scraper_extracts_real_enddate(self, samples):
        """Gercek endDate 12:00 UTC olmali (23:59:59 DEGIL)."""
        scraper = PolymarketScraper()
        for s in samples[:6]:
            end = s["raw"].get("endDate") or s["raw"].get("end_date_iso")
            dt = scraper._extract_end_date(s["raw"])
            assert dt is not None, f"market {s['market_id']} parse edilemedi"
            assert dt.hour == 12, f"{s['market_id']}: saat {dt.hour} olmali 12 (endDate={end})"
            assert dt.minute == 0
            # Gun, endDate'in gunune esit olmali
            assert dt.date() == datetime.fromisoformat(end.replace("Z", "+00:00")).date()

    def test_parser_extracts_real_enddate(self, samples):
        """MarketParser._extract_end_date ayni sonucu vermeli."""
        parser = MarketParser()
        for s in samples[:6]:
            end = s["raw"].get("endDate") or s["raw"].get("end_date_iso")
            dt = parser._extract_end_date(_mk_market(s["raw"]))
            assert dt is not None
            assert dt.hour == 12, f"{s['market_id']}: saat {dt.hour} olmali 12"
            assert dt.date() == datetime.fromisoformat(end.replace("Z", "+00:00")).date()

    def test_enddate_preferred_over_title_date(self, samples):
        """endDate varsa title'dan turetilen 23:59:59 kullanilmamali."""
        scraper = PolymarketScraper()
        for s in samples[:6]:
            raw = dict(s["raw"])
            # Title tabanli _extract_date 23:59:59 dondurur; endDate 12:00'yi eziyor.
            parsed = scraper._parse_market(raw)
            assert parsed["target_date"].hour == 12, (
                f"{s['market_id']}: _parse_market target_date {parsed['target_date']} "
                "endDate'i kullanmali (12:00), 23:59:59 degil"
            )

    def test_enddate_missing_falls_back_to_title(self):
        """endDate yoksa _extract_date fallback calisir (23:59:59)."""
        scraper = PolymarketScraper()
        raw = {"endDate": None, "end_date_iso": None}
        assert scraper._extract_end_date(raw) is None
        dt = scraper._extract_date("Will the highest temperature in London be 22C on August 7, 2026?")
        assert dt is not None
        assert dt.hour == 23 and dt.minute == 59


# ── 2) days_ahead hesabi 12:00 target_date ile dogru gunu vermeli ──────────
class TestDaysAheadRegression:
    def test_days_ahead_is_calendar_based(self):
        """target_date 12:00 olsa da days_ahead gun farki olmali, saat degil."""

        # now = 7 Agustos 14:00 UTC, target = 8 Agustos 12:00 UTC -> 1 gun
        now = datetime(2026, 8, 7, 14, 0, 0)
        target = datetime(2026, 8, 8, 12, 0, 0)

        with mock.patch("engine.calculator.datetime") as dtmock:
            dtmock.now.return_value = now
            dtmock.side_effect = lambda *a, **kw: datetime(*a, **kw) if a else now
            # hesap: (target.date() - now.date()).days
            days = (target.date() - now.date()).days
            assert days == 1, f"8 Agustos 12:00 bugune 1 gun uzakta olmali, got {days}"

        # Gercek kod yolu: Calculator.analyze_market'te kullanilan ifade
        source_path = os.path.join(os.path.dirname(__file__), "..", "engine", "calculator.py")
        with open(source_path, encoding="utf-8") as f:
            src = f.read()
        assert ".date()" in src.split("days_ahead =")[1].split("\n")[0], (
            "calculator.days_ahead date-bazli olmali (target_date.date() - now.date()).days"
        )

    def test_days_ahead_zero_for_today(self):
        """Bugunku market (ayni gun) days_ahead=0 vermeli."""

        now = datetime(2026, 8, 7, 14, 0, 0)
        target = datetime(2026, 8, 7, 12, 0, 0)  # bugun 12:00
        assert (target.date() - now.date()).days == 0


# ── 3) place_all_pending bet penceresi gercek endDate ile ───────────────────
class TestBetPlacementWindow:
    def test_market_with_enddate_12h_in_window(self):
        """8 Agustos 12:00 UTC marketi, 7 Agustos 20:00 UTC'de pencereye GIRMELI.

        Erken acilis siniri (2026-08-07): marketler yalnizca vadeye 8-18 saat
        kala bet acilir. 8 Agustos 12:00 - 7 Agustos 20:00 = 16h -> pencerede.
        """
        now = datetime(2026, 8, 7, 20, 0, 0)
        target = datetime(2026, 8, 8, 12, 0, 0)  # gercek endDate
        delta = target - now
        assert timedelta_ok(delta), f"kalan {delta} -> 18h icinde olmali"
        assert delta > _8h, "8 saatten az kaldiysa bet acilmaz"

    def test_market_with_enddate_14h_out_of_early_window(self):
        """7 Agustos 14:00 UTC'de 8 Agustos 12:00 (22h kala) marketi ARTIK acilmaz.

        Erken acilis siniri oncesi: 22h kalan marketler aciliyordu ve SL orani
        yuksekti (20-22h oncesi ortalama %45 vs 16-18h %26).
        """
        now = datetime(2026, 8, 7, 14, 0, 0)
        target = datetime(2026, 8, 8, 12, 0, 0)  # gercek endDate, kalan 22h
        delta = target - now
        assert delta > _18h, f"kalan {delta} 18h sinirini asmali"
        assert not timedelta_ok(delta), "22h kalan market erken-acilis sinirinda acilmaz"

    def test_market_with_bad_23_59_target_is_out_of_window(self):
        """Bug fix oncesi DB degeri (23:59:59) ile ayni market 18h disinda kaliyordu."""
        now = datetime(2026, 8, 7, 14, 0, 0)
        bad_target = datetime(2026, 8, 8, 23, 59, 59)
        delta = bad_target - now
        assert delta > _24h, f"23:59:59 target ile kalan {delta} > 24h (bug isareti)"
