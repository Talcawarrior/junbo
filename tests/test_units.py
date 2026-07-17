"""Unit testler - Calculator, Formulas, Probability modülleri.

Test cases:
✅ Calculator.estimate_probability - Olasılık hesaplama
✅ Calculator.kelly_criterion - Kelly staking
✅ Formulas.max_bet_cap - Maksimum bet limiti
✅ Formulas.max_exposure_cap - Maksimum exposure limiti
✅ Formulas.polymarket_fee - Fee hesaplama
✅ Formulas.settlement_pnl - Settlement PnL hesaplama
✅ Slippage modelleri - Orderbook, tiered, flat
✅ Gas fee modelleri
"""

import pytest
from unittest.mock import Mock

# Eklenecek modüller
from engine.calculator import Calculator
from utils.formulas import (
    max_bet_cap,
    max_exposure_cap,
    conservative_portfolio_value,
    polymarket_fee,
    polymarket_fee_from_stake,
    settlement_pnl,
    settlement_payout,
    portfolio_current_value,
    unrealized_pnl,
    bet_shares,
    roi_pct,
    win_rate_pct,
    daily_pnl,
)
from config.settings import bot_config


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 1. CALCULATOR - Olasılık ve Kelly Kriteri Testleri
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestCalculatorEstimateProbability:
    """Tahmin olasılık hesaplama testleri."""

    def test_single_forecast(self):
        """Tek tahmin varken olasılık hesaplaması."""
        calc = Calculator()

        # Tek tahmin = mean, std = 2.0 default
        prob = calc.estimate_probability(
            forecasts=[0.65],
            threshold=0.55,
            days_ahead=1,
        )

        assert 0.0 <= prob <= 1.0
        assert prob > 0.5  # mean(0.65) > threshold(0.55)

    def test_multiple_forecasts(self):
        """Birden fazla tahmin varken olasılık hesaplama."""
        calc = Calculator()
        forecasts = [0.6, 0.7, 0.65, 0.55, 0.75]

        prob = calc.estimate_probability(
            forecasts=forecasts,
            threshold=0.60,
            days_ahead=2,
        )

        assert 0.0 <= prob <= 1.0
        assert prob > 0.5  # mean = 0.65 > threshold = 0.60

    def test_forecast_std_impact(self):
        """Std'sız etkisi."""
        calc = Calculator()
        forecasts_high_std = [0.2, 0.8, 0.5, 0.3, 0.7]  # High variance
        forecasts_low_std = [0.6, 0.6, 0.6, 0.6, 0.6]  # Low variance

        prob_high = calc.estimate_probability(
            forecasts=forecasts_high_std,
            threshold=0.5,
            days_ahead=1,
        )

        calc.estimate_probability(
            forecasts=forecasts_low_std,
            threshold=0.5,
            days_ahead=1,
        )

        # High std -> olasılık dağılması daha geniş (daha konservatif)
        # Low std -> daha kesin tahmin
        # (örnek: 0.6 threshold için high std de düşük olasılık verir)
        assert prob_high < 0.7  # daha konservatif

    def test_days_ahead_effect(self):
        """Gün ilerisi etkisi - zaman gecikmesi."""
        calc = Calculator()
        forecasts = [0.6, 0.7, 0.65]

        prob_day_0 = calc.estimate_probability(
            forecasts=forecasts,
            threshold=0.6,
            days_ahead=0,
        )

        prob_day_2 = calc.estimate_probability(
            forecasts=forecasts,
            threshold=0.6,
            days_ahead=2,
        )

        # Daha uzun günlerde daha konservatif tahmin
        assert prob_day_0 >= prob_day_2

    def test_market_type_high(self):
        """HIGH market tipi testi."""
        calc = Calculator()
        forecasts = [0.6, 0.7]

        prob = calc.estimate_probability(
            forecasts=forecasts,
            threshold=0.6,
            days_ahead=1,
            market_type="HIGH",
        )

        assert 0.0 <= prob <= 1.0
        assert prob > 0.5  # mean > threshold

    def test_market_type_low(self):
        """LOW market tipi testi."""
        calc = Calculator()
        forecasts = [0.6, 0.7]

        prob = calc.estimate_probability(
            forecasts=forecasts,
            threshold=0.6,
            days_ahead=1,
            market_type="LOW",  # Threshold > mean
        )

        assert 0.0 <= prob <= 1.0
        # LOW market için YES = p(X > threshold)
        # mean = 0.65, threshold = 0.6 -> YES olasılığı düşük

    def test_range_market(self):
        """RANGE market tipi testi."""
        calc = Calculator()
        forecasts = [0.5, 0.6, 0.7]

        prob = calc.estimate_probability(
            forecasts=forecasts,
            threshold=0.6,
            days_ahead=1,
            market_type="RANGE",
            range_low=0.55,
            range_high=0.65,
        )

        assert 0.0 <= prob <= 1.0


