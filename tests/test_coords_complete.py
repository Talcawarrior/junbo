"""Test ICAO coordinates completeness and lookup correctness."""

from config.settings import Config
from scrapers.polymarket import PolymarketScraper


def test_all_icaos_have_coords():
    """Every ICAO in CITY_ICAO_MAP must have an entry in ICAO_COORDS."""
    missing = [
        code for code in Config.CITY_ICAO_MAP.values() if code not in Config.ICAO_COORDS
    ]
    assert not missing, f"ICAOs missing ICAO_COORDS: {missing}"


def test_no_secondary_coord_dicts():
    """No other module should define its own coords dict."""
    from scrapers.meteo import MeteoFetcher

    assert not hasattr(MeteoFetcher, "CITY_COORDS"), (
        "MeteoFetcher still has CITY_COORDS"
    )

    import inspect

    import scrapers.polymarket as pm

    source = inspect.getsource(pm.PolymarketScraper.get_city_coords)
    assert "coords_map" not in source, "get_city_coords still has local coords_map"


def test_get_city_coords_returns_known():
    """Spot-check 4 major airports return correct lat/lon."""
    scraper = PolymarketScraper()
    assert scraper.get_city_coords("EGLL") == (51.4700, -0.4543)  # London Heathrow
    assert scraper.get_city_coords("KLAX") == (33.9416, -118.4085)  # Los Angeles
    assert scraper.get_city_coords("RJTT") == (35.5533, 139.7811)  # Tokyo Haneda
    assert scraper.get_city_coords("LTFM") == (41.2753, 28.7519)  # Istanbul


def test_get_city_coords_unknown():
    """Unknown ICAO returns None (status=no_coords later)."""
    scraper = PolymarketScraper()
    assert scraper.get_city_coords("XXXX") is None


def test_icao_coords_has_new_entries():
    """Ensure the 29 newly added ICAOs are present in ICAO_COORDS."""
    new_icaos = {
        "CYUL",
        "CYVR",
        "CYYZ",
        "EDDM",
        "ESSA",
        "FACT",
        "HECA",
        "KDCA",
        "KMCO",
        "KSFO",
        "LGAV",
        "LLBG",
        "LOWW",
        "LPPT",
        "LSZH",
        "MMGL",
        "NZAA",
        "OTHH",
        "RCTP",
        "RJOO",
        "SAEZ",
        "SCEL",
        "SPJC",
        "VABB",
        "VIDP",
        "VTBS",
        "WIII",
        "WSSS",
        "YMML",
    }
    missing = [c for c in new_icaos if c not in Config.ICAO_COORDS]
    assert not missing, f"Newly-added ICAOs still missing coords: {missing}"
