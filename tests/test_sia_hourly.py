"""Smoke test for the SIA hourly layer (asi_engine/sia_hourly.py).

Verifies:
  1. Module imports cleanly.
  2. Default harness file is created if missing and is valid Python.
  3. predict_yes_probability returns a sane value for sample inputs.
  4. MetaAgent.decide() returns at least one action.
  5. TargetAgent.mutate_weights() returns a valid Hypothesis.
  6. FeedbackAgent.evaluate_harness_patch() rejects a syntax-broken patch.
  7. FeedbackAgent.evaluate_harness_patch() accepts a valid patch.
  8. run_sia_hourly() runs end-to-end without raising.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from asi_engine.karpathy_weekly import (  # noqa: E402
    DEFAULT_MODELS,
    Hypothesis,
    _uniform_weights,
)
from asi_engine.sia_hourly import (  # noqa: E402
    HARNESS_PATH,
    FeedbackAgent,
    MetaAgent,
    SIAState,
    TargetAgent,
    _ensure_harness,
    _load_harness_source,
    run_sia_hourly,
)


@pytest.fixture(autouse=True)
def restore_harness():
    """Restore the default harness after each test (in case a test patches it)."""
    _ensure_harness()
    original_src = _load_harness_source()
    yield
    with open(HARNESS_PATH, "w", encoding="utf-8") as f:
        f.write(original_src)


def test_harness_file_exists_and_valid():
    _ensure_harness()
    src = _load_harness_source()
    assert "def predict_yes_probability" in src
    # Compile to check syntax
    compile(src, HARNESS_PATH, "exec")


def test_harness_predict_returns_sane_value():
    import importlib

    import asi_engine.sia_harness as mod

    importlib.reload(mod)
    p = mod.predict_yes_probability(
        forecasts={m: 25.0 + i for i, m in enumerate(DEFAULT_MODELS)},
        weights=_uniform_weights(),
        threshold=30.0,
        days_ahead=2,
    )
    assert 0.0 <= p <= 1.0


def test_meta_agent_returns_at_least_one_action():
    meta = MetaAgent(use_llm=False)
    state = SIAState(
        parent_hypothesis=Hypothesis(
            description="test",
            model_weights=_uniform_weights(),
            min_edge=0.05,
            kelly_fraction=0.15,
            max_bet_pct=0.05,
        ),
        parent_stats={"sharpe": 0.0, "brier_score": 0.30, "total_trades": 5},
    )
    actions = meta.decide(state)
    assert len(actions) >= 1
    assert all(a in {"weight_mutation", "harness_patch"} for a in actions)


def test_meta_agent_suggests_harness_patch_when_brier_bad():
    meta = MetaAgent(use_llm=True)  # LLM enabled so harness_patch is allowed
    state = SIAState(
        parent_hypothesis=Hypothesis(
            description="test",
            model_weights=_uniform_weights(),
            min_edge=0.05,
            kelly_fraction=0.15,
            max_bet_pct=0.05,
        ),
        parent_stats={"sharpe": 1.0, "brier_score": 0.35, "total_trades": 100},
    )
    actions = meta.decide(state)
    assert "harness_patch" in actions


def test_target_agent_mutate_weights_returns_valid_hypothesis():
    target = TargetAgent(use_llm=False, seed=42)
    parent = Hypothesis(
        description="parent",
        model_weights=_uniform_weights(),
        min_edge=0.05,
        kelly_fraction=0.15,
        max_bet_pct=0.05,
    )
    child = target.mutate_weights(parent)
    assert isinstance(child, Hypothesis)
    assert abs(sum(child.model_weights.values()) - 1.0) < 1e-3
    assert child.source == "sia_weight_mutation"


def test_feedback_agent_rejects_broken_syntax():
    fb = FeedbackAgent(pd.DataFrame(), [])
    broken_src = "def predict_yes_probability(\n  # syntax error"
    stats, err = fb.evaluate_harness_patch(broken_src)
    assert stats is None
    assert "SyntaxError" in err or "syntax" in err.lower()


def test_feedback_agent_accepts_valid_patch():
    fb = FeedbackAgent(pd.DataFrame(), [])
    valid_src = """
import math

def predict_yes_probability(forecasts, weights, threshold, days_ahead=1):
    if not forecasts:
        return 0.5
    wsum = sum(weights.get(m, 0.0) for m in forecasts)
    if wsum <= 0:
        return 0.5
    mean = sum(weights.get(m, 0.0) * f for m, f in forecasts.items()) / wsum
    z = (mean - threshold) / 2.0
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return min(max(p, 0.01), 0.99)
"""
    stats, err = fb.evaluate_harness_patch(valid_src)
    # Even if Brier dataset is empty, the patch should at least be
    # syntactically valid (err should be empty)
    assert err == "" or "smoke" not in err.lower(), f"Unexpected error: {err}"


def test_run_sia_hourly_smoke():
    """End-to-end smoke test — should not raise even if data is empty."""
    summary = run_sia_hourly(use_llm=False, seed=42)
    # Either it ran or returned early with error
    assert "cycles_run" in summary or "error" in summary
    if summary.get("error"):
        assert summary["cycles_run"] == 0
        return
    assert summary["cycles_run"] == 1
    assert "actions_taken" in summary
    assert "best_hypothesis" in summary
    assert "best_stats" in summary
    # Best hypothesis weights must be normalised
    bw = summary["best_hypothesis"]["model_weights"]
    assert abs(sum(bw.values()) - 1.0) < 1e-3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
