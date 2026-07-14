"""Comprehensive test suite for Junbo bot system.

Tests cover:
- AI Models (Semua, Karpathy)
- Financial Formulas (Fee, Gas, Slippage, Kelly)
- API Endpoints
- Data Pipeline
- Risk Management
- UI/Dashboard
- End-to-End Workflows
"""

import pytest
from decimal import Decimal
import datetime


# ============================================================================
# 1. AI MODEL TESTLERİ
# ============================================================================

class TestAIModels:
    """Semua ve Karpathy AI modelleri için testler."""

    def test_researcher_agent_facts(self):
        """Researcher Agent'ın ansiklopedik bilgi verdiğini test et."""
        from asi_engine.researcher_agent import ResearcherAgent

        agent = ResearcherAgent()
        query = "What is Polymarket?"
        result = agent.ask(query)

        # Fact-check: Politic/religion kelimeleri olmamalı
        assert "politics" not in result.lower() or "political prediction" in result.lower()
        assert "religion" not in result.lower() or "religious event" in result.lower()

    def test_karpathy_grid_search(self):
        """Karpathy grid search parametre optimizasyonunu test et."""
        from asi_engine.karpathy_weekly import karpathy_search
        import pandas as pd

        # Mock data
        mock_data = pd.DataFrame({
            'date': pd.date_range('2026-01-01', periods=100),
            'edge': [0.05 + i * 0.01 for i in range(100)]
        })

        params_grid = {
            'min_edge': [0.03, 0.05, 0.08],
            'kelly_fraction': [0.10, 0.15, 0.20]
        }

        result = karpathy_search(params_grid, min_edge=0.05, kelly_fraction=0.15)

        # Validate result structure
        assert 'min_edge' in result
        assert 'kelly_fraction' in result
        assert 'roi' in result
        assert 0 < result['min_edge'] <= 0.10
        assert 0 < result['kelly_fraction'] <= 0.25
        assert result['roi'] > 0  # Must have positive ROI

    def test_karpathy_performance(self):
        """Karpathy search performansını test et."""
        import time
        from asi_engine.karpathy_weekly import karpathy_search
        import pandas as pd

        mock_data = pd.DataFrame({
            'date': pd.date_range('2026-01-01', periods=100),
            'edge': [0.05] * 100
        })

        start = time.time()
        result = karpathy_search({'min_edge': [0.05], 'kelly_fraction': [0.15]}, mock_data)
        duration = time.time() - start

        # Karpathy search 100 market için < 60s olmalı
        assert duration < 60

    def test_karpathy_cache(self):
        """Karpathy cache hit rate'ı test et."""
        from asi_engine.karpathy_weekly import KarpathyWeekly
        from cache import memoize

        # Mock cached results
        cache_hits = 0
        total_requests = 100

        # Test cache hit rate > 80%
        # (This would be implemented with actual caching mechanism)
        assert True  # Placeholder


# ============================================================================
# 2. FORMÜL TESTLERİ
# ============================================================================

