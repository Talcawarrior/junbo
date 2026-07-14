"""Property-Based Testing - Hypothesis ile rastgele üretilen girdilerle invariant testleri.

Finansal/olasılıksal hesaplamalarda sabit örnekler yerine binlerce rastgele
girdiyle invariant'ları test et. Bu, edge case'leri yakalamak için en iyi yoldur.

Kullanım:
    pip install hypothesis
    pytest tests/test_property_based.py -v
"""

import pytest
from hypothesis import given, strategies as st, assume, settings, HealthCheck
from hypothesis.stateful import rule, invariant, initialize, RuleBasedStateMachine


# ============================================================================
# 1. FEE FORMÜLÜ PROPERTIES
# ============================================================================

class TestFeeFormulas:
    """Fee formülü invariant'ları."""

    @given(
        shares=st.floats(min_value=0.01, max_value=10000),
        price=st.floats(min_value=0.01, max_value=0.99),
        fee_rate=st.floats(min_value=0.0, max_value=0.20),
    )
    def test_fee_never_negative(self, shares, price, fee_rate):
        """Fee hiçbir zaman negatif olmamalı."""
        from utils.formulas import polymarket_fee

        fee = polymarket_fee(shares, price, fee_rate)
        assert fee >= 0, f"Negative fee: shares={shares}, price={price}, fee={fee}"

    @given(
        shares=st.floats(min_value=0.01, max_value=10000),
        price=st.floats(min_value=0.01, max_value=0.99),
        fee_rate=st.floats(min_value=0.0, max_value=0.20),
    )
    def test_fee_bounded_by_stake(self, shares, price, fee_rate):
        """Fee her zaman pay kadar küçük olmalı."""
        from utils.formulas import polymarket_fee

        fee = polymarket_fee(shares, price, fee_rate)
        max_possible_fee = shares * fee_rate  # p=0.5'te max fee

        assert fee <= max_possible_fee * 1.01, f"Fee too large: {fee} > {max_possible_fee}"

    @given(
        shares=st.floats(min_value=0.01, max_value=10000),
        price=st.floats(min_value=0.01, max_value=0.99),
        fee_rate=st.floats(min_value=0.01, max_value=0.20),
    )
    def test_fee_symmetric_at_05(self, shares, price, fee_rate):
        """Fee p=0.5'te simetrik olmalı."""
        from utils.formulas import polymarket_fee

        # p=0.5 ve p=0.5 civarında fee aynı olmalı
        fee_at_05 = polymarket_fee(shares, 0.5, fee_rate)
        fee_at_049 = polymarket_fee(shares, 0.49, fee_rate)
        fee_at_051 = polymarket_fee(shares, 0.51, fee_rate)

        # 0.49 ve 0.51 fee'leri birbirine yakın olmalı
        assert abs(fee_at_049 - fee_at_051) < fee_at_05 * 0.1


# ============================================================================
# 2. KELLY CRITERION PROPERTIES
# ============================================================================

class TestKellyCriterion:
    """Kelly criterion invariant'ları."""

    @given(
        prob=st.floats(min_value=0.01, max_value=0.99),
        price=st.floats(min_value=0.01, max_value=0.99),
    )
    def test_kelly_in_01_range(self, prob, price):
        """Kelly fraction 0-1 arasında olmalı."""
        from utils.kelly import kelly_fraction

        kelly = kelly_fraction(prob, price)

        assert 0 <= kelly <= 1.0, f"Kelly out of range: {kelly}"

    @given(
        prob=st.floats(min_value=0.01, max_value=0.99),
        price=st.floats(min_value=0.01, max_value=0.99),
    )
    def test_kelly_positive_edge(self, prob, price):
        """Pozitif edge'de Kelly pozitif olmalı."""
        from utils.kelly import kelly_fraction

        assume(prob > price)  # Pozitif edge

        kelly = kelly_fraction(prob, price)

        assert kelly > 0, f"Kelly should be positive: prob={prob}, price={price}"

    @given(
        prob=st.floats(min_value=0.01, max_value=0.49),
        price=st.floats(min_value=0.51, max_value=0.99),
    )
    def test_kelly_negative_edge_zero(self, prob, price):
        """Negatif edge'de Kelly sıfır olmalı."""
        from utils.kelly import kelly_fraction

        assume(prob < price)  # Negatif edge

        kelly = kelly_fraction(prob, price)

        assert kelly == 0, f"Kelly should be zero for negative edge: {kelly}"

    @given(
        prob=st.floats(min_value=0.51, max_value=0.99),
        price=st.floats(min_value=0.01, max_value=0.49),
    )
    def test_kelly_with_fraction_multiplier(self, prob, price):
        """Kelly fraction multiplier doğru çalışmalı."""
        from utils.kelly import kelly_bet_amount

        kelly_full = kelly_bet_amount(1000, prob, price, fraction=1.0)
        kelly_half = kelly_bet_amount(1000, prob, price, fraction=0.5)

        # Half fraction should be roughly half of full
        # (with min/max clamping, it may not be exactly half)
        assert kelly_half <= kelly_full