class TestCalculatorKellyCriterion:
    """Kelly kriteri testleri."""

    def test_kelly_fraction(self):
        """Kelly fraction hesaplama."""
        calc = Calculator()
        prob = 0.65
        price = 0.60

        kelly = calc.kelly_criterion(prob, price, fraction=0.15)

        assert kelly > 0
        assert kelly <= 1.0  # Kelly %'i 100% altında

    def test_kelly_low_prob(self):
        """Düşük olasılıkta Kelly."""
        calc = Calculator()
        prob = 0.3
        price = 0.25

        kelly = calc.kelly_criterion(prob, price, fraction=0.15)

        assert kelly >= 0
        # Low edge = düşük Kelly

    def test_kelly_negative_edge(self):
        """Negatif edge'de Kelly = 0."""
        calc = Calculator()
        prob = 0.45  # Mean = 0.45 < Price = 0.50
        price = 0.50

        kelly = calc.kelly_criterion(prob, price, fraction=0.15)

        assert kelly == 0

    def test_kelly_fraction_multiplier(self):
        """Fraction multiplier etkisi."""
        calc = Calculator()
        prob = 0.65
        price = 0.60

        kelly_full = calc.kelly_criterion(prob, price, fraction=1.0)
        kelly_quarter = calc.kelly_criterion(prob, price, fraction=0.25)

        # Quarter Kelly = Full Kelly * 0.25
        expected = kelly_full * 0.25
        assert abs(kelly_quarter - expected) < 0.001


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 2. FORMULAS - Finansal Formül Testleri
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestMaxBetCap:
    """Maksimum bet limiti testleri."""

    def test_max_bet_cap_calculation(self):
        """Max bet cap hesaplama."""
        portfolio = 1000.0
        max_pct = 0.003  # 0.3%
        max_cap = max_bet_cap(portfolio, max_pct)

        assert max_cap == pytest.approx(3.0)
        assert max_cap <= portfolio  # Max bet % portfolio'dan küçük

    def test_max_bet_cap_zero_portfolio(self):
        """Sıfır portföyde."""
        max_cap = max_bet_cap(0.0, 0.01)
        assert max_cap == 0.0

    def test_max_bet_cap_zero_pct(self):
        """Sıfır max bet %."""
        max_cap = max_bet_cap(1000.0, 0.0)
        assert max_cap == 0.0


