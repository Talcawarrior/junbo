"""Tests for Active Risk Management — position-level stop-loss, take-profit,
time decay, trailing stop, rebalancing, and model reversal detection."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from config.settings import Config
from engine.strategy import RiskManager

# ── Helpers ──────────────────────────────────────────────────────────────────


def make_mock_bet(**kwargs):
    """Create a minimal Bet-like object with the fields RiskManager reads."""
    bet = MagicMock()
    bet.id = kwargs.get("id", 1)
    bet.market_id = kwargs.get("market_id", "test_123")
    bet.city_code = kwargs.get("city_code", "TEST")
    bet.city = kwargs.get("city", "TestCity")
    bet.side = kwargs.get("side", "YES")
    bet.outcome = kwargs.get("outcome", "YES")
    bet.stake = kwargs.get("stake", 100.0)
    bet.amount = kwargs.get("amount", 100.0)
    bet.entry_price = kwargs.get("entry_price", 0.50)
    bet.price = kwargs.get("price", 0.50)
    bet.current_price = kwargs.get("current_price", 0.50)
    bet.shares = kwargs.get("shares", 200.0)
    bet.fair_value = kwargs.get("fair_value", 0.55)
    bet.expected_value = kwargs.get("expected_value", 0.10)
    bet.unrealized_pnl = kwargs.get("unrealized_pnl", 0.0)
    bet.realized_pnl = kwargs.get("realized_pnl", 0.0)
    bet.pnl = kwargs.get("pnl", 0.0)
    bet.status = kwargs.get("status", "placed")
    bet.ladder_data = kwargs.get("ladder_data", None)
    bet.result_data = kwargs.get("result_data", None)
    bet.close_reason = kwargs.get("close_reason", None)
    bet.closed_at = kwargs.get("closed_at", None)
    bet.placed_at = kwargs.get("placed_at", datetime.now(timezone.utc) - timedelta(hours=1))
    bet.partial_tp_done = kwargs.get("partial_tp_done", False)
    bet.covered_fraction = kwargs.get("covered_fraction", 0.0)
    return bet


def make_mock_market(**kwargs):
    """Create a minimal WeatherMarket-like object."""
    m = MagicMock()
    m.id = kwargs.get("id", "test_123")
    m.yes_price = kwargs.get("yes_price", 0.50)
    m.no_price = kwargs.get("no_price", 0.50)
    resolution = kwargs.get("resolution_date", datetime.now(timezone.utc) + timedelta(days=2))
    m.resolution_date = resolution
    m.target_date = kwargs.get("target_date", resolution)
    return m


class _SignalObj:
    """Simple attribute-based signal object — not a MagicMock so getattr works."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_mock_signal(**kwargs):
    """Create a minimal signal/dict-like object for rebalance tests."""
    return _SignalObj(
        market_id=kwargs.get("market_id", "new_456"),
        city=kwargs.get("city", "London"),
        edge=kwargs.get("edge", 0.30),
        probability=kwargs.get("probability", 0.70),
        entry_price=kwargs.get("entry_price", 0.40),
    )


def make_risk_manager():
    """Create a RiskManager with a mock db session."""
    rm = RiskManager(db_session=MagicMock(), cfg=Config)
    # Override the config risk settings for deterministic tests
    return rm


# ── Stop-Loss Tests ──────────────────────────────────────────────────────────


