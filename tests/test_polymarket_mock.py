"""Faz 1: Mock unit tests for PolymarketScraper parse/filter logic."""

import unittest
from datetime import datetime

from scrapers.polymarket import PolymarketScraper


class TestParseMarket(unittest.TestCase):
    """Test _parse_market and _is_weather_market."""

    def setUp(self):
        self.scraper = PolymarketScraper()

    def test_parse_temperature_high(self):
        raw = {
            "id": "test-123",
            "title": "Will NYC have highest temperature above 90F on June 9?",
            "question": "Will New York have highest temperature above 90F on June 9, 2026?",
            "description": "NYC temp",
            "yes_price": 0.35,
            "no_price": 0.65,
            "volume": 50000,
            "liquidity": 10000,
        }
        result = self.scraper._parse_market(raw)

        self.assertIsNotNone(result["target_date"])
        self.assertEqual(result["target_date"].month, 6)
        self.assertEqual(result["target_date"].day, 9)
        self.assertGreater(result["threshold"], 0)
        self.assertIn(result["metric"], ["temperature_max", "temperature_min"])
        self.assertIsNotNone(result["city_code"])
        self.assertIn(result["market_type"], ["HIGH", "LOW"])
        print(
            f"Parse OK: {result['id']} date={result['target_date']} "
            f"threshold={result['threshold']:.1f} metric={result['metric']} "
            f"city_code={result['city_code']} type={result['market_type']}"
        )

    def test_parse_temperature_min(self):
        raw = {
            "id": "test-456",
            "title": "Will London have lowest temperature below 35F on June 9?",
            "question": "Will London have lowest temperature below 35F on June 9, 2026?",
            "description": "London temp",
            "yes_price": 0.35,
            "no_price": 0.65,
            "volume": 50000,
            "liquidity": 10000,
        }
        result = self.scraper._parse_market(raw)

        self.assertIsNotNone(result["target_date"])
        self.assertGreater(result["threshold"], 0)
        # "below" + "lowest" => temperature_min, "below" => LOW
        self.assertEqual(result["metric"], "temperature_min")
        self.assertEqual(result["market_type"], "LOW")
        print(
            f"Parse min OK: {result['id']} threshold={result['threshold']:.1f} "
            f"metric={result['metric']} type={result['market_type']}"
        )

    def test_reject_rain(self):
        raw = {
            "title": "Will London have rainfall above 10mm on June 9?",
            "question": "Will London have rainfall above 10mm?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("Rain rejected OK")

    def test_reject_snow(self):
        raw = {
            "title": "Will NYC have snowfall on June 9?",
            "question": "Will New York have snowfall?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("Snow rejected OK")

    def test_reject_storm(self):
        raw = {
            "title": "Will Miami have hurricane on June 9?",
            "question": "Will Miami have hurricane?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("Storm rejected OK")

    def test_reject_humidity(self):
        raw = {
            "title": "Will Tokyo have humidity above 80% on June 9?",
            "question": "Will Tokyo have humidity above 80%?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("Humidity rejected OK")

    def test_reject_wind(self):
        raw = {
            "title": "Will Chicago have wind speed above 20mph on June 9?",
            "question": "Will Chicago have wind speed above 20mph?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("Wind rejected OK")

    def test_accept_temperature(self):
        raw = {
            "title": "Will Tokyo temperature be above 35C on June 9?",
            "question": "Will Tokyo temperature be above 35C?",
        }
        self.assertTrue(self.scraper._is_weather_market(raw))
        print("Temperature accepted OK")

    def test_no_city_rejected(self):
        raw = {
            "title": "Will temperature be above 35C on June 9?",
            "question": "Will temperature be above 35C?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("No-city rejected OK")

    def test_extract_date_iso(self):
        """ISO format date extraction."""
        result = self.scraper._extract_date("Will NYC temp be above 90F on 2026-06-15?")
        self.assertIsNotNone(result)
        self.assertEqual(result.month, 6)
        self.assertEqual(result.day, 15)
        self.assertEqual(result.hour, 23)
        self.assertEqual(result.minute, 59)
        print(f"ISO date extraction OK: {result}")

    def test_extract_date_yearless(self):
        """Yearless date extraction (should use current year)."""
        result = self.scraper._extract_date("Will NYC temp be above 90F on June 15?")
        self.assertIsNotNone(result)
        self.assertEqual(result.month, 6)
        self.assertEqual(result.day, 15)
        self.assertEqual(result.year, datetime.now().year)
        print(f"Yearless date extraction OK: {result}")

    def test_extract_city_known(self):
        """Known city should return ICAO code."""
        code = self.scraper._extract_city(
            "Will New York have highest temperature above 90F?"
        )
        self.assertEqual(code, "KLGA")
        print(f"City extraction OK: new york -> {code}")

    def test_extract_city_unknown(self):
        """Unknown city should return empty string."""
        code = self.scraper._extract_city(
            "Will Atlantis have highest temperature above 90F?"
        )
        self.assertEqual(code, "")
        print(f"City extraction unknown OK: -> '{code}'")

    def test_strike_fahrenheit(self):
        """F to C conversion: 90F -> 32.2C"""
        strike = self.scraper._extract_strike("Will NYC temp be above 90F?")
        self.assertAlmostEqual(strike, 32.2, delta=0.1)
        print(f"Strike F->C OK: 90F -> {strike:.1f}C")

    def test_strike_celsius(self):
        """Direct C: 35C -> 35.0"""
        strike = self.scraper._extract_strike("Will Tokyo temp be above 35C?")
        self.assertAlmostEqual(strike, 35.0, delta=0.1)
        print(f"Strike C OK: 35C -> {strike:.1f}C")

    def test_no_city_rejects_no_market(self):
        """Market without a known city should be rejected."""
        raw = {
            "title": "Will temperature be above 90F on June 9?",
            "question": "Will temperature be above 90F on June 9, 2026?",
        }
        self.assertFalse(self.scraper._is_weather_market(raw))
        print("No-city market rejected OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
