"""Smoke test for the LLM loop orchestrator (Layer 4 integration glue).

Verifies:
  1. Module imports cleanly.
  2. find_global_best() returns None when no layer has run yet.
  3. deploy_best_to_live() returns gracefully when nothing to deploy.
  4. get_status() returns the expected shape.
  5. run_karpathy_layer() runs end-to-end without raising (even if data missing).
  6. run_full_cycle() runs end-to-end without raising.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from asi_engine.llm_loop_orchestrator import (  # noqa: E402
    deploy_best_to_live,
    find_global_best,
    get_status,
    run_full_cycle,
    run_karpathy_layer,
)


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """Redirect all layer best-paths to a temp dir so tests don't pollute real data."""
    import asi_engine.asi_evolve as ae
    import asi_engine.karpathy_weekly as kw
    import asi_engine.llm_loop_orchestrator as orch
    import asi_engine.sia_hourly as sh

    # Redirect orchestrator paths
    monkeypatch.setattr(orch, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orch, "LIVE_WEIGHTS_PATH", str(tmp_path / "model_weights.json"))
    monkeypatch.setattr(
        orch, "LIVE_STRATEGY_PATH", str(tmp_path / "strategy_params.json")
    )
    monkeypatch.setattr(orch, "ORCHESTRATOR_LOG", str(tmp_path / "llm_loop_runs.jsonl"))

    # Redirect each layer's best path
    monkeypatch.setattr(kw, "BEST_PATH", str(tmp_path / "karpathy_best.json"))
    monkeypatch.setattr(kw, "RESULTS_TSV_PATH", str(tmp_path / "karpathy_results.tsv"))
    monkeypatch.setattr(ae, "BEST_PATH", str(tmp_path / "asi_evolve_best.json"))
    monkeypatch.setattr(
        ae, "RESULTS_TSV_PATH", str(tmp_path / "asi_evolve_results.tsv")
    )
    monkeypatch.setattr(ae, "EXP_DB_PATH", str(tmp_path / "asi_evolve_experiments.db"))
    monkeypatch.setattr(sh, "BEST_PATH", str(tmp_path / "sia_hourly_best.json"))
    monkeypatch.setattr(
        sh, "RESULTS_TSV_PATH", str(tmp_path / "sia_hourly_results.tsv")
    )

    yield


def test_find_global_best_returns_none_when_empty():
    hyp, stats, source = find_global_best()
    assert hyp is None
    assert stats is None
    assert source is None


def test_deploy_best_to_live_returns_gracefully_when_empty():
    result = deploy_best_to_live()
    assert result["deployed"] is False
    assert result["reason"] == "no_eligible_best"


def test_get_status_returns_expected_shape():
    status = get_status()
    assert "layers" in status
    assert "karpathy_weekly" in status["layers"]
    assert "asi_evolve_daily" in status["layers"]
    assert "sia_hourly" in status["layers"]
    assert "live_trader" in status
    assert "global_best" in status


def test_run_karpathy_layer_smoke():
    """End-to-end Karpathy layer run — should not raise even if data is missing."""
    summary = run_karpathy_layer(rounds=1, use_llm=False)
    # Either it ran or returned early with error
    assert "error" in summary or "rounds_run" in summary
    # Even on error, the orchestrator should not crash
    assert "deploy" in summary or summary.get("error")


def test_run_full_cycle_smoke():
    """End-to-end full cycle — should not raise even if data is missing."""
    summary = run_full_cycle(karpathy_rounds=1, asi_evolve_candidates=2, use_llm=False)
    assert "karpathy_weekly" in summary
    assert "asi_evolve_daily" in summary
    assert "sia_hourly" in summary
    assert "final_deploy" in summary
    assert "elapsed_seconds" in summary
    assert summary["elapsed_seconds"] >= 0


def test_orchestrator_logs_to_jsonl():
    """Run a layer and verify the run is logged to ORCHESTRATOR_LOG."""
    run_karpathy_layer(rounds=1, use_llm=False)
    # The log file should have at least one entry
    from asi_engine.llm_loop_orchestrator import ORCHESTRATOR_LOG

    assert os.path.exists(ORCHESTRATOR_LOG)
    with open(ORCHESTRATOR_LOG) as f:
        lines = f.readlines()
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert "timestamp" in entry
    assert "layer" in entry
    assert entry["layer"] == "karpathy_weekly"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