class TestStopLoss:
    """RiskManager.check_stop_loss tests."""

    def test_stop_loss_triggers_at_threshold(self):
        """%30+ zararda stop-loss tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        # %30 zarar = 0.35
        should_exit, reason = rm.check_stop_loss(bet, 0.35)
        assert should_exit is True
        assert "stop_loss" in reason.lower()

    def test_stop_loss_triggers_above_threshold(self):
        """%35 zararda da tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_stop_loss(bet, 0.30)
        assert should_exit is True

    def test_stop_loss_below_threshold_no_trigger(self):
        """%25 zararda stop-loss tetiklenmemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_stop_loss(bet, 0.38)
        assert should_exit is False

    def test_stop_loss_kardayken_tetiklenmez(self):
        """Kardayken stop-loss tetiklenmez."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_stop_loss(bet, 0.60)
        assert should_exit is False

    def test_stop_loss_sifir_entry(self):
        """Entry price 0'sa hata vermemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.0)
        should_exit, reason = rm.check_stop_loss(bet, 0.30)
        assert should_exit is False


# ── Take-Profit Tests ────────────────────────────────────────────────────────


class TestTakeProfit:
    """RiskManager.check_take_profit tests."""

    def test_take_profit_triggers_at_threshold(self):
        """%100+ karda take-profit tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.30)
        should_exit, reason = rm.check_take_profit(bet, 0.60)
        assert should_exit is True
        assert "take_profit" in reason.lower()

    def test_take_profit_yuksek_karda_tetiklenir(self):
        """%200 karda da tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.25)
        should_exit, reason = rm.check_take_profit(bet, 0.75)
        assert should_exit is True

    def test_take_profit_below_threshold(self):
        """%50 karda tetiklenmemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.40)
        should_exit, reason = rm.check_take_profit(bet, 0.55)
        assert should_exit is False

    def test_take_profit_zarardayken_tetiklenmez(self):
        """Zarardayken take-profit tetiklenmez."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_take_profit(bet, 0.40)
        assert should_exit is False


# ── Time Decay Tests ─────────────────────────────────────────────────────────


class TestTimeDecay:
    """RiskManager.check_time_decay tests."""

    def test_time_decay_exit_within_window_in_loss(self):
        """Settlement'a <24h kala ve %10+ zarardaysa çık."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market(resolution_date=datetime.now(timezone.utc) + timedelta(hours=20))
        should_exit, reason = rm.check_time_decay(bet, 0.42, market)
        assert should_exit is True
        assert "time_decay" in reason.lower()

    def test_time_decay_outside_window(self):
        """Settlement'a >24h kala tetiklenmemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market(resolution_date=datetime.now(timezone.utc) + timedelta(hours=48))
        should_exit, reason = rm.check_time_decay(bet, 0.42, market)
        assert should_exit is False

    def test_time_decay_kardayken_tetiklenmez(self):
        """Settlement'a <24h kala ama kardaysak tetiklenmez."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market(resolution_date=datetime.now(timezone.utc) + timedelta(hours=10))
        should_exit, reason = rm.check_time_decay(bet, 0.55, market)
        assert should_exit is False

    def test_time_decay_no_market_object(self):
        """Market objesi yoksa hata vermemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, reason = rm.check_time_decay(bet, 0.40, None)
        assert should_exit is False

    def test_time_decay_market_passed(self):
        """Settlement zamanı geçmişse tetiklenmemeli (settlement halleder)."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market(resolution_date=datetime.now(timezone.utc) - timedelta(hours=2))
        should_exit, reason = rm.check_time_decay(bet, 0.40, market)
        assert should_exit is False


# ── Trailing Stop Tests ──────────────────────────────────────────────────────


class TestTrailingStop:
    """RiskManager.check_trailing_stop tests."""

    def test_trailing_stop_drop_from_peak(self):
        """Tepeden %15+ düşüşte trailing stop tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data=json.dumps({"peak_price": 0.90}))
        should_exit, reason = rm.check_trailing_stop(bet, 0.70)
        assert should_exit is True
        assert "trailing_stop" in reason.lower()

    def test_trailing_stop_small_drop(self):
        """%5 düşüşte tetiklenmemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data=json.dumps({"peak_price": 0.80}))
        should_exit, reason = rm.check_trailing_stop(bet, 0.77)
        assert should_exit is False

    def test_trailing_stop_new_peak_updates(self):
        """Yeni tepe noktası peak_price'ı güncellemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data=json.dumps({"peak_price": 0.60}))
        # Fiyat 0.80'e çıktı -> peak güncellenmeli
        rm.check_trailing_stop(bet, 0.80)
        data = json.loads(bet.result_data)
        assert data["peak_price"] == 0.80

    def test_trailing_stop_no_peak_data(self):
        """result_data'da peak_price yoksa entry_price kullanilir.
        peak <= entry ise (pozisyon hic karli olmadi) trailing stop tetiklenmez.
        Bu durumda trailing stop yerine stop_loss calismalidir."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data=None)
        # 0.50'den 0.42'ye dusus = %16
        # Ama peak = entry (0.50 <= 0.50) oldugu icin trailing stop tetiklenmez
        should_exit, reason = rm.check_trailing_stop(bet, 0.42)
        assert should_exit is False

    def test_trailing_stop_sifir_entry(self):
        """Entry price 0'sa hata vermemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.0)
        should_exit, reason = rm.check_trailing_stop(bet, 0.50)
        assert should_exit is False


# ── Early Exit Combined Tests ────────────────────────────────────────────────


class TestEarlyExit:
    """RiskManager.check_early_exit — stop-loss, take-profit, trailing-stop combined."""

    def test_early_exit_stop_loss(self):
        """Stop-loss öncelikli tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.30, market)
        assert should_exit is True
        assert "stop_loss" in reason.lower()

    def test_early_exit_take_profit(self):
        """Take-profit tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.25)
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.60, market)
        assert should_exit is True
        assert "take_profit" in reason.lower()

    def test_early_exit_trailing_stop(self):
        """Trailing stop tetiklenmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data=json.dumps({"peak_price": 0.90}))
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.70, market)
        assert should_exit is True
        assert "trailing_stop" in reason.lower()

    def test_early_exit_hold(self):
        """Hiçbir koşul yoksa Hold dönmeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        market = make_mock_market()
        should_exit, reason = rm.check_early_exit(bet, 0.52, market)
        assert should_exit is False
        assert reason == "Hold"

    def test_early_exit_no_market(self):
        """Market yoksa time-decay atlanır ama stop-loss/take-profit çalışır."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        # Stop-loss tetiklenmeli (market=None olsa da)
        should_exit, reason = rm.check_early_exit(bet, 0.30, None)
        assert should_exit is True
        assert "stop_loss" in reason.lower()