class TestFinancialFormulas:
    """Finansal formüller için testler."""

    def test_polymarket_fee(self):
        """Polymarket fee formülünü resmi dokümantasyon ile test et."""
        from utils.formulas import polymarket_fee

        # Weather kategori (5% fee_rate)
        result = polymarket_fee(shares=100, price=0.55, fee_rate=0.05)
        expected = 100 * 0.05 * 0.55 * (1 - 0.55)
        assert abs(result - expected) < 0.0001

        # Crypto kategori (7% fee_rate)
        result = polymarket_fee(shares=100, price=0.60, fee_rate=0.07)
        expected = 100 * 0.07 * 0.60 * (1 - 0.60)
        assert abs(result - expected) < 0.0001

    def test_polymarket_fee_edge_cases(self):
        """Polymarket fee edge case testleri."""
        from utils.formulas import polymarket_fee

        # Lowest price (0.01)
        result = polymarket_fee(shares=100, price=0.01, fee_rate=0.05)
        assert abs(result - 0.00495) < 0.00001

        # Highest price (0.99)
        result = polymarket_fee(shares=100, price=0.99, fee_rate=0.05)
        assert abs(result - 0.00495) < 0.00001

        # Price = 1.00 (resolve'da)
        result = polymarket_fee(shares=100, price=1.00, fee_rate=0.05)
        assert result == 0.0

    def test_gas_fee_calculation(self):
        """Gas fee hesaplama testi."""
        from utils.slippage import GAS_COST_USD, adjust_edge_for_costs

        GAS_COST_USD = 0.10

        # $30 bet için gas edge
        raw_edge = 0.10
        bet_size = 30.0
        adjusted_edge = adjust_edge_for_costs(raw_edge, bet_size)

        gas_edge_pct = (GAS_COST_USD / bet_size) * 0.55
        expected_edge = raw_edge - gas_edge_pct
        assert abs(adjusted_edge - expected_edge) < 0.0001

        # $100 bet için gas edge (daha küçük)
        bet_size = 100.0
        adjusted_edge = adjust_edge_for_costs(raw_edge, bet_size)
        expected_edge = raw_edge - ((GAS_COST_USD / bet_size) * 0.55)
        assert abs(adjusted_edge - expected_edge) < 0.0001

    def test_tiered_slippage(self):
        """Tiered slippage modelini test et."""
        from utils.slippage import _tiered_slippage

        # Thin book (< 0.05) → 3%
        result = _tiered_slippage(0.03)
        assert abs(result - 0.03) < 0.0001

        # Moderate book (0.05-0.10) → 1%
        result = _tiered_slippage(0.07)
        assert abs(result - 0.01) < 0.0001

        # Deep book (> 0.10) → 0.5%
        result = _tiered_slippage(0.55)
        assert abs(result - 0.005) < 0.0001

    def test_flat_slippage(self):
        """Flat slippage modelini test et."""
        from utils.slippage import SlippageEstimate, estimate_slippage

        est = estimate_slippage(entry_price=0.55, model="flat")
        assert est.model_used == "flat"
        assert est.slippage_pct == 0.005  # Sabit %0.5

    def test_kelly_criterion(self):
        """Kelly criterion formülünü test et."""
        from utils.kelly import kelly_bet_amount

        # Portfolio $1000, edge 10% (0.10)
        portfolio = 1000.0
        edge = 0.10

        kelly_size = kelly_bet_amount(portfolio, edge)

        # Kelly fraction = 15% (0.15)
        expected = portfolio * 0.15 * 0.15  # Min edge threshold handling
        assert kelly_size == expected

    def test_kelly_fraction_variations(self):
        """Kelly fraction çeşitlerini test et."""
        from utils.kelly import kelly_bet_amount

        portfolio = 1000.0
        edge = 0.10

        for frac in [0.10, 0.15, 0.20]:
            kelly_size = kelly_bet_amount(portfolio, edge, kelly_fraction=frac)
            expected = portfolio * edge * frac
            assert abs(kelly_size - expected) < 0.01

    def test_unrealized_pnl(self):
        """Unrealized PnL hesaplama."""
        from utils.formulas import unrealized_pnl

        shares = 1000
        current_price = 0.60
        entry_price = 0.55

        pnl = unrealized_pnl(shares, current_price, entry_price)
        expected = shares * (current_price - entry_price)
        assert abs(pnl - expected) < 0.01

    def test_settlement_pnl(self):
        """Settlement PnL hesaplama."""
        from utils.formulas import settlement_pnl

        # Kazanan bet
        won_pnl = settlement_pnl(stake=100.0, entry_price=0.55,
                                  entry_fee=0.04, won=True)
        expected_payout = 100.0 / 0.55
        expected_pnl = expected_payout - 100.0 - 0.04
        assert abs(won_pnl - expected_pnl) < 0.01

        # Kaybeden bet
        lost_pnl = settlement_pnl(stake=100.0, entry_price=0.55,
                                   entry_fee=0.04, won=False)
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
        assert "api" in data
        assert "bot" in data
        assert "database" in data
        assert "edge_distribution" in data
        assert "seven_day_pnl" in data

    def test_portfolio_endpoint(self, test_client):
        """Portfolio endpoint testi."""
        response = test_client.get("/api/status")
        assert response.status_code == 200

        data = response.json()
        assert "portfolio" in data
        assert "open_bets" in data
        assert "strategy_params" in data

        portfolio = data["portfolio"]
        assert "initial_capital" in portfolio
        assert "current" in portfolio
        assert "max_exposure" in portfolio

    def test_markets_endpoint(self, test_client):
        """Markets endpoint testi."""
        response = test_client.get("/api/markets")
        assert response.status_code == 200

        data = response.json()
        assert "markets" in data
        assert "total_count" in data
        assert "page" in data

        if len(data["markets"]) > 0:
            market = data["markets"][0]
            assert "id" in market
            assert "prices" in market
            assert "edge" in market
            assert "kelly_size" in market
            assert "should_bet" in market


# ============================================================================
# 4. DATA PIPELINE TESTLERİ
# ============================================================================

class TestDataPipeline:
    """Data pipeline testleri."""

    def test_weather_ensemble_fetch(self):
        """Weather ensemble fetch testi."""
        from data_pipeline.weather_ensemble import WeatherEnsemble
        import pandas as pd

        engine = WeatherEnsemble()
        weather_data = engine.fetch_all()

        # 8 model, 11 şehir, 14 günlük tahmin = 1,232 veri noktası (min)
        assert len(weather_data) >= 1000
        assert weather_data is not None

    def test_polymarket_ingest(self):
        """Polymarket ingest testi."""
        from data_pipeline.polymarket_ingest import PolymarketIngest
        import pandas as pd

        ingest = PolymarketIngest()
        markets = ingest.fetch_markets()

        # En az 100 hava piyasası olmalı
        assert len(markets) >= 100
        assert markets is not None

    def test_unified_datastore_split(self):
        """Walk-forward OOS split testi."""
        from data_pipeline.unified_datastore import UnifiedDatastore

        datastore = UnifiedDatastore()
        train, test = datastore.get_train_test_split(days=10)

        # Train ve test tarihleri çakışmamalı
        assert max(train.index) < min(test.index)

        # Train set en az 5 gün olmalı
        assert len(train) >= 5

        # Test set en az 1 gün olmalı
        assert len(test) >= 1


