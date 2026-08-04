"""Test cases for PolymarketScraper — see test_polymarket_real.py for
functional tests that exercise the actual parser logic.
"""

import pytest

from scrapers.polymarket import PolymarketScraper


@pytest.mark.asyncio
async def test_polymarket_scraper_has_required_methods():
    """Smoke test: scraper exposes the public async methods the rest of
    the codebase calls."""
    scraper = PolymarketScraper()
    assert hasattr(scraper, "fetch_polymarket_events")
    assert hasattr(scraper, "fetch_and_save")
    assert hasattr(scraper, "init_session")
    assert hasattr(scraper, "close_session")
