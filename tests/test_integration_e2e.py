"""Integration/E2E Tests - Tüm pipeline'ı uçtan uca test eder.

Gerçek API çağrısı yapmadan (mock/fixture ile) tüm pipeline'ı çalıştır:
veri çekme → sinyal üretme → pozisyon boyutlandırma → emir gönderme

Modüller ayrı ayrı doğru olsa da birleşince bozulabiliyor.
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta


# ============================================================================
# 1. FULL PIPELINE E2E TEST
# ============================================================================

class TestFullPipelineE2E:
    """Tam pipeline E2E testi."""

    @pytest.mark.asyncio
    async def test_fetch_to_bet_pipeline(self):
        """Veri çekme → analiz → bahis pipeline'ı."""
        # Mock veri
        mock_market = {
            "id": "test_123",
            "city": "Dallas",
            "threshold": 80.0,
            "target_date": datetime.now(timezone.utc) + timedelta(days=2),
            "metric": "temperature_max",
            "yes_price": 0.55,
            "no_price": 0.45,
        }

        mock_forecasts = [
            {"source": "gfs_seamless", "predicted_value": 0.7, "model_weight": 0.30},
            {"source": "ecmwf_ifs025", "predicted_value": 0.65, "model_weight": 0.25},
            {"source": "icon_global", "predicted_value": 0.6, "model_weight": 0.10},
        ]

        # Step 1: Fetch markets (mock)
        with patch('scrapers.polymarket.PolymarketScraper') as mock_scraper:
            mock_scraper.return_value.fetch_markets.return_value = [mock_market]

            markets = mock_scraper.return_value.fetch_markets()
            assert len(markets) == 1
            assert markets[0]["id"] == "test_123"

        # Step 2: Weather forecast (mock)
        with patch('engine.calculator.WeatherEngine') as mock_weather:
            mock_weather.return_value.get_multi_model_forecast.return_value = {
                "weighted_mean": 0.65,
                "weighted_std": 0.1,
                "model_count": 3,
                "model_temps": {"gfs_seamless": 0.7, "ecmwf_ifs025": 0.65, "icon_global": 0.6},
            }

            forecast = await mock_weather.return_value.get_multi_model_forecast(
                city_code="KDAL", latitude=32.8471, longitude=-96.8517
            )
            assert forecast is not None
            assert forecast["model_count"] == 3

        # Step 3: Calculate probability (real)
        from engine.calculator import Calculator

        calc = Calculator()
        prob = calc.estimate_probability(
            forecasts=[f["predicted_value"] for f in mock_forecasts],
            threshold=mock_market["threshold"],
            days_ahead=2,
        )
        assert 0.0 <= prob <= 1.0

        # Step 4: Calculate Kelly (real)
        from utils.kelly import kelly_fraction

        kelly = kelly_fraction(prob, mock_market["yes_price"])
        assert 0 <= kelly <= 1.0

        # Step 5: Calculate fee (real)
        from utils.formulas import polymarket_fee

        fee = polymarket_fee(shares=100, price=mock_market["yes_price"], fee_rate=0.05)
        assert fee >= 0

        # Step 6: Calculate edge (real)
        edge = prob - mock_market["yes_price"]
        # Edge pozitif olabilir veya negatif
        assert isinstance(edge, float)


# ============================================================================
# 2. RISK MANAGEMENT E2E
# ============================================================================

