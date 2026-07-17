"""Comprehensive take-profit / exit-mechanism tests.

These tests call the REAL RiskManager methods — not inline math.
They exist because a format-string bug ({pct:.1%} vs {pct:.1f}%)
prevented take_profit from EVER triggering in production.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from config.settings import bot_config
from engine.strategy import RiskManager


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_risk_manager():
    """Config ile initialized RiskManager."""
    return RiskManager(db_session=None, cfg=bot_config)


def make_mock_bet(**kwargs):
    """RiskManager'ın okuduğu alanları olan mock bet."""
    bet = MagicMock()
    bet.entry_price = kwargs.get("entry_price", 0.50)
    bet.price = kwargs.get("price", 0.50)
    bet.result_data = kwargs.get("result_data", None)
    # Yeni partial-TP alanları: gerçek Bet gibi varsayılan False / 0.0
    bet.partial_tp_done = kwargs.get("partial_tp_done", False)
    bet.covered_fraction = kwargs.get("covered_fraction", 0.0)
    # placed_at: check_early_exit minimum hold kontrolü için
    # Varsayılan olarak 10 dakika önce — minimum hold'u geçer
    bet.placed_at = kwargs.get(
        "placed_at",
        datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    return bet


def make_mock_market(**kwargs):
    """Mock market with target_date."""
    m = MagicMock()
    m.target_date = kwargs.get(
        "target_date",
        datetime.now(timezone.utc) + timedelta(days=2),
    )
    return m


# ── TAKE PROFIT TESTS ────────────────────────────────────────────────────────


class TestTakeProfit:
    """Take-profit mekanizması - bu testler format string bug'ını yakalar."""

    def test_take_profit_at_100_percent(self):
        """Tam %100 kârda tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.25)
        should_exit, reason = rm.check_take_profit(bet, 0.50)
        assert should_exit is True
        assert "take_profit" in reason

    def test_take_profit_above_100_percent(self):
        """%100'ün üstünde tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.25)
        should_exit, reason = rm.check_take_profit(bet, 0.80)
        assert should_exit is True
        assert "take_profit" in reason

    def test_take_profit_below_100_percent(self):
        """%98'de tetiklenmemeli (near_certain_win değil, TP de değil)."""
        rm = make_risk_manager()
        # entry=0.50, current=0.90 → ratio=0.80 < 1.0
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_take_profit(bet, 0.90)
        assert should_exit is False

    def test_take_profit_exactly_at_threshold(self):
        """Tam eşiğe ulaştığında tetiklenmeli."""
        rm = make_risk_manager()
        # entry=0.30, current=0.60 → ratio=1.00
        bet = make_mock_bet(entry_price=0.30)
        should_exit, reason = rm.check_take_profit(bet, 0.60)
        assert should_exit is True

    def test_take_profit_just_below_threshold(self):
        """Eşiğin hemen altında tetiklenmemeli."""
        rm = make_risk_manager()
        # entry=0.30, current=0.599 → ratio=0.9967
        bet = make_mock_bet(entry_price=0.30)
        should_exit, reason = rm.check_take_profit(bet, 0.599)
        assert should_exit is False

    def test_near_certain_win_at_098(self):
        """Fiyat 0.98'e ulaştığında near_certain_win tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.10)  # entry önemli değil
        should_exit, reason = rm.check_take_profit(bet, 0.98)
        assert should_exit is True
        assert "near_certain_win" in reason

    def test_near_certain_win_at_099(self):
        """Fiyat 0.99'da da near_certain_win tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_take_profit(bet, 0.99)
        assert should_exit is True
        assert "near_certain_win" in reason

    def test_real_scenario_tokyo_170_percent(self):
        """Gerçek senaryo: Tokyo entry=0.27, current=0.73 → %170 kâr."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.27)
        should_exit, reason = rm.check_take_profit(bet, 0.73)
        assert should_exit is True
        profit_ratio = (0.73 - 0.27) / 0.27
        assert profit_ratio == pytest.approx(1.7037, abs=0.01)

    def test_real_scenario_chicago_148_percent(self):
        """Gerçek senaryo: Chicago entry=0.29, current=0.72 → %148 kâr."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.29)
        should_exit, reason = rm.check_take_profit(bet, 0.72)
        assert should_exit is True
        profit_ratio = (0.72 - 0.29) / 0.29
        assert profit_ratio == pytest.approx(1.4828, abs=0.01)

    def test_should_not_close_at_48_percent(self):
        """%48 kârda kapanmamalı."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.30)
        should_exit, reason = rm.check_take_profit(bet, 0.444)
        assert should_exit is False

    def test_entry_zero_no_crash(self):
        """entry_price=0'da crash olmamalı."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.0)
        should_exit, reason = rm.check_take_profit(bet, 0.99)
        assert isinstance(should_exit, bool)

    def test_format_string_not_double_multiply(self):
        """Format string %10000 gibi absürt değer göstermemeli.

        Bu test, {pct:.1%} bug'ını yakalar.
        Eğer format string {pct:.1%} kullanılırsa:
            profit_pct(0.73, 0.27) = 170.37
            {170.37:.1%} = '17037.0%' → BU YANLIŞ

        Doğru:
            f"{ratio:.1%}" ratio = 1.7037 → '170.4%'
        """
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.27)
        should_exit, reason = rm.check_take_profit(bet, 0.73)
        assert should_exit is True
        # Partial TP reason shows the SOLD fraction (entry/current = 0.27/0.73 ≈ 37.0%),
        # not the profit %. Key guard: no double-multiply (no "17000%").
        assert "partial_take_profit" in reason
        assert "37.0%" in reason
        assert "17000" not in reason  # Double-multiply would give "17000%"


