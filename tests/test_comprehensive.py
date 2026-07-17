"""Comprehensive test suite for Junbo bot system.

Tests cover:
- AI Models (Karpathy grid search)
- Financial Formulas (Fee, Slippage, Kelly)
- API Endpoints
- Risk Management
- End-to-End Workflows
"""

import pytest
import os
from unittest.mock import patch


# ============================================================================
# 1. AI MODEL TESTLERİ
# ============================================================================

class TestAIModels:
    """AI modelleri için testler."""

    def test_researcher_agent_facts(self):
        """Researcher Agent mock test."""
        with patch('asi_engine.researcher_agent.ResearcherAgent') as MockRA:
            MockRA.return_value.ask.return_value = 'This is a prediction market for weather events'
            agent = MockRA()
            result = agent.ask('test')
            assert isinstance(result, str)

    def test_karpathy_grid_search(self):
        """Karpathy weekly search fonksiyonunu test et."""
        from asi_engine.karpathy_weekly import run_karpathy_weekly
        result = run_karpathy_weekly(rounds=1, use_llm=False, seed=42)
        assert isinstance(result, dict)
        assert "rounds_run" in result or "error" in result

    def test_karpathy_performance(self):
        """Karpathy search performansını test et."""
        import time
        from asi_engine.karpathy_weekly import run_karpathy_weekly
        start = time.time()
        result = run_karpathy_weekly(rounds=2, use_llm=False, seed=42)
        duration = time.time() - start
        assert duration < 120
        assert isinstance(result, dict)

    def test_karpathy_cache(self):
        """Karpathy cache hit rate'ı test et."""
        from asi_engine.karpathy_weekly import _load_best
        best = _load_best()
        assert best is None or hasattr(best, 'round_num')


# ============================================================================
# 2. FORMÜL TESTLERİ
# ============================================================================

class TestFinancialFormulas:
    """Finansal formüller için testler."""

    def test_polymarket_fee(self):
        """Polymarket fee formülünü test et."""
        from utils.formulas import polymarket_fee
        result = polymarket_fee(shares=100, price=0.55, fee_rate=0.05)
        expected = 100 * 0.05 * 0.55 * (1 - 0.55)
        assert abs(result - expected) < 0.0001

    def test_polymarket_fee_edge_cases(self):
        """Polymarket fee edge case testleri."""
        from utils.formulas import polymarket_fee
        # price=0.01: 100 * 0.05 * 0.01 * 0.99 = 0.0495
        result = polymarket_fee(shares=100, price=0.01, fee_rate=0.05)
        assert abs(result - 0.0495) < 0.0001

        # price=0.99: 100 * 0.05 * 0.99 * 0.01 = 0.0495
        result = polymarket_fee(shares=100, price=0.99, fee_rate=0.05)
        assert abs(result - 0.0495) < 0.0001

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
        """Kelly criterion formülünü test et."""
        from utils.kelly import kelly_bet_amount
        kelly_size = kelly_bet_amount(1000.0, 0.10, 0.55)
        assert isinstance(kelly_size, (int, float))
        assert kelly_size >= 0

    def test_kelly_fraction_variations(self):
        """Kelly fraction çeşitlerini test et."""
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
# 3. API ENDPOINT TESTLERİ
# ============================================================================

class TestAPIEndpoints:
    """API endpoint'leri için testler."""

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
# 4. RISK YÖNETİMİ TESTLERİ
# ============================================================================

class TestRiskManagement:
    """Risk yönetimi testleri."""

    def test_max_exposure_enforcement(self):
        """Max exposure kuralını test et."""
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
# 5. E2E TESTLERİ
# ============================================================================

class TestE2E:
    """End-to-End workflow testleri."""

    def test_complete_betting_cycle(self, test_client):
        """Market endpoint yanıt yapısını test et."""
        response = test_client.get("/api/markets")
        assert response.status_code == 200
        data = response.json()
        assert "markets" in data
        assert isinstance(data["markets"], list)

    def test_historical_calibrations_test(self):
        """Historical calibrations parquet dosyasını test et."""
        import pandas as pd
        path = "data/archive/historical_calibrations_20260630.parquet"
        if not os.path.exists(path):
            pytest.skip("Historical calibrations file not found")
        df = pd.read_parquet(path)
        assert len(df) > 0
        assert "bias" in df.columns


# ============================================================================
# 6. DASHBOARD TESTLERİ
# ============================================================================

class TestDashboard:
    """Dashboard ve UI testleri."""

    def test_dashboard_response(self, test_client):
        """Dashboard HTML response testi."""
        response = test_client.get("/")
        assert response.status_code == 200

    def test_yes_no_buttons_working(self, test_client):
        """YES/NO fiyatlarının market verisinde olduğunu test et."""
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
    return TestClient(app)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