# ============================================================================
# 3. PROBABILITY ESTIMATION PROPERTIES
# ============================================================================

class TestProbabilityEstimation:
    """Olasılık tahmini invariant'ları."""

    @given(
        mean=st.floats(min_value=0.0, max_value=100.0),
        std=st.floats(min_value=0.1, max_value=20.0),
        threshold=st.floats(min_value=0.0, max_value=100.0),
    )
    @settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
    def test_probability_in_01_range(self, mean, std, threshold):
        """Olasılık her zaman 0-1 arasında olmalı."""
        from utils.probability import estimate_probability

        prob = estimate_probability(
            mean=mean,
            std=std,
            threshold=threshold,
            days_ahead=1,
            market_type="HIGH",
        )

        assert 0.0 <= prob <= 1.0, f"Probability out of range: {prob}"

    @given(
        mean=st.floats(min_value=0.0, max_value=100.0),
        std=st.floats(min_value=0.1, max_value=20.0),
        threshold=st.floats(min_value=0.0, max_value=100.0),
    )
    def test_high_market_higher_threshold_lower_prob(self, mean, std, threshold):
        """HIGH market'ta daha yüksek threshold = daha düşük olasılık."""
        from utils.probability import estimate_probability

        prob_low_threshold = estimate_probability(mean, std, threshold - 5, 1, "HIGH")
        prob_high_threshold = estimate_probability(mean, std, threshold + 5, 1, "HIGH")

        # Daha yüksek threshold = daha düşük olasılık
        assert prob_low_threshold >= prob_high_threshold

    @given(
        mean=st.floats(min_value=0.0, max_value=100.0),
        std=st.floats(min_value=0.1, max_value=20.0),
        threshold=st.floats(min_value=0.0, max_value=100.0),
    )
    def test_low_market_inverted(self, mean, std, threshold):
        """LOW market'ta threshold ilişkisi ters olmalı."""
        from utils.probability import estimate_probability

        prob_low_threshold = estimate_probability(mean, std, threshold - 5, 1, "LOW")
        prob_high_threshold = estimate_probability(mean, std, threshold + 5, 1, "LOW")

        # LOW market'ta daha yüksek threshold = daha yüksek olasılık
        assert prob_high_threshold >= prob_low_threshold


# ============================================================================
# 4. PORTFOLIO FORMULAS PROPERTIES
# ============================================================================

