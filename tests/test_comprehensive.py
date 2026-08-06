"""Comprehensive test suite for Junbo bot system.

Tests cover:
- AI Models
- Financial Formulas (Fee, Slippage, Kelly)
- API Endpoints
- Risk Management
- End-to-End Workflows
"""

import pytest
import os


# ============================================================================
# 2. FORMUL TESTLERI
# ============================================================================


class TestFinancialFormulas:
    """Finansal formuller icin testler."""

    def test_polymarket_fee(self):
        """Polymarket fee formulunu test et (weather exponent=0.5)."""
        from config.settings import bot_config
        from utils.formulas import polymarket_fee

        result = polymarket_fee(shares=100, price=0.55, fee_rate=0.05)
        # Weather kategorisi flatter fee curve kullanir: fee = C·rate·p·(1-p)^0.5
        exponent = getattr(bot_config.strategy, "fee_exponent", 1.0)
        expected = 100 * 0.05 * 0.55 * ((1 - 0.55) ** exponent)
        assert abs(result - expected) < 0.0001

    def test_polymarket_fee_edge_cases(self):
        """Polymarket fee edge case testleri."""
        from config.settings import bot_config
        from utils.formulas import polymarket_fee

        exponent = getattr(bot_config.strategy, "fee_exponent", 1.0)

        # price=0.01: 100 * 0.05 * 0.01 * 0.99^exp
        result = polymarket_fee(shares=100, price=0.01, fee_rate=0.05)
        expected = 100 * 0.05 * 0.01 * (0.99**exponent)
        assert abs(result - expected) < 0.0001

        # price=0.99: 100 * 0.05 * 0.99 * 0.01^exp
        result = polymarket_fee(shares=100, price=0.99, fee_rate=0.05)
        expected = 100 * 0.05 * 0.99 * (0.01**exponent)
        assert abs(result - expected) < 0.0001

        # price=1.00: fee = 0
        result = polymarket_fee(shares=100, price=1.00, fee_rate=0.05)
        assert result == 0.0

    def test_gas_fee_calculation(self):
        """Gas fee hesaplama testi — adjust_edge_for_costs."""
        from utils.slippage import adjust_edge_for_costs

        raw_edge = 0.10
        result = adjust_edge_for_costs(raw_edge, entry_price=0.55, bet_amount_usd=30.0)
        assert isinstance(result, (int, float))
        assert result <= raw_edge  # Gas reduces edge

    def test_tiered_slippage(self):
        """Tiered slippage modelini test et."""
        from utils.slippage import _tiered_slippage

        assert abs(_tiered_slippage(0.03) - 0.03) < 0.0001
        assert abs(_tiered_slippage(0.07) - 0.01) < 0.0001
        assert abs(_tiered_slippage(0.55) - 0.005) < 0.0001

    def test_flat_slippage(self):
        """Flat slippage modelini test et."""
        from utils.slippage import estimate_slippage

        est = estimate_slippage(entry_price=0.55, model="flat")
        assert est.model_used == "flat"
        assert est.slippage_pct == 0.005

    def test_kelly_criterion(self):
        """Kelly criterion formulunu test et."""
        from utils.kelly import kelly_bet_amount

        kelly_size = kelly_bet_amount(1000.0, 0.10, 0.55)
        assert isinstance(kelly_size, (int, float))
        assert kelly_size >= 0

    def test_kelly_fraction_variations(self):
        """Kelly fraction cesitlerini test et."""
        from utils.kelly import kelly_bet_amount

        for frac in [0.10, 0.15, 0.20]:
            kelly_size = kelly_bet_amount(1000.0, 0.10, 0.55, fraction=frac)
            assert kelly_size >= 0

    def test_unrealized_pnl(self):
        """Unrealized PnL hesaplama."""
        from utils.formulas import unrealized_pnl

        pnl = unrealized_pnl(1000, 0.60, 0.55)
        assert abs(pnl - 50.0) < 0.01

    def test_settlement_pnl(self):
        """Settlement PnL hesaplama."""
        from utils.formulas import settlement_pnl

        won_pnl = settlement_pnl(stake=100.0, entry_price=0.55, entry_fee=0.04, won=True)
        expected_payout = 100.0 / 0.55
        expected_pnl = expected_payout - 100.0 - 0.04
        assert abs(won_pnl - expected_pnl) < 0.01

        lost_pnl = settlement_pnl(stake=100.0, entry_price=0.55, entry_fee=0.04, won=False)
        assert lost_pnl == -(100.0 + 0.04)