# ── Rebalance Tests ──────────────────────────────────────────────────────────


class TestRebalance:
    """RiskManager.check_rebalance tests."""

    def test_rebalance_high_edge_opportunity(self):
        """Yeni edge 2x+ ise ve eski pozisyon zarardaysa rebalance önerir."""
        rm = make_risk_manager()
        old_bet = make_mock_bet(expected_value=0.10, unrealized_pnl=-20.0, stake=100.0)
        new_signal = make_mock_signal(edge=0.40)  # 4x eski
        result = rm.check_rebalance(new_signal, [old_bet])
        assert result is not None

    def test_rebalance_low_edge_no_action(self):
        """Yeni edge yeterli değilse None dönmeli."""
        rm = make_risk_manager()
        old_bet = make_mock_bet(expected_value=0.20, unrealized_pnl=-20.0, stake=100.0)
        new_signal = make_mock_signal(edge=0.25)  # 1.25x < 2.0
        result = rm.check_rebalance(new_signal, [old_bet])
        assert result is None

    def test_rebalance_profitable_position_kept(self):
        """Eski pozisyon kardaysa rebalance yapılmaz."""
        rm = make_risk_manager()
        old_bet = make_mock_bet(expected_value=0.10, unrealized_pnl=10.0, stake=100.0)
        new_signal = make_mock_signal(edge=0.40)
        result = rm.check_rebalance(new_signal, [old_bet])
        assert result is None

    def test_rebalance_no_active_bets(self):
        """Aktif bahis yoksa None dönmeli."""
        rm = make_risk_manager()
        new_signal = make_mock_signal(edge=0.40)
        result = rm.check_rebalance(new_signal, [])
        assert result is None


# ── Model Reversal Tests ─────────────────────────────────────────────────────


class TestModelReversal:
    """RiskManager.check_model_reversal tests."""

    def test_model_reversal_strong_reversal(self):
        """Model prob %20+ ters yönde değiştiyse ve zarardaysak çık."""
        rm = make_risk_manager()
        bet = make_mock_bet(fair_value=0.80, unrealized_pnl=-15.0, stake=100.0)
        analysis = MagicMock()
        analysis.estimated_probability = 0.40  # 0.80 -> 0.40 = -0.50 değişim
        should_exit, reason = rm.check_model_reversal(bet, analysis)
        assert should_exit is True
        assert "model_reversal" in reason.lower()

    def test_model_reversal_no_reversal(self):
        """Model prob değişmediyse çıkılmaz."""
        rm = make_risk_manager()
        bet = make_mock_bet(fair_value=0.55, unrealized_pnl=-5.0, stake=100.0)
        analysis = MagicMock()
        analysis.estimated_probability = 0.53  # Küçük değişim
        should_exit, reason = rm.check_model_reversal(bet, analysis)
        assert should_exit is False

    def test_model_reversal_no_analysis(self):
        """Analysis yoksa hata vermemeli."""
        rm = make_risk_manager()
        bet = make_mock_bet(fair_value=0.55)
        should_exit, reason = rm.check_model_reversal(bet, None)
        assert should_exit is False

    def test_model_reversal_strong_reversal_any_pnl(self):
        """Model prob %30+ ters yönde değiştiyse karda da çık."""
        rm = make_risk_manager()
        bet = make_mock_bet(fair_value=0.80, unrealized_pnl=10.0, stake=100.0)
        analysis = MagicMock()
        analysis.estimated_probability = 0.35  # 0.80 -> 0.35 = -0.45
        should_exit, reason = rm.check_model_reversal(bet, analysis)
        assert should_exit is True