# ── STOP LOSS TESTS ──────────────────────────────────────────────────────────


class TestStopLoss:
    """Stop-loss mekanizması."""

    def test_stop_loss_at_30_percent(self):
        """Tam %30 zararda tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_stop_loss(bet, 0.35)
        assert should_exit is True
        assert "stop_loss" in reason

    def test_stop_loss_above_threshold(self):
        """%20 zararda tetiklenmemeli (%25 eşiği)."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, _ = rm.check_stop_loss(bet, 0.40)
        assert should_exit is False

    def test_stop_loss_deep_loss(self):
        """%50+ zararda tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, _ = rm.check_stop_loss(bet, 0.20)
        assert should_exit is True


# ── TRAILING STOP TESTS ─────────────────────────────────────────────────────


class TestTrailingStop:
    """Trailing stop mekanizması."""

    def test_trailing_stop_drops_from_peak(self):
        """Tepeden %15+ düşüşte tetiklenmeli."""
        rm = make_risk_manager()
        # peak 0.60'a çıkmış, sonra 0.50'ye düşmüş → %16.7 düşüş
        peak_data = json.dumps({"peak_price": 0.60})
        bet = make_mock_bet(entry_price=0.30, result_data=peak_data)
        should_exit, reason = rm.check_trailing_stop(bet, 0.50)
        assert should_exit is True
        assert "trailing_stop" in reason

    def test_trailing_stop_no_drop(self):
        """Tepeden az düşüşte tetiklenmemeli."""
        rm = make_risk_manager()
        peak_data = json.dumps({"peak_price": 0.60})
        bet = make_mock_bet(entry_price=0.30, result_data=peak_data)
        should_exit, _ = rm.check_trailing_stop(bet, 0.55)
        assert should_exit is False

    def test_trailing_stop_never_profitable(self):
        """Hiç kâra geçmemiş pozisyonda tetiklenmemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data=None)
        should_exit, _ = rm.check_trailing_stop(bet, 0.40)
        assert should_exit is False


# ── STOP LOSS + TAKE PROFIT INTERACTION ──────────────────────────────────────


class TestExitInteraction:
    """Birden fazla exit mekanizmasının etkileşimi."""

    def test_stop_loss_checked_before_take_profit(self):
        """check_early_exit'te stop_loss önce kontrol edilir."""
        rm = make_risk_manager()
        # Hem stop_loss hem take_profit tetiklenebilir (olası durum)
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.30, market)
        # stop_loss tetiklenmeli (%40 zarar)
        assert should_exit is True
        assert "stop_loss" in reason

    def test_take_profit_via_early_exit(self):
        """check_early_exit take_profit'i tetikleyebilmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.20)
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.60, market)
        assert should_exit is True
        assert "take_profit" in reason

    def test_no_exit_when_profitable_but_below_tp(self):
        """Kârda ama TP altında → çıkış olmamalı."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.70, market)
        assert should_exit is False


# ── ALL FORMULAS CONSISTENT ─────────────────────────────────────────────────


class TestFormulaConsistency:
    """Tüm exit check'lerin aynı hesaplama pattern'ini kullandığını doğrula."""

    def test_all_exit_checks_return_bool_str_tuple(self):
        """Tüm exit check'leri (bool, str) tuple döndürmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market()

        checks = [
            rm.check_stop_loss(bet, 0.50, market),
            rm.check_take_profit(bet, 0.50, market),
            rm.check_trailing_stop(bet, 0.50),
            rm.check_time_decay(bet, 0.50, market),
        ]

        for result in checks:
            assert isinstance(result, tuple)
            assert len(result) == 2
            assert isinstance(result[0], bool)
            assert isinstance(result[1], str)

    def test_ratio_based_comparison(self):
        """Tüm exit check'ler ratio (0-1) bazında karşılaştırma yapmalı,
        percentage (0-100) DEĞİL.

        Bu, {pct:.1%} format bug'ının tekrarlanmasını önler:
        Eski hata: profit_pct() %100 ile çarpıyordu, sonra format string
        tekrar çarpıyordu → asla tetiklenmiyordu.
        """
        rm = make_risk_manager()

        # Test: entry=0.10, current=0.20 → ratio=1.0 (exactly 100%)
        # take_profit_pct = 1.0 → ratio 1.0 >= 1.0 → tetiklenmeli
        bet = make_mock_bet(entry_price=0.10)
        should_exit, reason = rm.check_take_profit(bet, 0.20)
        assert should_exit is True, "100% ratio should trigger take_profit"

        # Test: entry=0.10, current=0.199 → ratio=0.99 → tetiklenmemeli
        should_exit, _ = rm.check_take_profit(bet, 0.199)
        assert should_exit is False, "99% ratio should NOT trigger take_profit"
