"""Real functional tests for PolymarketScraper parser logic.

These replace the previous one-line `test_polymarket.py` that only checked
`hasattr(scraper, "fetch_polymarket_events")`. The new tests exercise
`_parse_market`, `_is_weather_market`, `_extract_city`, `_extract_date`,
and `_determine_market_type` with realistic Polymarket-shaped payloads.
"""

from datetime import datetime

import pytest

from scrapers.polymarket import PolymarketScraper


@pytest.fixture
def scraper():
    return PolymarketScraper()


class TestIsWeatherMarket:
    def test_accepts_temperature_above_question(self, scraper):
        market = {
            "title": "Highest temperature in Miami above 90F on June 9?",
            "question": "Will Miami have highest temperature above 90F on June 9, 2026?",
            "description": "",
        }
        assert scraper._is_weather_market(market) is True

    def test_rejects_sports_market_sharing_city_name(self, scraper):
        market = {
            "title": "Will Boston Bruins win on June 9?",
            "question": "Will Boston win the game?",
            "description": "",
        }
        assert scraper._is_weather_market(market) is False

    def test_rejects_rain_market(self, scraper):
        market = {
            "title": "Will London have rainfall above 10mm on June 9?",
            "question": "Will London have rainfall above 10mm?",
            "description": "",
        }
        assert scraper._is_weather_market(market) is False

    def test_rejects_snow_market(self, scraper):
        market = {
            "title": "Will NYC have snowfall on June 9?",
            "question": "Will New York have snowfall?",
            "description": "",
        }
        assert scraper._is_weather_market(market) is False

    def test_rejects_unknown_city(self, scraper):
        market = {
            "title": "Highest temperature in Atlantis above 90F on June 9?",
            "question": "Will Atlantis have highest temperature above 90F?",
            "description": "",
        }
        assert scraper._is_weather_market(market) is False


class TestExtractCity:
    def test_finds_known_city_lowercase(self, scraper):
        assert scraper._extract_city("Will miami have temperature above 90F?") == "KMIA"

    def test_finds_multiword_city(self, scraper):
        assert (
            scraper._extract_city("Will new york have temperature above 90F?") == "KLGA"
        )

    def test_returns_empty_for_unknown_city(self, scraper):
        assert scraper._extract_city("Will Atlantis have temperature above 90F?") == ""

    def test_returns_empty_for_empty_input(self, scraper):
        assert scraper._extract_city("") == ""


class TestExtractDate:
    def test_parses_full_date_with_year(self, scraper):
        d = scraper._extract_date(
            "Will NYC have temperature above 90F on June 9, 2026?"
        )
        assert d is not None
        assert d.year == 2026
        assert d.month == 6
        assert d.day == 9

    def test_parses_iso_date(self, scraper):
        d = scraper._extract_date("Temperature forecast for 2026-06-09 in NYC")
        assert d is not None
        assert d.year == 2026
        assert d.month == 6
        assert d.day == 9

    def test_parses_yearless_date_uses_current_year(self, scraper):
        d = scraper._extract_date("Will NYC have temperature above 90F on June 9?")
        assert d is not None
        assert d.month == 6
        assert d.day == 9
        assert d.year == datetime.now().year

    def test_returns_none_for_no_date(self, scraper):
        assert scraper._extract_date("Will NYC have temperature above 90F?") is None

    def test_returns_none_for_empty(self, scraper):
        assert scraper._extract_date("") is None

    def test_does_not_false_match_above_90(self, scraper):
        # The string "above 90" should NOT be misinterpreted as a month/day.
        # (Previous regex could match "above 90" as "above" + 90, etc.)
        d = scraper._extract_date("Temperature will be above 90 degrees")
        assert d is None


class TestDetermineMarketType:
    def test_above_is_high(self, scraper):
        assert (
            scraper._determine_market_type("Will temperature be above 90F?") == "HIGH"
        )

    def test_higher_is_high(self, scraper):
        assert (
            scraper._determine_market_type("Will temperature be higher than 90F?")
            == "HIGH"
        )

    def test_below_is_low(self, scraper):
        assert scraper._determine_market_type("Will temperature be below 32F?") == "LOW"

    def test_lower_is_low(self, scraper):
        assert (
            scraper._determine_market_type("Will temperature be lower than 32F?")
            == "LOW"
        )

    def test_neutral_is_range(self, scraper):
        assert (
            scraper._determine_market_type("What will the temperature be?") == "RANGE"
        )


class TestParseMarket:
    def test_parse_extracts_all_fields(self, scraper):
        raw = {
            "id": "test-001",
            "title": "Highest temperature in Miami above 90F on June 9, 2026?",
            "question": "Will Miami have highest temperature above 90F on June 9, 2026?",
            "description": "Miami temperature market",
            "tokens": [
                {"outcome": "YES", "price": 0.35},
                {"outcome": "NO", "price": 0.65},
            ],
            "volume": 50000,
            "liquidity": 10000,
        }
        result = scraper._parse_market(raw)

        assert result["id"] == "test-001"
        assert result["city"] == "Miami"
        assert result["city_code"] == "KMIA"
        assert result["threshold"] > 0  # 90F → ~32.2C
        assert result["market_type"] == "HIGH"
        assert result["yes_price"] == 0.35
        assert result["no_price"] == 0.65
        assert result["volume"] == 50000
        assert result["liquidity"] == 10000
        assert result["target_date"] is not None
        assert result["target_date"].month == 6
        assert result["target_date"].day == 9

    def test_parse_handles_missing_tokens_uses_lastTradePrice(self, scraper):  # noqa: N802
        raw = {
            "id": "test-002",
            "title": "Highest temperature in Miami above 90F on June 9, 2026?",
            "question": "Will Miami have highest temperature above 90F on June 9, 2026?",
            "lastTradePrice": 0.40,
            "volume": 1000,
            "liquidity": 0,
        }
        result = scraper._parse_market(raw)
        assert result["yes_price"] == 0.40
        # NO price is derived as 1 - YES.
        assert abs(result["no_price"] - 0.60) < 1e-6

    def test_parse_defaults_to_half_when_no_price(self, scraper):
        raw = {
            "id": "test-003",
            "title": "Highest temperature in Miami above 90F on June 9, 2026?",
            "question": "Will Miami have highest temperature above 90F on June 9, 2026?",
        }
        result = scraper._parse_market(raw)
        assert result["yes_price"] == 0.5
        assert result["no_price"] == 0.5

    def test_parse_fahrenheit_converted_to_celsius(self, scraper):
        # 90°F should be converted to ~32.2°C internally.
        raw = {
            "id": "test-004",
            "title": "Highest temperature in Miami above 90F on June 9, 2026?",
            "question": "Will Miami have highest temperature above 90F on June 9, 2026?",
        }
        result = scraper._parse_market(raw)
        # 90°F = (90-32) * 5/9 = 32.222°C
        assert 31.0 < result["threshold"] < 33.5


class TestGetCityCoords:
    def test_known_icao_returns_coords(self, scraper):
        coords = scraper.get_city_coords("KMIA")
        assert coords is not None
        lat, lon = coords
        # Miami is at ~25.8°N, ~80.3°W
        assert 25.0 < lat < 26.5
        assert -81.0 < lon < -79.5

    def test_unknown_icao_returns_none(self, scraper):
        assert scraper.get_city_coords("XXXX") is None