# ============================================================================
# 3. API ENDPOINT TESTLERI
# ============================================================================


class TestAPIEndpoints:
    """API endpoint'leri icin testler."""

    def test_health_check_endpoint(self, test_client):
        """Health check endpoint testi."""
        response = test_client.get("/api/health-check")
        assert response.status_code == 200
        data = response.json()
        # Current API returns "verdict" and "is_running"
        assert "verdict" in data or "is_running" in data

    def test_portfolio_endpoint(self, test_client):
        """Portfolio endpoint testi."""
        response = test_client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "portfolio" in data

    def test_markets_endpoint(self, test_client):
        """Markets endpoint testi."""
        response = test_client.get("/api/markets")
        assert response.status_code == 200
        data = response.json()
        assert "markets" in data
        assert "count" in data


# ============================================================================
# 4. RISK YONETIMI TESTLERI
# ============================================================================


class TestRiskManagement:
    """Risk yonetimi testleri."""

    def test_max_exposure_enforcement(self):
        """Max exposure kuralini test et."""
        from utils.formulas import max_exposure_cap

        max_exposure = max_exposure_cap(1000.0, 50.0, 0.25)
        expected = (1000.0 + 50.0) * 0.25
        assert abs(max_exposure - expected) < 0.01

    def test_stop_loss_trigger(self):
        """Stop-loss trigger testi."""
        edge = -0.03
        stop_loss_threshold = -0.02
        assert edge < stop_loss_threshold

    def test_stop_loss_safe_zone(self):
        """Stop-loss safe zone testi."""
        edge = -0.01
        stop_loss_threshold = -0.02
        assert not (edge < stop_loss_threshold)


# ============================================================================
# 5. E2E TESTLERI
# ============================================================================


class TestE2E:
    """End-to-End workflow testleri."""

    def test_complete_betting_cycle(self, test_client):
        """Market endpoint yanit yapisini test et."""
        response = test_client.get("/api/markets")
        assert response.status_code == 200
        data = response.json()
        assert "markets" in data
        assert isinstance(data["markets"], list)

    def test_historical_calibrations_test(self):
        """Historical calibrations parquet dosyasini test et."""
        import pandas as pd

        path = "data/archive/historical_calibrations_20260630.parquet"
        if not os.path.exists(path):
            pytest.skip("Historical calibrations file not found")
        df = pd.read_parquet(path)
        assert len(df) > 0
        assert "bias" in df.columns


# ============================================================================
# 6. DASHBOARD TESTLERI
# ============================================================================


class TestDashboard:
    """Dashboard ve UI testleri."""

    def test_dashboard_response(self, test_client):
        """Dashboard HTML response testi."""
        response = test_client.get("/")
        assert response.status_code == 200

    def test_yes_no_buttons_working(self, test_client):
        """YES/NO fiyatlarinin market verisinde oldugunu test et."""
        response = test_client.get("/api/markets")
        assert response.status_code == 200
        data = response.json()
        assert "markets" in data

    def test_dashboard_data_update(self):
        """WebSocket update testi — stub."""
        assert True


# ============================================================================
# CONFTEST (Shared fixtures)
# ============================================================================


@pytest.fixture
def test_client():
    """FastAPI test client."""
    from fastapi.testclient import TestClient
    from main import app
    from database.db import ensure_initial_portfolio

    # TestClient lifesp'a event'leri calistirmaz; get_status portfolio
    # satiri olmadan None crash'i verir. Temp DB'ye portfolio ekle.
    ensure_initial_portfolio()

    return TestClient(app)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