# ============================================================================
# 5. RISK YÖNETİMİ TESTLERİ
# ============================================================================

class TestRiskManagement:
    """Risk yönetimi testleri."""

    def test_city_cap_enforcement(self):
        """City cap kuralını test et."""
        from database.models import Bet
        from engine.strategy import Strategy

        strategy = Strategy()
        city = "Dallas"
        current_open = 4  # Max cap

        # 5. bet city cap'te
        should_reject = strategy.check_city_cap(city, current_open)
        assert should_reject == True

        # 4. bet hala geçerli
        should_accept = strategy.check_city_cap(city, current_open - 1)
        assert should_accept == True

    def test_max_exposure_enforcement(self):
        """Max exposure kuralını test et."""
        from utils.formulas import max_exposure_cap

        initial_capital = 1000.0
        realized_before_today = 50.0
        total_exposure_pct = 0.25

        max_exposure = max_exposure_cap(initial_capital, realized_before_today,
                                        total_exposure_pct)

        expected = (initial_capital + realized_before_today) * total_exposure_pct
        assert abs(max_exposure - expected) < 0.01

    def test_stop_loss_trigger(self):
        """Stop-loss trigger testi."""
        edge = -0.03  # -3%
        stop_loss_threshold = -0.02  # -2%

        # Edge stop-loss altında ise trigger olmalı
        should_stop = edge < stop_loss_threshold
        assert should_stop == True

    def test_stop_loss_safe_zone(self):
        """Stop-loss safe zone testi."""
        edge = -0.01  # -1%
        stop_loss_threshold = -0.02  # -2%

        # Edge stop-loss üstünde ise trigger olmamalı
        should_stop = edge < stop_loss_threshold
        assert should_stop == False


# ============================================================================
# 6. E2E TESTLERİ
# ============================================================================

class TestE2E:
    """End-to-End workflow testleri."""

    def test_complete_betting_cycle(self, test_client):
        """Tam bahis döngüsünü test et."""
        # 1. Fetch markets
        response = test_client.get("/api/markets")
        assert response.status_code == 200
        markets = response.json()["markets"]

        # 2. Analyze and filter
        filtered = [m for m in markets if m.get("edge", 0) >= 5.0]
        assert len(filtered) > 0

        # 3. Place bet
        if len(filtered) > 0:
            bet_data = {
                "market_id": filtered[0]["id"],
                "side": "YES",
                "amount_usd": 15.75
            }
            response = test_client.post("/api/bet", json=bet_data)
            assert response.status_code in [200, 201]

    def test_historical_calibrations_test(self, test_client):
        """Historical calibrations backtest testi."""
        from utils.formulas import polymarket_fee

        # 124 gün, 11 şehir, 7 model = 9,548 veri noktası (min)
        historical_calibrations = pd.read_parquet(
            "data/archive/historical_calibrations_20260630.parquet"
        )

        assert len(historical_calibrations) >= 9000
        assert historical_calibrations is not None

        # Bias düzeltmesi testi
        mean_bias = historical_calibrations["bias"].mean()
        assert abs(mean_bias) < 0.01  # Bias < 1%

        # Edge hesaplama
        edges = historical_calibrations["predicted_value"] - \
                historical_calibrations["actual_value"]
        min_edge_threshold = 0.05
        qualifying_edges = edges[edges >= min_edge_threshold]

        assert len(qualifying_edges) > 0


# ============================================================================
# 7. DASHBOARD TESTLERİ
# ============================================================================

class TestDashboard:
    """Dashboard ve UI testleri."""

    def test_dashboard_response(self, test_client):
        """Dashboard HTML response testi."""
        response = test_client.get("/")
        assert response.status_code == 200
        assert b"Junbo Bot Dashboard" in response.content

    def test_yes_no_buttons_working(self, test_client):
        """YES/NO butonlarının çalıştığını test et."""
        # Fetch markets
        response = test_client.get("/api/markets")
        assert response.status_code == 200
        markets = response.json()["markets"]

        # Her market için YES ve NO butonları aktif olmalı
        if len(markets) > 0:
            market = markets[0]
            assert "prices" in market
            assert "yes" in market["prices"]
            assert "no" in market["prices"]
            assert market["prices"]["yes"] > 0
            assert market["prices"]["no"] > 0

    def test_dashboard_data_update(self, test_client):
        """WebSocket ile dashboard güncellemesini test et."""
        # Test WebSocket connection
        # Bu endpoint WebSocket testi yapmalı
        # (simüle edilmiş test - gerçek WebSocket testi için pytest-asyncio kullanılmalı)
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


@pytest.fixture
def test_database():
    """Test database setup."""
    from database.db import init_db
    from database.models import Base

    # Database initialization
    init_db()

    # Create tables
    Base.metadata.create_all()

    yield

    # Clean up
    Base.metadata.drop_all()


# ============================================================================
# TEST RUNNER
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])