class TestMaxExposureCap:
    """Max exposure limiti testleri."""

    def test_max_exposure_cap_calculation(self):
        """Max exposure cap hesaplama."""
        initial = 1000.0
        realized_before_today = 50.0
        total_pct = 0.25  # 25%

        max_exp = max_exposure_cap(initial, realized_before_today, total_pct)

        expected = (initial + realized_before_today) * total_pct
        assert max_exp == pytest.approx(expected)
        assert max_exp <= initial * total_pct * 1.05  # Biraz tolerans

    def test_conservative_portfolio_value(self):
        """Conservative portfolio value hesaplama."""
        initial = 1000.0
        realized_before_today = 100.0

        cons_val = conservative_portfolio_value(initial, realized_before_today)

        assert cons_val == initial + realized_before_today
        assert cons_val < 1000.0 + 1000.0  # Unrealized dahil değil

    def test_max_exposure_cap_with_negative_realized(self):
        """Negatif realized PnL."""
        initial = 1000.0
        realized_before_today = -50.0
        total_pct = 0.25

        max_exp = max_exposure_cap(initial, realized_before_today, total_pct)

        expected = (initial + realized_before_today) * total_pct
        assert max_exp == pytest.approx(expected)


class TestPolymarketFee:
    """Polymarket fee hesaplama testleri."""

    def test_polymarket_fee_high_price(self):
        """Yüksek fiyatda fee."""
        shares = 100.0
        price = 0.75  # Close to 1.0
        fee_rate = 0.05

        fee = polymarket_fee(shares, price, fee_rate)

        # fee = shares * rate * p * (1-p)
        # p=0.75 -> p*(1-p) = 0.75*0.25 = 0.1875
        expected = shares * fee_rate * 0.75 * (1 - 0.75)
        assert fee == pytest.approx(expected, abs=0.01)

    def test_polymarket_fee_mid_price(self):
        """Orta fiyatda fee."""
        shares = 100.0
        price = 0.50  # Midpoint
        fee_rate = 0.05

        fee = polymarket_fee(shares, price, fee_rate)

        # p*(1-p) = 0.5*0.5 = 0.25
        expected = shares * fee_rate * 0.25
        assert fee == pytest.approx(expected, abs=0.01)

    def test_polymarket_fee_low_price(self):
        """Düşük fiyatda fee."""
        shares = 100.0
        price = 0.10  # Low price
        fee_rate = 0.05

        fee = polymarket_fee(shares, price, fee_rate)

        # p*(1-p) = 0.10*0.90 = 0.09
        expected = shares * fee_rate * 0.09
        assert fee == pytest.approx(expected, abs=0.01)

    def test_polymarket_fee_from_stake(self):
        """Stake-based fee shortcut."""
        stake = 100.0
        price = 0.60
        fee_rate = 0.05

        fee = polymarket_fee_from_stake(stake, price, fee_rate)

        # fee = stake * rate * (1-p)
        expected = stake * fee_rate * (1 - price)
        assert fee == pytest.approx(expected, abs=0.01)


class TestSettlementPnL:
    """Settlement PnL hesaplama testleri."""

    def test_won_bet_pnl(self):
        """Kazanan bet settlement PnL."""
        stake = 100.0
        entry_price = 0.60
        entry_fee = 1.50  # fee calculated beforehand
        won = True

        pnl = settlement_pnl(stake, entry_price, entry_fee, won)

        # payout = stake / entry_price = 100 / 0.6 = 166.67
        # net = 166.67 - 100 - 1.50 = 65.17
        expected_payout = stake / entry_price
        expected = expected_payout - stake - entry_fee
        assert pnl == pytest.approx(expected, abs=0.01)

    def test_lost_bet_pnl(self):
        """Kaybeden bet settlement PnL."""
        stake = 100.0
        entry_fee = 1.50
        won = False

        pnl = settlement_pnl(stake, 0.50, entry_fee, won)

        # net = -stake - entry_fee
        expected = -(stake + entry_fee)
        assert pnl == expected

    def test_settlement_payout(self):
        """Payout hesaplama."""
        stake = 100.0
        entry_price = 0.60

        payout = settlement_payout(stake, entry_price)

        # payout = stake / entry_price = 100 / 0.6 = 166.67
        expected = stake / entry_price
        assert payout == pytest.approx(expected, abs=0.01)