# ── Position Sizing Tests ───────────────────────────────────────────────────


class TestPositionSizing:
    """RiskManager.calculate_position_size_with_risk tests."""

    def test_position_size_respects_max_bet_pct(self):
        """Max bet %3'ü aşmamalı."""
        rm = make_risk_manager()
        signal = _SignalObj(model_prob=0.90, entry_price=0.50)
        size = rm.calculate_position_size_with_risk(signal, 10000)
        # %3 of 10000 = 300
        assert size <= 300

    def test_position_size_minimum_bet(self):
        """Minimum bet boyutundan küçük olmamalı."""
        rm = make_risk_manager()
        # Çok düşük Kelly için
        signal = _SignalObj(model_prob=0.51, entry_price=0.49)
        size = rm.calculate_position_size_with_risk(signal, 1000)
        assert size >= 1.0  # MIN_BET_SIZE

    def test_position_size_sifir_prob(self):
        """Model prob 0'sa 0 dönmeli."""
        rm = make_risk_manager()
        signal = _SignalObj(model_prob=0.0, entry_price=0.50)
        size = rm.calculate_position_size_with_risk(signal, 1000)
        assert size == 0.0


# ── Edge Case / Safety Tests ─────────────────────────────────────────────────


class TestEdgeCases:
    """RiskManager safety and edge case tests."""

    def test_check_early_exit_corrupted_bet(self):
        """Eksik alanları olan bet'te crash olmamalı."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.0, fair_value=None, shares=None)
        should_exit, reason = rm.check_early_exit(bet, 0.50, None)
        # should_exit False olabilir ama crash olmamalı
        assert isinstance(should_exit, bool)
        assert isinstance(reason, str)

    def test_stop_loss_configurable_threshold(self):
        """Farklı stop_loss_pct değerleriyle test."""
        # Varsayılan %25
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, _ = rm.check_stop_loss(bet, 0.40)  # %20 zarar
        assert should_exit is False  # %25'i geçmedi

    def test_take_profit_configurable_threshold(self):
        """Farklı take_profit_pct değerleriyle test."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50)
        should_exit, _ = rm.check_take_profit(bet, 0.95)  # %90 kar
        assert should_exit is False  # %100'ü geçmedi

    def test_rebalance_with_dict_signal(self):
        """Signal dict olarak da gelebilmeli."""
        rm = make_risk_manager()
        old_bet = make_mock_bet(expected_value=0.10, unrealized_pnl=-20.0, stake=100.0)
        # Dict olarak signal
        new_signal = {
            "market_id": "new",
            "city": "Paris",
            "edge": 0.50,
            "probability": 0.80,
            "entry_price": 0.30,
        }
        result = rm.check_rebalance(new_signal, [old_bet])
        assert result is not None  # edge 5x > 0.10

    def test_trailing_stop_corrupted_json(self):
        """Bozuk JSON'da hata vermemeli, peak=entry_oldugu icin tetiklenmez."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=0.50, result_data="{corrupted json!!!}")
        should_exit, reason = rm.check_trailing_stop(bet, 0.40)
        # Bozuk JSON -> peak=entry_price -> peak <= entry oldugu icin tetiklenmez
        assert should_exit is False

    def test_model_reversal_same_probability(self):
        """Prob aynıysa çıkılmaz."""
        rm = make_risk_manager()
        bet = make_mock_bet(fair_value=0.55)
        analysis = MagicMock()
        analysis.estimated_probability = 0.55
        should_exit, reason = rm.check_model_reversal(bet, analysis)
        assert should_exit is False

    def test_early_exit_no_crash_on_none_values(self):
        """None değerlerde crash olmamalı."""
        rm = make_risk_manager()
        bet = make_mock_bet(entry_price=None, price=None, shares=None)
        should_exit, reason = rm.check_early_exit(bet, 0.50, None)
        assert isinstance(should_exit, bool)
        assert isinstance(reason, str)