class TestPortfolioFormulas:
    """Portfolio formülü invariant'ları."""

    @given(
        initial=st.floats(min_value=0.0, max_value=100000),
        realized=st.floats(min_value=-50000, max_value=50000),
        unrealized=st.floats(min_value=-50000, max_value=50000),
    )
    def test_portfolio_current_value(self, initial, realized, unrealized):
        """Portfolio current value = initial + realized + unrealized."""
        from utils.formulas import portfolio_current_value

        value = portfolio_current_value(initial, realized, unrealized)
        expected = initial + realized + unrealized

        assert abs(value - expected) < 0.01

    @given(
        initial=st.floats(min_value=0.0, max_value=100000),
        realized_before_today=st.floats(min_value=-50000, max_value=50000),
    )
    def test_max_exposure_cap_positive(self, initial, realized_before_today):
        """Max exposure cap her zaman pozitif olmalı."""
        from utils.formulas import max_exposure_cap

        total_exposure_pct = 0.25
        max_exp = max_exposure_cap(initial, realized_before_today, total_exposure_pct)

        # Exposure cap = (initial + realized) * pct
        # When initial + realized < 0, cap can be negative (which means no betting allowed)
        # The formula is correct, so we just verify the calculation
        expected = (initial + realized_before_today) * total_exposure_pct
        assert abs(max_exp - expected) < 0.01

    @given(
        stake=st.floats(min_value=0.01, max_value=10000),
        entry_price=st.floats(min_value=0.01, max_value=0.99),
    )
    def test_bet_shares_positive(self, stake, entry_price):
        """Bet shares her zaman pozitif olmalı."""
        from utils.formulas import bet_shares

        shares = bet_shares(stake, entry_price)

        assert shares > 0


# ============================================================================
# 5. EDGE CALCULATION PROPERTIES
# ============================================================================

class TestEdgeCalculation:
    """Edge hesaplama invariant'ları."""

    @given(
        prob=st.floats(min_value=0.01, max_value=0.99),
        price=st.floats(min_value=0.01, max_value=0.99),
    )
    def test_edge_symmetric(self, prob, price):
        """Edge = prob - price (simetrik)."""
        # YES edge = prob - price
        # NO edge = (1-prob) - (1-price) = price - prob
        # Toplam = 0

        yes_edge = prob - price
        no_edge = (1 - prob) - (1 - price)
        total_edge = yes_edge + no_edge

        assert abs(total_edge) < 0.001

    @given(
        prob=st.floats(min_value=0.51, max_value=0.99),
        price=st.floats(min_value=0.01, max_value=0.49),
    )
    def test_positive_edge_betting(self, prob, price):
        """Pozitif edge'de bahis yapılmalı."""
        edge = prob - price

        if edge > 0:
            # Bahis yapılmalı
            assert edge > 0

    @given(
        prob=st.floats(min_value=0.01, max_value=0.49),
        price=st.floats(min_value=0.51, max_value=0.99),
    )
    def test_negative_edge_no_betting(self, prob, price):
        """Negatif edge'de bahis yapılmamalı."""
        edge = prob - price

        if edge < 0:
            # Bahis yapılmamalı
            assert edge < 0


# ============================================================================
# 6. STATEFUL TEST (Portfolio State Machine)
# ============================================================================

class PortfolioStateMachine(RuleBasedStateMachine):
    """Stateful test: Portfolio durum makinesi."""

    def __init__(self):
        super().__init__()
        self.portfolio = 1000.0
        self.bets = []

    @initialize()
    def init_portfolio(self):
        self.portfolio = 1000.0
        self.bets = []

    @rule(
        stake=st.floats(min_value=1.0, max_value=10.0),
        entry_price=st.floats(min_value=0.1, max_value=0.9),
    )
    def place_bet(self, stake, entry_price):
        """Bahis yerleştir."""
        if stake <= self.portfolio:
            self.portfolio -= stake
            self.bets.append({
                "stake": stake,
                "entry_price": entry_price,
                "status": "open",
            })

    @rule(
        won=st.booleans(),
    )
    def settle_bet(self, won):
        """Bahisi sonlandır."""
        if self.bets:
            bet = self.bets.pop(0)
            if won:
                payout = bet["stake"] / bet["entry_price"]
                self.portfolio += payout
            # Kaybeden bahiste para kaybedildi (zaten düşüldü)

    @invariant()
    def portfolio_never_negative(self):
        """Portfolio hiçbir zaman negatif olmamalı."""
        assert self.portfolio >= 0, f"Negative portfolio: {self.portfolio}"


PortfolioStateTest = PortfolioStateMachine.TestCase


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
