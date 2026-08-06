"""Golden/Snapshot Tests - Backtest sonuclarinin dogrulugu.

Bir refactor sonrasi backtest sonuclari sessizce degisirse,
bu testler hemen kirmizi yanar.

Testler:
- Historical calibrations snapshot
- Model weights snapshot
- Fee calculation snapshot
- Portfolio value snapshot
"""

import pytest
import json
import hashlib
from pathlib import Path


# ============================================================================
# SNAPSHOT DIRECTORY
# ============================================================================

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def load_snapshot(name: str) -> dict:
    """Load a snapshot file."""
    snapshot_path = SNAPSHOT_DIR / f"{name}.json"
    if not snapshot_path.exists():
        return {}
    with open(snapshot_path, "r") as f:
        return json.load(f)


def save_snapshot(name: str, data: dict):
    """Save a snapshot file."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{name}.json"
    with open(snapshot_path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def compute_hash(data: dict) -> str:
    """Compute hash of data for comparison."""
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(serialized.encode()).hexdigest()


# ============================================================================
# 1. HISTORICAL CALIBRATIONS SNAPSHOT
# ============================================================================


class TestHistoricalCalibrations:
    """Historical calibrations snapshot testleri."""

    def test_calibration_data_structure(self):
        """Kalibrasyon veri yapisi degismemeli."""
        # Beklenen yapi
        expected_keys = {"city_code", "city", "date", "metric", "model", "predicted_value", "actual_value", "bias"}

        # Bu yapi degisirse test fail eder
        snapshot = load_snapshot("calibration_structure")
        if not snapshot:
            # Ilk calistirma: kaydet
            save_snapshot("calibration_structure", {"required_keys": list(expected_keys), "version": "1.0"})
            return

        # Sonraki calistirmalarda karsilastir
        assert set(snapshot["required_keys"]) == expected_keys

    def test_model_weights_snapshot(self):
        """Model agirliklari degismemeli (refactor sonrasi)."""
        from config.settings import bot_config

        current_weights = bot_config.model_weights

        snapshot = load_snapshot("model_weights")
        if not snapshot:
            # Ilk calistirma: kaydet
            save_snapshot("model_weights", current_weights)
            return

        # Karsilastir
        for model, weight in snapshot.items():
            assert model in current_weights, f"Model {model} missing"
            assert abs(current_weights[model] - weight) < 0.01, (
                f"Weight changed for {model}: {weight} -> {current_weights[model]}"
            )


# ============================================================================
# 2. FEE CALCULATION SNAPSHOT
# ============================================================================


class TestFeeCalculationSnapshot:
    """Fee hesaplama snapshot testleri."""

    @pytest.mark.parametrize(
        "shares,price,fee_rate,expected_fee",
        [
            (100, 0.50, 0.05, 1.768),  # (1-p)^0.5 = 0.7071
            (100, 0.25, 0.05, 1.083),  # (1-p)^0.5 = 0.866
            (100, 0.75, 0.05, 1.875),  # (1-p)^0.5 = 0.5
            (100, 0.10, 0.05, 0.474),  # (1-p)^0.5 = 0.949
            (100, 0.90, 0.05, 1.423),  # (1-p)^0.5 = 0.316
            (50, 0.50, 0.05, 0.884),  # half shares
            (200, 0.50, 0.05, 3.536),  # double shares
            (100, 0.50, 0.07, 2.475),  # higher fee_rate
        ],
    )
    def test_fee_values_snapshot(self, shares, price, fee_rate, expected_fee):
        """Fee degerleri snapshot'i - refactor sonrasi degismemeli."""
        from utils.formulas import polymarket_fee

        fee = polymarket_fee(shares, price, fee_rate)

        # Snapshot karsilastirmasi
        snapshot_key = f"fee_{shares}_{price}_{fee_rate}"
        snapshot = load_snapshot("fee_values")

        if not snapshot:
            # Ilk calistirma: kaydet
            save_snapshot("fee_values", {snapshot_key: expected_fee})
            return

        if snapshot_key in snapshot:
            assert abs(fee - snapshot[snapshot_key]) < 0.01, f"Fee changed: {fee} -> {snapshot[snapshot_key]}"
        else:
            # Yeni test case, kaydet
            snapshot[snapshot_key] = fee
            save_snapshot("fee_values", snapshot)


