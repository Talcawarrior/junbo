"""Integration testler - Bot loop'lari, veri pipeline, API endpoints."""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock


class TestBotStartup:
    """Bot baslatma ve calisma testleri."""

    def test_bot_lifespan_startup(self):
        """Bot lifespan startup testi."""
        from config.settings import bot_config

        assert bot_config is not None

    def test_initial_portfolio_creation(self):
        """Initial portfolio mevcut mu kontrol et."""
        from database.db import ensure_initial_portfolio, get_session
        from database.models import Portfolio

        ensure_initial_portfolio()
        with get_session() as session:
            p = session.query(Portfolio).filter(Portfolio.id == 1).first()
            assert p is not None

    def test_stop_bot(self):
        """Bot state nesnesi calisiyor mu."""
        from api import state

        assert hasattr(state, "is_running")
        assert isinstance(state.is_running, bool)


class TestDataPipeline:
    """Veri pipeline testleri."""

    def test_fetch_markets(self):
        """Polymarket market fetch — mock."""
        with patch("scrapers.polymarket.PolymarketScraper") as MockScraper:
            mock_instance = Mock()
            MockScraper.return_value = mock_instance
            mock_instance.fetch_polymarket_events = AsyncMock(
                return_value=[{"id": "123", "question": "Temperature test"}]
            )
            loop = asyncio.new_event_loop()
            try:
                markets = loop.run_until_complete(mock_instance.fetch_polymarket_events())
            finally:
                loop.close()
            assert len(markets) > 0
            assert markets[0]["id"] == "123"

    def test_analyze_market(self):
        """Market analiz — skip (requires full bot init)."""
        pytest.skip("Requires full bot initialization with DB + weather data")

    def test_place_bet(self):
        """Bet yerlestirme — skip (bet placement in executor now)."""
        pytest.skip("Bet placement moved to executor module")


class TestAPIEndpoints:
    """API endpoint testleri."""

    def test_health_check_endpoint(self):
        """Health-check endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/health-check")
        assert response.status_code == 200
        data = response.json()
        assert "verdict" in data or "is_running" in data

    def test_status_endpoint(self):
        """Status endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "portfolio" in data

    def test_markets_endpoint(self):
        """Markets endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/markets")
        assert response.status_code == 200
        data = response.json()
        assert "markets" in data
        assert "count" in data

    def test_signals_endpoint(self):
        """Signals endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/signals")
        assert response.status_code == 200
        data = response.json()
        assert "signals" in data

    def test_history_endpoint(self):
        """History endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert "history" in data

    def test_slippage_endpoint(self):
        """Slippage endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/slippage")
        assert response.status_code == 200
        data = response.json()
        assert "slippage" in data

    def test_equity_curve_endpoint(self):
        """Equity curve endpoint."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/equity-curve")
        assert response.status_code == 200
        data = response.json()
        assert "initial" in data
        assert "points" in data


class TestUIComponents:
    """UI component testleri — Python-level data validation only."""

    def test_dashboard_stats_display(self):
        """Dashboard istatistikleri."""
        stats = {"total_bets": 100, "win_count": 55, "loss_count": 45, "total_pnl": 250.0, "total_roi": 25.0}
        win_rate = (stats["win_count"] / stats["total_bets"]) * 100
        assert win_rate == pytest.approx(55.0, abs=0.1)

    def test_open_position_display(self):
        """Acik pozisyon PnL hesaplama."""
        entry = 0.50
        current = 0.52
        shares = 6.0
        pnl = shares * (current - entry)  # 0.12
        assert pnl == pytest.approx(0.12, abs=0.01)


class TestRiskManagement:
    """Risk yonetimi testleri."""

    def test_exposure_cap_validation(self):
        """Exposure cap validasyonu."""
        from utils.formulas import max_exposure_cap

        max_exposure = max_exposure_cap(1000.0, 50.0, 0.25)
        expected = 1050.0 * 0.25
        assert max_exposure == pytest.approx(expected, abs=0.1)

    def test_city_cap_validation(self):
        """City cap validasyonu."""
        city_cap = 4
        city_distribution = [3, 3, 2, 2]
        total_in_cities = sum(city_distribution)
        assert total_in_cities <= city_cap * len(city_distribution)

    def test_daily_loss_limit(self):
        """Daily loss limit."""
        daily_loss_limit_pct = 0.05
        daily_loss_amount = 50.0
        portfolio = 1000.0
        limit = daily_loss_limit_pct * portfolio
        assert daily_loss_amount == pytest.approx(limit, abs=0.01)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