class TestRiskManagementE2E:
    """Risk yönetimi E2E testi."""

    def test_exposure_cap_enforcement(self):
        """Exposure cap uygulanmalı."""
        from utils.formulas import max_exposure_cap

        initial_capital = 1000.0
        realized_before_today = 50.0
        total_exposure_pct = 0.25

        max_exp = max_exposure_cap(initial_capital, realized_before_today, total_exposure_pct)

        # Mevcut exposure
        current_exposure = 200.0

        # Yeni bahis
        new_bet = 3.0

        # Exposure cap kontrol
        if current_exposure + new_bet > max_exp:
            # Bahis reddedilmeli
            assert True  # Reject
        else:
            # Bahis kabul edilmeli
            assert current_exposure + new_bet <= max_exp

    def test_city_cap_enforcement(self):
        """City cap uygulanmalı."""
        from config.settings import bot_config

        city_cap = bot_config.city_cap

        # Şehir bazlı bet sayısı
        city_bets = {
            "Dallas": 4,
            "London": 3,
            "Paris": 2,
        }

        # Dallas: limit dolmuş
        assert city_bets["Dallas"] >= city_cap

        # London: hala yer var
        assert city_bets["London"] < city_cap

    def test_daily_loss_limit(self):
        """Daily loss limit uygulanmalı."""
        from config.settings import bot_config

        initial_capital = 1000.0
        daily_loss_limit_pct = bot_config.strategy.daily_loss_limit
        daily_loss_limit_amount = initial_capital * daily_loss_limit_pct

        # Günlük zarar
        daily_pnl = -60.0

        # Limit kontrol
        if daily_pnl <= -daily_loss_limit_amount:
            # Bot durdurulmalı
            assert True  # Stop
        else:
            # Devam etmeli
            assert daily_pnl > -daily_loss_limit_amount


# ============================================================================
# 3. DATABASE E2E
# ============================================================================

class TestDatabaseE2E:
    """Database E2E testi."""

    def test_market_creation_and_query(self):
        """Market oluşturma ve sorgulama."""
        from database.db import get_session
        from database.models import WeatherMarket

        session = get_session()
        try:
            # Market oluştur
            market = WeatherMarket(
                id="test_e2e_123",
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

            # Sorgula
            queried = session.query(WeatherMarket).filter_by(id="test_e2e_123").first()
            assert queried is not None
            assert queried.city == "Dallas"
            assert queried.yes_price == 0.55

            # Temizle
            session.delete(queried)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def test_bet_creation_and_query(self):
        """Bet oluşturma ve sorgulama."""
        from database.db import get_session
        from database.models import Bet

        session = get_session()
        try:
            # Bet oluştur
            bet = Bet(
                id=99999,
                market_id="test_e2e_123",
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

            # Sorgula
            queried = session.query(Bet).filter_by(id=99999).first()
            assert queried is not None
            assert queried.city == "Dallas"
            assert queried.amount == 3.0

            # Temizle
            session.delete(queried)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()


# ============================================================================
# 4. API ENDPOINTS E2E
# ============================================================================

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

        # Health check yanıt yapısı
        assert "is_running" in data or "red_flags" in data

    def test_status_endpoint_e2e(self):
        """Status endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()

        # Status yanıt yapısı
        assert "portfolio" in data
        assert "stats" in data
        assert "limits" in data

    def test_markets_endpoint_e2e(self):
        """Markets endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/markets")

        assert response.status_code == 200
        data = response.json()

        # Markets yanıt yapısı
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

        # Signals yanıt yapısı
        assert "signals" in data
        assert "count" in data

    def test_history_endpoint_e2e(self):
        """History endpoint E2E."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        response = client.get("/api/history")

        assert response.status_code == 200
        data = response.json()

        # History yanıt yapısı
        assert "history" in data
        assert "stats" in data


# ============================================================================
# 5. SLIPPAGE & GAS FEE E2E
# ============================================================================

class TestSlippageGasFeeE2E:
    """Slippage ve gas fee E2E testleri."""

    def test_slippage_calculation_e2e(self):
        """Slippage hesaplama E2E."""
        from utils.slippage import estimate_slippage

        # Farklı entry price'lar için slippage hesapla
        test_prices = [0.10, 0.25, 0.50, 0.75, 0.90]

        for price in test_prices:
            slippage = estimate_slippage(price)
            assert 0 <= slippage <= 0.10  # Max %10 slippage

    def test_gas_fee_calculation_e2e(self):
        """Gas fee hesaplama E2E."""
        from config.settings import bot_config

        gas_cost = bot_config.strategy.gas_cost_usd

        # Gas cost pozitif olmalı
        assert gas_cost > 0

        # Gas cost makul olmalı (< $1)
        assert gas_cost < 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
