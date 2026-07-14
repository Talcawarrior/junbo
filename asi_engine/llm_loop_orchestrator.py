"""Layer 4: Integration orchestrator for the 3-layer LLM research loop.

Wires Layer 1 (Karpathy weekly) → Layer 2 (ASI-Evolve daily) →
Layer 3 (SIA hourly) → live trader (engine/strategy.py SIALoop +
utils/weights_store.save_weights / save_strategy_params).

Cadence (suggested cron):
  - Hourly:    run_sia_hourly()           (weight mutations, ~1-3 candidates)
  - Daily 03:00: run_asi_evolve_daily()   (20-50 candidates, UCB1 + crossover)
  - Weekly Sun 02:00: run_karpathy_weekly() (broad mutation ladder, 6 rounds)
  - After each layer: deploy_best_to_live() pushes the winning weights
    to data/model_weights.json + data/strategy_params.json, which the
    live trader reads on its next restart.

The orchestrator also exposes run_full_cycle() which runs all 3 layers
back-to-back (useful for testing / initial bootstrap).

Production: each layer is independent and idempotent. They share state
through files in data/ (karpathy_best.json, asi_evolve_best.json,
sia_hourly_best.json) and the experiment DB. If one layer fails the
others still run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from asi_engine.asi_evolve import run_asi_evolve_daily
from asi_engine.karpathy_weekly import run_karpathy_weekly
from asi_engine.sia_hourly import run_sia_hourly
from utils.weights_store import save_strategy_params as _save_strategy_params
from utils.weights_store import save_weights

logger = logging.getLogger("LLM_LOOP_ORCH")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "data"))
LIVE_WEIGHTS_PATH = os.path.join(DATA_DIR, "model_weights.json")
LIVE_STRATEGY_PATH = os.path.join(DATA_DIR, "strategy_params.json")
ORCHESTRATOR_LOG = os.path.join(DATA_DIR, "llm_loop_runs.jsonl")


# ---------------------------------------------------------------------------
# Helpers: find the current global best across all 3 layers
# ---------------------------------------------------------------------------


def _load_json(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not load %s: %s", path, e)
        return None


def find_global_best() -> tuple[
    dict[str, Any] | None, dict[str, float] | None, str | None
]:
    """Find the best hypothesis across all 3 layers.

    Returns (hypothesis_dict, stats_dict, source_layer_name).

    Selection rule: highest Sharpe, with Brier as tiebreaker (lower better).
    A hypothesis is only eligible if it has at least 5 OOS trades.
    """
    candidates: list[tuple[dict[str, Any], dict[str, float], str]] = []

    karpathy_best = _load_json(os.path.join(DATA_DIR, "karpathy_best.json"))
    if karpathy_best and "stats" in karpathy_best:
        stats = karpathy_best["stats"]
        if stats.get("total_trades", 0) >= 5:
            hyp = {
                k: v
                for k, v in karpathy_best.items()
                if k != "stats" and k != "saved_at"
            }
            candidates.append((hyp, stats, "karpathy_weekly"))

    asi_evolve_best = _load_json(os.path.join(DATA_DIR, "asi_evolve_best.json"))
    if asi_evolve_best and "stats" in asi_evolve_best:
        stats = asi_evolve_best["stats"]
        if stats.get("total_trades", 0) >= 5:
            hyp = {
                k: v
                for k, v in asi_evolve_best.items()
                if k != "stats" and k != "saved_at"
            }
            candidates.append((hyp, stats, "asi_evolve_daily"))

    sia_hourly_best = _load_json(os.path.join(DATA_DIR, "sia_hourly_best.json"))
    if sia_hourly_best and "stats" in sia_hourly_best:
        stats = sia_hourly_best["stats"]
        # SIA's harness patches don't simulate trades — accept based on Brier alone
        if "brier_score" in stats:
            hyp = {
                k: v
                for k, v in sia_hourly_best.items()
                if k != "stats" and k != "saved_at"
            }
            candidates.append((hyp, stats, "sia_hourly"))

    if not candidates:
        return None, None, None

    # Pick by Sharpe (desc), then Brier (asc)
    candidates.sort(
        key=lambda c: (-c[1].get("sharpe", -1e9), c[1].get("brier_score", 1.0))
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Deploy best → live trader
# ---------------------------------------------------------------------------


def deploy_best_to_live() -> dict[str, Any]:
    """Push the global best weights + strategy params to the live trader's
    config files (data/model_weights.json + data/strategy_params.json).

    The live trader (engine/strategy.py SIALoop) reads these on next restart
    via utils/weights_store.load_weights / load_strategy_params.

    Weights are persisted via ``utils.weights_store.save_weights`` so the
    central MIN_MODEL_WEIGHT floor + renormalize is applied — no layer
    can collapse the ensemble to a 1-2 model dominant solution.

    Returns a dict describing what was deployed.
    """
    hyp, stats, source = find_global_best()
    if hyp is None:
        logger.warning("No eligible best hypothesis to deploy — skipping")
        return {"deployed": False, "reason": "no_eligible_best"}

    os.makedirs(DATA_DIR, exist_ok=True)

    # 1. Write weights — route through save_weights() so the
    #    MIN_MODEL_WEIGHT=0.05 diversification floor is enforced.
    raw_weights = hyp.get("model_weights", {})
    save_weights(raw_weights, path=LIVE_WEIGHTS_PATH)
    # Re-read what actually got persisted so the return value reflects
    # the floor'd + renormalized weights (not the raw optimizer output).
    weights = _load_json(LIVE_WEIGHTS_PATH) or dict(raw_weights)

    # 2. Write strategy params (only the keys the live trader reads)
    strategy_params = {
        "min_edge": hyp.get("min_edge", 0.05),
        "kelly_fraction": hyp.get("kelly_fraction", 0.15),
    }
    # Preserve any extra keys the live trader reads from the existing file.
    # NOTE: max_bet_pct is EXCLUDED — it comes from Config.MAX_BET_PCT (.env)
    # and should never be overridden by SIA, as it controls the hard per-bet cap.
    existing = _load_json(LIVE_STRATEGY_PATH) or {}
    for k in ("min_entry_price", "inefficiency_min"):
        if k in existing:
            strategy_params[k] = existing[k]
        elif k in hyp:
            strategy_params[k] = hyp[k]

    _save_strategy_params(strategy_params)
    # save_strategy_params writes to its own default path; also mirror
    # to the explicit LIVE_STRATEGY_PATH for backward compat.
    if os.path.abspath(_strategy_default_path()) != os.path.abspath(LIVE_STRATEGY_PATH):
        with open(LIVE_STRATEGY_PATH, "w", encoding="utf-8") as f:
            json.dump(strategy_params, f, indent=2, sort_keys=True)

    logger.info(
        "Deployed %s best to live trader: sharpe=%.3f brier=%.4f",
        source,
        stats.get("sharpe", 0.0),
        stats.get("brier_score", 0.25),
    )

    return {
        "deployed": True,
        "source_layer": source,
        "weights": weights,
        "strategy_params": strategy_params,
        "stats": stats,
        "deployed_at": datetime.now(UTC).isoformat(),
    }


def _strategy_default_path() -> str:
    """Resolve the default strategy params path used by save_strategy_params.

    Imported lazily to avoid touching the module at import time.
    """
    from utils import weights_store as _ws

    return _ws._STRATEGY_PATH


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_run(layer: str, summary: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(ORCHESTRATOR_LOG), exist_ok=True)
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "layer": layer,
        "summary": summary,
    }
    with open(ORCHESTRATOR_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Per-layer entry points (with error handling + auto-deploy)
# ---------------------------------------------------------------------------


def run_karpathy_layer(rounds: int = 6, use_llm: bool = False) -> dict[str, Any]:
    """Run Layer 1 (Karpathy weekly) + auto-deploy."""
    logger.info("=== Layer 1: Karpathy weekly ===")
    try:
        summary = run_karpathy_weekly(rounds=rounds, use_llm=use_llm)
    except Exception as e:
        logger.exception("Layer 1 failed: %s", e)
        summary = {"error": str(e), "layer": "karpathy_weekly"}
    _log_run("karpathy_weekly", summary)

    # Auto-deploy if successful
    if not summary.get("error"):
        deploy = deploy_best_to_live()
        summary["deploy"] = deploy

    return summary


def run_asi_evolve_layer(
    n_candidates: int = 20, use_llm: bool = False
) -> dict[str, Any]:
    """Run Layer 2 (ASI-Evolve daily) + auto-deploy."""
    logger.info("=== Layer 2: ASI-Evolve daily ===")
    try:
        summary = run_asi_evolve_daily(
            n_candidates=n_candidates,
            use_llm=use_llm,
        )
    except Exception as e:
        logger.exception("Layer 2 failed: %s", e)
        summary = {"error": str(e), "layer": "asi_evolve_daily"}
    _log_run("asi_evolve_daily", summary)

    if not summary.get("error"):
        deploy = deploy_best_to_live()
        summary["deploy"] = deploy

    return summary


def run_sia_layer(use_llm: bool = False) -> dict[str, Any]:
    """Run Layer 3 (SIA hourly) + auto-deploy."""
    logger.info("=== Layer 3: SIA hourly ===")
    try:
        summary = run_sia_hourly(use_llm=use_llm)
    except Exception as e:
        logger.exception("Layer 3 failed: %s", e)
        summary = {"error": str(e), "layer": "sia_hourly"}
    _log_run("sia_hourly", summary)

    if not summary.get("error"):
        deploy = deploy_best_to_live()
        summary["deploy"] = deploy

    return summary


# ---------------------------------------------------------------------------
# Full cycle (all 3 layers back-to-back)
# ---------------------------------------------------------------------------


def run_full_cycle(
    karpathy_rounds: int = 6,
    asi_evolve_candidates: int = 20,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Run all 3 layers back-to-back, then deploy the global best.

    Useful for initial bootstrap or integration testing.
    In production, schedule each layer separately (hourly/daily/weekly).
    """
    logger.info("############ FULL LLM-LOOP CYCLE START ############")
    start = datetime.now(UTC)

    k_summary = run_karpathy_layer(rounds=karpathy_rounds, use_llm=use_llm)
    a_summary = run_asi_evolve_layer(
        n_candidates=asi_evolve_candidates, use_llm=use_llm
    )
    s_summary = run_sia_layer(use_llm=use_llm)

    # Final deploy picks the global best across all 3 layers
    final_deploy = deploy_best_to_live()

    elapsed = (datetime.now(UTC) - start).total_seconds()
    logger.info("############ FULL CYCLE DONE in %.1fs ############", elapsed)

    return {
        "karpathy_weekly": k_summary,
        "asi_evolve_daily": a_summary,
        "sia_hourly": s_summary,
        "final_deploy": final_deploy,
        "elapsed_seconds": elapsed,
        "completed_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Status (for dashboard / debugging)
# ---------------------------------------------------------------------------


def get_status() -> dict[str, Any]:
    """Return a snapshot of the 3-layer loop's current state."""
    k_best = _load_json(os.path.join(DATA_DIR, "karpathy_best.json"))
    a_best = _load_json(os.path.join(DATA_DIR, "asi_evolve_best.json"))
    s_best = _load_json(os.path.join(DATA_DIR, "sia_hourly_best.json"))
    live_weights = _load_json(LIVE_WEIGHTS_PATH)
    live_strategy = _load_json(LIVE_STRATEGY_PATH)

    return {
        "layers": {
            "karpathy_weekly": {
                "best_description": k_best.get("description") if k_best else None,
                "best_stats": k_best.get("stats") if k_best else None,
                "saved_at": k_best.get("saved_at") if k_best else None,
            },
            "asi_evolve_daily": {
                "best_description": a_best.get("description") if a_best else None,
                "best_stats": a_best.get("stats") if a_best else None,
                "saved_at": a_best.get("saved_at") if a_best else None,
            },
            "sia_hourly": {
                "best_description": s_best.get("description") if s_best else None,
                "best_stats": s_best.get("stats") if s_best else None,
                "saved_at": s_best.get("saved_at") if s_best else None,
            },
        },
        "live_trader": {
            "weights": live_weights,
            "strategy_params": live_strategy,
        },
        "global_best": find_global_best()[2],  # source layer name
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="3-layer LLM loop orchestrator")
    parser.add_argument(
        "command",
        choices=["karpathy", "asi_evolve", "sia", "full", "status", "deploy"],
        help="Which command to run",
    )
    parser.add_argument("--rounds", type=int, default=6, help="Karpathy rounds")
    parser.add_argument(
        "--candidates", type=int, default=20, help="ASI-Evolve candidates"
    )
    parser.add_argument("--llm", action="store_true", help="Use LLM where available")
    args = parser.parse_args()

    if args.command == "karpathy":
        out = run_karpathy_layer(rounds=args.rounds, use_llm=args.llm)
    elif args.command == "asi_evolve":
        out = run_asi_evolve_layer(n_candidates=args.candidates, use_llm=args.llm)
    elif args.command == "sia":
        out = run_sia_layer(use_llm=args.llm)
    elif args.command == "full":
        out = run_full_cycle(
            karpathy_rounds=args.rounds,
            asi_evolve_candidates=args.candidates,
            use_llm=args.llm,
        )
    elif args.command == "status":
        out = get_status()
    elif args.command == "deploy":
        out = deploy_best_to_live()
    else:
        out = {"error": f"unknown command: {args.command}"}

    print(json.dumps(out, indent=2, default=str))
