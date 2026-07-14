"""Tests for the Karpathy-style autonomous parameter search.

NOTE: scripts/karpathy_search.py was deleted during project cleanup.
These tests are all skipped until the script is restored or the logic
is moved to a proper module (e.g. asi_engine/karpathy_weekly.py).
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_skip_script_deleted = pytest.mark.skip(reason="scripts/karpathy_search.py was deleted — restore or migrate to test")


@_skip_script_deleted
def test_karpathy_search_runs_with_small_budget():
    """The search loop should run end-to-end on a small budget and
    return a best candidate with all the expected fields populated.
    """
    import karpathy_search as ks

    best, leaderboard = ks.search(num_candidates=20, seed=7, verbose=False)

    assert best is not None, "search() returned None for best"
    assert "candidate" in best
    assert "score" in best
    assert "trades" in best
    assert "roi" in best
    assert "sharpe" in best
    assert "win_rate" in best
    assert "model_weights" in best["candidate"]
    assert "min_edge" in best["candidate"]
    assert "kelly_fraction" in best["candidate"]
    assert "min_entry_price" in best["candidate"]
    assert "inefficiency_min" in best["candidate"]
    assert len(leaderboard) == 20


@_skip_script_deleted
def test_karpathy_search_is_deterministic():
    """Two runs with the same seed must produce the same best candidate."""
    import karpathy_search as ks

    best1, _ = ks.search(num_candidates=15, seed=42, verbose=False)
    best2, _ = ks.search(num_candidates=15, seed=42, verbose=False)

    assert best1["score"] == best2["score"]
    assert best1["candidate"]["min_edge"] == best2["candidate"]["min_edge"]
    assert best1["candidate"]["kelly_fraction"] == best2["candidate"]["kelly_fraction"]
    assert best1["candidate"]["min_entry_price"] == best2["candidate"]["min_entry_price"]


@_skip_script_deleted
def test_karpathy_search_finds_positive_roi_candidate():
    """Given enough candidates, the search should find at least one
    parameter set with positive ROI. This is the whole point of the
    Karpathy loop — the asymmetric-payoff bleed is fixable with the
    min_entry_price lever.
    """
    import karpathy_search as ks

    best, leaderboard = ks.search(num_candidates=30, seed=7, verbose=False)

    # With 30 candidates and a Brier-driven weight prior, at least one
    # should beat break-even. If this fails consistently, the search
    # space is too narrow.
    positive_roi = [r for r in leaderboard if r["roi"] > 0]
    assert len(positive_roi) > 0, "No candidate had positive ROI — the search space is broken."


@_skip_script_deleted
def test_save_best_to_disk_writes_strategy_params():
    """save_best_to_disk should write a valid strategy_params.json
    that the production config loader can read back.
    """
    import karpathy_search as ks

    # Construct a fake best candidate
    fake_best = {
        "candidate": {
            "model_weights": {"gfs_seamless": 0.5, "ecmwf_ifs025": 0.5},
            "min_edge": 0.07,
            "kelly_fraction": 0.10,
            "max_bet_pct": 0.03,
            "min_entry_price": 0.30,
            "inefficiency_min": -0.10,
        },
        "trades": 100,
        "won": 95,
        "lost": 5,
        "roi": 10.0,
        "sharpe": 0.8,
        "win_rate": 0.95,
        "pnl": 1000.0,
        "brier": 0.02,
        "score": 2.5,
    }

    ks.save_best_to_disk(fake_best)

    # Verify the file was written
    strategy_path = REPO / "data" / "strategy_params.json"
    assert strategy_path.exists()
    with open(strategy_path) as f:
        params = json.load(f)

    assert params["min_edge"] == 0.07
    assert params["kelly_fraction"] == 0.10
    assert params["min_entry_price"] == 0.30
    assert params["inefficiency_min"] == -0.10
    assert params["backtest"]["trades"] == 100
    assert params["backtest"]["roi_pct"] == 10.0


def test_apply_persisted_strategy_params_reads_karpathy_file():
    """apply_persisted_strategy_params should pick up the
    strategy_params.json file written by the Karpathy search and apply
    the values to bot_config.
    """
    from config.settings import Config, apply_persisted_strategy_params, bot_config

    applied = apply_persisted_strategy_params()

    # The fixture test_setup wrote a strategy_params.json with
    # min_edge=0.07, kelly_fraction=0.10, etc. Either that file is
    # present (and applied) or there's no file (and applied is empty).
    if applied:
        assert "min_edge" in applied
        assert "kelly_fraction" in applied
        # The values should match what was written.
        assert bot_config.strategy.min_edge == applied["min_edge"]
        assert bot_config.strategy.kelly_fraction == applied["kelly_fraction"]
        # Config mirror fields should be in sync.
        assert Config.KELLY_FRACTION == bot_config.strategy.kelly_fraction
        assert Config.MIN_ENTRY_PRICE == bot_config.strategy.min_entry_price