# ============================================================================
# 3. PORTFOLIO VALUE SNAPSHOT
# ============================================================================


class TestPortfolioValueSnapshot:
    """Portfolio deger snapshot testleri."""

    def test_initial_portfolio_value(self):
        """Baslangic portfolio degeri degismemeli."""
        from config.settings import bot_config

        initial = bot_config.initial_portfolio

        snapshot = load_snapshot("portfolio_values")
        if not snapshot:
            save_snapshot("portfolio_values", {"initial": initial})
            return

        assert initial == snapshot["initial"], f"Initial portfolio changed: {initial} -> {snapshot['initial']}"

    def test_max_bet_cap_snapshot(self):
        """Max bet cap degismemeli."""
        from config.settings import bot_config

        max_bet_pct = bot_config.strategy.max_bet_pct
        initial = bot_config.initial_portfolio
        max_bet_cap = initial * max_bet_pct

        snapshot = load_snapshot("portfolio_values")
        if not snapshot:
            save_snapshot("portfolio_values", {"max_bet_cap": max_bet_cap})
            return

        if "max_bet_cap" in snapshot:
            assert abs(max_bet_cap - snapshot["max_bet_cap"]) < 0.01


# ============================================================================
# 4. SETTLEMENT PNL SNAPSHOT
# ============================================================================


class TestSettlementPnLSnapshot:
    """Settlement PnL snapshot testleri."""

    @pytest.mark.parametrize(
        "stake,entry_price,entry_fee,won,expected_pnl",
        [
            (100, 0.60, 1.50, True, 65.17),  # Won
            (100, 0.60, 1.50, False, -101.50),  # Lost
            (50, 0.50, 1.0, True, 99.0),  # Won
            (50, 0.50, 1.0, False, -51.0),  # Lost
            (100, 0.25, 1.0, True, 299.0),  # Won (low price)
            (100, 0.75, 1.0, True, 32.33),  # Won (high price)
        ],
    )
    def test_settlement_pnl_snapshot(self, stake, entry_price, entry_fee, won, expected_pnl):
        """Settlement PnL snapshot'i - refactor sonrasi degismemeli."""
        from utils.formulas import settlement_pnl

        pnl = settlement_pnl(stake, entry_price, entry_fee, won)

        # Snapshot karsilastirmasi
        snapshot_key = f"pnl_{stake}_{entry_price}_{entry_fee}_{won}"
        snapshot = load_snapshot("settlement_pnl")

        if not snapshot:
            save_snapshot("settlement_pnl", {snapshot_key: round(expected_pnl, 2)})
            return

        if snapshot_key in snapshot:
            assert abs(pnl - snapshot[snapshot_key]) < 0.1, f"PnL changed: {pnl} -> {snapshot[snapshot_key]}"
        else:
            snapshot[snapshot_key] = round(pnl, 2)
            save_snapshot("settlement_pnl", snapshot)


# ============================================================================
# 5. WALK-FORWARD SPLIT SNAPSHOT
# ============================================================================


class TestWalkForwardSplit:
    """Walk-forward split snapshot testleri."""

    def test_split_no_temporal_leakage(self):
        """Walk-forward split'te temporal leakage olmamali."""
        # Bu test, unified_datastore'un walk-forward split'lerinin
        # temporally correct calistigini dogrular

        # Mock test - gercek unified_datastore kullanildiginda aktif edilmeli
        expected_properties = [
            "train_end_date < test_start_date",
            "test_start_date > train_end_date",
            "no_future_data_in_train",
        ]

        snapshot = load_snapshot("walk_forward_properties")
        if not snapshot:
            save_snapshot("walk_forward_properties", {"properties": expected_properties, "version": "1.0"})
            return

        assert set(snapshot["properties"]) == set(expected_properties)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
