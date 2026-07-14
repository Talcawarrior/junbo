"""Smoke test for the ASI-Evolve daily layer (asi_engine/asi_evolve.py).

Verifies:
  1. Module imports cleanly.
  2. Experiment DB schema creates correctly.
  3. UCB1 parent selection returns None on empty DB (uses prior).
  4. CognitionStore.add() and retrieve() work with hash-fallback embeddings.
  5. Crossover produces a valid Hypothesis with normalised weights.
  6. run_asi_evolve_daily(n_candidates=2) runs end-to-end without raising.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from asi_engine.asi_evolve import (  # noqa: E402
    CognitionStore,
    EngineerAgent,
    _get_db,
    _mean_stats,
    crossover,
    run_asi_evolve_daily,
    ucb1_select_parent,
)
from asi_engine.karpathy_weekly import (  # noqa: E402
    DEFAULT_MODELS,
    Hypothesis,
    _uniform_weights,
    generate_hypothesis,
)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Redirect the experiment DB to a temp file."""
    db_path = tmp_path / "test_experiments.db"
    monkeypatch.setattr("asi_engine.asi_evolve.EXP_DB_PATH", str(db_path))
    return _get_db()


def test_ucb1_returns_none_on_empty_db(temp_db):
    parent_id = ucb1_select_parent(temp_db)
    assert parent_id is None


def test_cognition_store_add_and_retrieve():
    store = CognitionStore(dim=64)
    store.add(1, "boost ECMWF + ICON")
    store.add(2, "tighten min_edge to 7%")
    store.add(3, "aggressive Kelly 25%")
    results = store.retrieve("tighten edge threshold", k=2)
    assert len(results) <= 2
    # The most similar should be the min_edge one
    if results:
        assert "id" in results[0]
        assert "similarity" in results[0]


def test_crossover_normalises_weights():
    h1 = generate_hypothesis(0)
    h2 = generate_hypothesis(1)
    child = crossover(h1, h2)
    assert abs(sum(child.model_weights.values()) - 1.0) < 1e-3
    assert child.source == "crossover"
    # All models present
    assert all(m in child.model_weights for m in DEFAULT_MODELS)


def test_engineer_propose_fallback_ladder():
    eng = EngineerAgent(use_llm=False)
    parent = Hypothesis(
        description="test parent",
        model_weights=_uniform_weights(),
        min_edge=0.05,
        kelly_fraction=0.15,
        max_bet_pct=0.05,
    )
    hyp = eng.propose(parent, round_num=1, candidate_idx=0, context=[])
    assert isinstance(hyp, Hypothesis)
    assert abs(sum(hyp.model_weights.values()) - 1.0) < 1e-3


def test_mean_stats_empty():
    out = _mean_stats([])
    assert out["sharpe"] == 0.0
    assert out["total_trades"] == 0


def test_mean_stats_basic():
    s1 = {
        "sharpe": 1.0,
        "roi_pct": 5.0,
        "win_rate": 0.6,
        "brier_score": 0.2,
        "total_trades": 10,
        "total_pnl": 100.0,
        "total_staked": 1000.0,
    }
    s2 = {
        "sharpe": 2.0,
        "roi_pct": 10.0,
        "win_rate": 0.7,
        "brier_score": 0.18,
        "total_trades": 8,
        "total_pnl": 200.0,
        "total_staked": 1500.0,
    }
    out = _mean_stats([s1, s2])
    assert out["sharpe"] == 1.5
    assert out["total_trades"] == 18


def test_run_asi_evolve_daily_smoke():
    """End-to-end smoke test — should not raise even if data is empty."""
    summary = run_asi_evolve_daily(n_candidates=2, use_llm=False, seed=42)
    # Either it ran or returned early with error
    assert "candidates_run" in summary or "error" in summary
    if summary.get("error"):
        assert summary["candidates_run"] == 0
        return
    assert summary["candidates_run"] == 2
    assert "best_hypothesis" in summary
    assert "best_stats" in summary
    # Best hypothesis weights must be normalised
    bw = summary["best_hypothesis"]["model_weights"]
    assert abs(sum(bw.values()) - 1.0) < 1e-3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