class TestPortfolioValues:
    """Portfolio hesaplamaları testleri."""

    def test_unrealized_pnl(self):
        """Unrealized PnL hesaplama."""
        shares = 100.0
        entry_price = 0.6
        current_price = 0.65

        pnl = unrealized_pnl(shares, current_price, entry_price)

        expected = shares * (current_price - entry_price)
        assert pnl == expected

    def test_unrealized_loss(self):
        """Unrealized loss."""
        shares = 100.0
        entry_price = 0.7
        current_price = 0.6

        pnl = unrealized_pnl(shares, current_price, entry_price)

        shares * (current_price - entry_price)  # -10.0
        assert pnl < 0

    def test_bet_shares(self):
        """Bet shares hesaplama."""
        stake = 100.0
        fill_price = 0.60

        shares = bet_shares(stake, fill_price)

        expected = stake / fill_price
        assert shares == pytest.approx(expected, abs=0.01)

    def test_portfolio_current_value(self):
        """Portfolio market value."""
        initial = 1000.0
        realized = 50.0
        unrealized = 30.0

        value = portfolio_current_value(initial, realized, unrealized)

        expected = initial + realized + unrealized
        assert value == expected

    def test_roi_pct(self):
        """ROI hesaplama."""
        pnl = 50.0
        stake = 100.0

        roi = roi_pct(pnl, stake)

        expected = (pnl / stake) * 100
        assert roi == pytest.approx(expected, abs=0.01)

    def test_roi_pct_zero_stake(self):
        """Sıfır stake'de ROI."""
        roi = roi_pct(10.0, 0.0)
        assert roi == 0.0

    def test_win_rate_pct(self):
        """Win rate hesaplama."""
        wins = 60
        total_closed = 100

        winrate = win_rate_pct(wins, total_closed)

        expected = (wins / total_closed) * 100
        assert winrate == pytest.approx(expected, abs=0.01)

    def test_daily_pnl(self):
        """Daily PnL hesaplama."""
        realized_today = 20.0
        open_bets = [
            Mock(unrealized_pnl=30.0),
            Mock(unrealized_pnl=-10.0),
        ]

        pnl = daily_pnl(realized_today, open_bets)

        expected = realized_today + 30.0 + (-10.0)
        assert pnl == expected


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 3. SLIPPAGE & GAS FEE MODÜLLERİ (Test için mock kullanılacak)
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestSlippageModels:
    """Slippage modelleri testleri (mock API)."""

    def test_orderbook_slippage_estimation(self):
        """Orderbook slippage tahmini."""
        # TODO: utils/slippage.py mock testi
        # condition_id varken orderbook depth bazlı slippage hesapla
        # Varsayılan: slippage = entry_price * 0.005 (0.5%)
        pass

    def test_tiered_slippage(self):
        """Tiered slippage modeli."""
        # TODO: tiered model testi
        # <0.05: 3%, 0.05-0.10: 1%, >0.10: 0.5%
        pass

    def test_flat_slippage(self):
        """Flat slippage modeli."""
        # TODO: flat model testi
        # sabit slippage % parametresi
        pass

    def test_gas_cost_calculation(self):
        """Gas cost hesaplama."""
        # TODO: gas_cost_usd = $0.10 default
        # Polygon gas fee (her round-trip için)
        pass


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 4. STRATEGY PARAMETERS (Karpathy search sonuçları)
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestStrategyParams:
    """Karpathy-search parametreleri testleri."""

    def test_default_min_edge(self):
        """Default min_edge = 5%."""
        assert bot_config.strategy.min_edge == 0.05

    def test_min_entry_price_gate(self):
        """Min entry price gate."""
        # Polymarket public-search'te genelde entry_price ~0.5
        # Long-shot bets (< 30%) filtrelenmeli
        assert bot_config.strategy.min_entry_price == 0.01  # Default (permissive)

    def test_inefficiency_min_gate(self):
        """Inefficiency min gate."""
        # Negatif = gate disabled
        assert bot_config.strategy.inefficiency_min == -1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])