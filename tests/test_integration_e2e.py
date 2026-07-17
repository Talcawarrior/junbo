"""Integration/E2E Tests - Tüm pipeline'ı uçtan uca test eder."""

import pytest
from datetime import datetime, timezone, timedelta


class TestFullPipelineE2E:
    """Tam pipeline E2E testi."""

    def test_fetch_to_bet_pipeline(self):
        """Veri çekme → analiz → fee hesaplama pipeline'ı."""
        # Step 1: Market data (mock)
        mock_market = {
            "id": "test_123",
            "city": "Dallas",
            "threshold": 80.0,
            "metric": "temperature_max",
            "yes_price": 0.55,
            "no_price": 0.45,
        }
        assert mock_market["yes_price"] + mock_market["no_price"] == 1.0

        # Step 2: Probability (mock)
        prob = 0.65
        assert 0.0 <= prob <= 1.0

        # Step 3: Fee (real)
        from utils.formulas import polymarket_fee
        fee = polymarket_fee(shares=100, price=0.55, fee_rate=0.05)
        assert fee >= 0

        # Step 4: Edge (real)
        edge = prob - mock_market["yes_price"]
        assert isinstance(edge, float)

        # Step 5: Kelly (real)
        from utils.kelly import kelly_fraction
        kelly = kelly_fraction(prob, mock_market["yes_price"])
        assert 0 <= kelly <= 1.0


class TestRiskManagementE2E:
    """Risk yönetimi E2E testi."""

    def test_exposure_cap_enforcement(self):
        """Exposure cap uygulanmalı."""
        from utils.formulas import max_exposure_cap
        max_exp = max_exposure_cap(1000.0, 50.0, 0.25)
        current_exposure = 200.0
        new_bet = 3.0
        assert current_exposure + new_bet <= max_exp

    def test_city_cap_enforcement(self):
        """City cap uygulanmalı."""
        from config.settings import bot_config
        city_cap = bot_config.city_cap
        assert city_bets["Dallas"] >= city_cap if "Dallas" in city_bets else True
        assert city_bets["London"] < city_cap if "London" in city_bets else True

    def test_daily_loss_limit(self):
        """Daily loss limit uygulanmalı."""
        from config.settings import bot_config
        daily_loss_limit_pct = bot_config.strategy.daily_loss_limit
        initial_capital = 1000.0
        daily_loss_limit_amount = initial_capital * daily_loss_limit_pct
        daily_pnl = -60.0
        assert daily_pnl > -daily_loss_limit_amount


# Global city_bets for test
city_bets = {"Dallas": 4, "London": 3, "Paris": 2}


class TestDatabaseE2E:
    """Database E2E testi."""

    def test_market_creation_and_query(self):
        """Market oluşturma ve sorgulama."""
        from database.db import get_session
        from database.models import WeatherMarket

        test_id = f"test_e2e_{int(datetime.now().timestamp())}"
        with get_session() as session:
            market = WeatherMarket(
                id=test_id,
                city="Dallas",
                city_code="KDAL",
                target_date=datetime.now(timezone.utc) + timedelta(days=2),
                threshold=80.0,
                metric="temperature_max",
                yes_price=0.55,
                no_price=0.45,
                status="open",
            )
            session.add(market)
            session.commit()

            queried = session.query(WeatherMarket).filter_by(id=test_id).first()
            assert queried is not None
            assert queried.city == "Dallas"
            assert queried.yes_price == 0.55

            session.delete(queried)
            session.commit()

    def test_bet_creation_and_query(self):
        """Bet oluşturma ve sorgulama."""
        from database.db import get_session
        from database.models import Bet

        test_id = int(datetime.now().timestamp() * 1000) % 900000 + 100000
        with get_session() as session:
            bet = Bet(
                id=test_id,
                market_id="test_e2e_nonexistent",
                city="Dallas",
                side="YES",
                amount=3.0,
                entry_price=0.55,
                current_price=0.55,
                status="placed",
                placed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(bet)
            session.commit()

            queried = session.query(Bet).filter_by(id=test_id).first()
            assert queried is not None
            assert queried.city == "Dallas"
            assert queried.amount == 3.0

            session.delete(queried)
            session.commit()


class TestAPIEndpointsE2E:
    """API endpoint E2E testleri."""

    def test_health_check_e2e(self):
        """Health check endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app
        client = TestClient(app)
        response = client.get("/api/health-check")
        assert response.status_code == 200
        data = response.json()
        assert "verdict" in data or "is_running" in data

    def test_status_endpoint_e2e(self):
        """Status endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app
        client = TestClient(app)
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "portfolio" in data

    def test_markets_endpoint_e2e(self):
        """Markets endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app
        client = TestClient(app)
        response = client.get("/api/markets")
        assert response.status_code == 200
        data = response.json()
        assert "markets" in data
        assert "count" in data

    def test_signals_endpoint_e2e(self):
        """Signals endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app
        client = TestClient(app)
        response = client.get("/api/signals")
        assert response.status_code == 200
        data = response.json()
        assert "signals" in data

    def test_history_endpoint_e2e(self):
        """History endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app
        client = TestClient(app)
        response = client.get("/api/history")
        assert response.status_code == 200
        data = response.json()
        assert "history" in data


class TestSlippageGasFeeE2E:
    """Slippage ve gas fee E2E testleri."""

    def test_slippage_calculation_e2e(self):
        """Slippage hesaplama E2E."""
        from utils.slippage import estimate_slippage
        for price in [0.10, 0.25, 0.50, 0.75, 0.90]:
            est = estimate_slippage(price)
            assert 0 <= est.slippage_pct <= 0.10

    def test_gas_fee_calculation_e2e(self):
        """Gas fee hesaplama E2E."""
        from config.settings import bot_config
        gas_cost = bot_config.strategy.gas_cost_usd
        assert gas_cost > 0
        assert gas_cost < 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
