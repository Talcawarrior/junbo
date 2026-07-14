"""Piyasa ve meteoroloji lokasyon eşleştirme modülü."""

from scrapers.polymarket import PolymarketScraper


class LocationMatcher:
    """Matches markets with coordinates and city information (lightweight, no scraper init)."""

    @staticmethod
    def get_coordinates(city_code: str) -> tuple | None:
        """Get lat/lon coordinates from city code."""
        return PolymarketScraper.get_city_coords(city_code)
