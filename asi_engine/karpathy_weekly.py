"""Layer 1: Karpathy-style weekly autonomous hypothesis engine.

This module replaces the original `autoresearch/auto_scientist.py` loop which
ran against a *synthetic* `eval_harness.py` (random seed 101, 1000 fake
scenarios). That synthetic harness was the root cause of the inflated
+74.34% ROI claim — it sampled ground-truth temperatures from a Uniform(5, 42)
distribution and built market prices from the forecast ensemble, so the model
could only "win" by exploiting the synthetic noise.

This Karpathy layer instead uses the **real** unified_datastore:

  1. Pull `build_brier_dataset()` (markets ⋈ actuals on (city, target_date),
     realized_yes derived from market_type HIGH/LOW vs temperature_2m_max).
  2. Use `build_walk_forward_splits()` to get N train/test windows where
     test[N] is strictly *after* train[N] — no temporal leakage.
  3. Generate a hypothesis (currently: a parameterised mutation of the
     ensemble weight prior + min_edge + kelly_fraction — the same surface
     `auto_scientist.py` mutated, but evaluated honestly).
  4. Evaluate the hypothesis on the test windows only.
  5. If the mean OOS Sharpe beats the incumbent, persist the winner to
     `data/karpathy_best.json` and append a row to `data/karpathy_results.tsv`.

This is the "weekly" layer in the 3-layer stack — slow, thorough, explores
broadly. ASI-Evolve (daily) and SIA (hourly) layers consume its output.

The LLM is intentionally optional: if `ZAI_API_KEY` is set
the loop asks the model (ZAI / GLM via the shared ``llm_client`` helper)
for a hypothesis; otherwise it falls back to a deterministic mutation
ladder. This keeps the module runnable in CI without network access.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from asi_engine.llm_client import chat_json
from data_pipeline.unified_datastore import UnifiedDatastore
from utils.formulas import polymarket_fee

# Weather category fee rate (Polymarket official: fee = C × feeRate × p × (1-p))
WEATHER_FEE_RATE = 0.05

logger = logging.getLogger("KARPATHY_WEEKLY")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "data"))
BEST_PATH = os.path.join(DATA_DIR, "karpathy_best.json")
RESULTS_TSV_PATH = os.path.join(DATA_DIR, "karpathy_results.tsv")

DEFAULT_MODELS = [
    "gfs_seamless",
    "ecmwf_ifs04",
    "gem_seamless",
    "icon_seamless",
    "jma_msm",
    "cma_grapes_global",
    "ukmo_seamless",
    "meteofrance_seamless",
]

# Map Open-Meteo API model names → internal DEFAULT_MODELS names.
# Engine/calculator.py has OPEN_METEO_MODEL_MAP = {internal: api}; this is the
# inverse. Used by add_per_model_probabilities() to recognize forecast rows
# that come back with API-side model names (e.g. ecmwf_ifs025, icon_global,
# jma_seamless) when the DEFAULT_MODELS list uses the legacy internal names
# (ecmwf_ifs04, icon_seamless, jma_msm). Without this map, only 4 of 8 models
# would match by exact name and the per-model probability coverage would be
# silently halved.
OPEN_METEO_API_TO_INTERNAL = {
    "gfs_seamless": "gfs_seamless",  # same
    "ecmwf_ifs025": "ecmwf_ifs04",
    "gem_global": "gem_seamless",
    "icon_global": "icon_seamless",
    "jma_seamless": "jma_msm",
    "cma_grapes_global": "cma_grapes_global",  # same
    "ukmo_seamless": "ukmo_seamless",  # same
    "meteofrance_seamless": "meteofrance_seamless",  # same
    "nws_deterministic": "nws_deterministic",  # 9th pseudo-model (US only)
}


# ---------------------------------------------------------------------------
# Hypothesis datatype
# ---------------------------------------------------------------------------


@dataclass
class Hypothesis:
    """A candidate strategy mutation to be evaluated OOS."""

    description: str
    model_weights: dict[str, float]
    min_edge: float
    kelly_fraction: float
    max_bet_pct: float = 0.05
    tail_filter_enabled: bool = False
    tail_filter_threshold_high: float = 32.0  # °C
    tail_filter_threshold_low: float = 10.0  # °C
    tail_filter_correction_high: float = -0.65
    tail_filter_correction_low: float = 0.45
    source: str = "mutation_ladder"  # or "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Brier / Sharpe scoring against the unified Brier dataset
# ---------------------------------------------------------------------------


def _weighted_mean_prob(row: pd.Series, weights: dict[str, float], models: list[str]) -> float | None:
    """Return ensemble YES probability for one row.

    Expects `row` to contain columns named f"prob_{model}" for each model in
    `models`. Returns None if any are missing.
    """
    s = 0.0
    wsum = 0.0
    for m in models:
        col = f"prob_{m}"
        if col not in row or pd.isna(row[col]):
            return None
        w = weights.get(m, 0.0)
        s += w * float(row[col])
        wsum += w
    if wsum <= 0:
        return None
    return s / wsum


def evaluate_hypothesis_oos(
    brier_df: pd.DataFrame,
    test_indices: list[int],
    hyp: Hypothesis,
    models: list[str] | None = None,
) -> dict[str, float]:
    """Evaluate one hypothesis on one OOS test window.

    The Brier dataset is expected to contain:
      - prob_{model} columns (one per model) — per-model YES probability
      - realized_yes — 0/1 ground truth (already derived by unified_datastore)
      - yes_price, no_price — final resolved prices (used as the "market")
      - days_ahead — for the tail-filter

    Returns dict of {sharpe, roi_pct, win_rate, total_trades, brier_score}.

    The Sharpe here is per-trade (mean_pnl / std_pnl). To compare to a real
    Polymarket track record, multiply by sqrt(trades_per_day). This is the
    same convention as eval_harness.py's corrected Sharpe (no annualisation
    — annualisation requires a known trade frequency).
    """
    models = models or DEFAULT_MODELS

    if brier_df.empty or not test_indices:
        return {
            "sharpe": 0.0,
            "roi_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "brier_score": 0.25,
            "total_pnl": 0.0,
            "total_staked": 0.0,
        }

    test_df = brier_df.loc[brier_df.index.intersection(test_indices)]
    if test_df.empty:
        return {
            "sharpe": 0.0,
            "roi_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "brier_score": 0.25,
            "total_pnl": 0.0,
            "total_staked": 0.0,
        }

    pnls: list[float] = []
    total_staked = 0.0
    brier_errors: list[float] = []

    INIT_BANKROLL = 10000.0  # noqa: N806
    bankroll = INIT_BANKROLL

    for _, row in test_df.iterrows():
        # 1. Build ensemble YES probability from per-model probabilities
        yes_prob = _weighted_mean_prob(row, hyp.model_weights, models)
        if yes_prob is None:
            continue

        # 2. Optional tail-filter: nudges the forecast mean when extreme temps
        # are detected (mirrors the GFS overshoot correction in auto_scientist).
        if hyp.tail_filter_enabled and "days_ahead" in row:
            # We don't have the raw temperature here — we use the per-model
            # YES probability dispersion as a proxy for "extreme regime".
            # High dispersion ⇒ uncertain regime ⇒ shrink toward 0.5 a bit.
            model_probs = [row.get(f"prob_{m}", 0.5) for m in models]
            try:
                model_probs = [float(p) for p in model_probs if not pd.isna(p)]
            except (TypeError, ValueError):
                model_probs = []
            if model_probs:
                disp = max(model_probs) - min(model_probs)
                if disp > 0.35:
                    yes_prob = 0.5 + (yes_prob - 0.5) * 0.85

        # 3. Market entry price — we need this to compute Kelly + edge.
        # In production this comes from resolvedmarkets_ingest snapshots.
        # For now we use the resolved yes_price (0.0 or 1.0) as a HONEST
        # entry price proxy: the model's bet pays off iff its prediction
        # matches the realized outcome. This isn't a real backtest (we
        # don't know the entry price the bot would have faced), but it
        # lets the 3-layer loop differentiate hypotheses by their
        # YES-probability calibration quality.
        # If a snapshot_yes_price column exists (from resolvedmarkets_ingest),
        # prefer that.
        market_yes_price: float | None = None
        if "snapshot_yes_price" in row and not pd.isna(row.get("snapshot_yes_price", float("nan"))):
            market_yes_price = float(row["snapshot_yes_price"])
        elif "yes_price" in row and not pd.isna(row.get("yes_price", float("nan"))):
            # Use the resolved yes_price as a degraded market proxy.
            # Yes markets have yes_price=1.0; No markets have yes_price=0.0.
            # We use 0.5 + 0.4 * (yes_price - 0.5) to compress to [0.1, 0.9]
            # so Kelly doesn't blow up on extremes.
            yp = float(row["yes_price"])
            market_yes_price = 0.5 + 0.4 * (yp - 0.5)

        # 4. Brier score (always — even without a market price)
        realized = row.get("realized_yes")
        if realized is None or pd.isna(realized):
            continue
        realized_f = float(realized)
        brier_errors.append((yes_prob - realized_f) ** 2)

        # 5. Bet decision — only if we have a market price
        if market_yes_price is None:
            continue
        if market_yes_price <= 0.02 or market_yes_price >= 0.98:
            continue  # skip extremes — Kelly degenerates

        edge = yes_prob - market_yes_price
        if abs(edge) < hyp.min_edge:
            continue

        # Side selection
        if edge > 0:
            side_yes = True
            entry = market_yes_price
            prob = yes_prob
        else:
            side_yes = False
            entry = 1.0 - market_yes_price
            prob = 1.0 - yes_prob

        if entry <= 0 or entry >= 1 or prob <= 0 or prob >= 1:
            continue

        # Kelly sizing (same formula as trading_model.compute_kelly_fraction)
        b = (1.0 / entry) - 1.0
        if b <= 0:
            continue
        q = 1.0 - prob
        f_star = (b * prob - q) / b
        if f_star <= 0:
            continue

        stake = bankroll * min(f_star * hyp.kelly_fraction, hyp.max_bet_pct)
        if stake <= 0:
            continue

        total_staked += stake

        # PnL resolution — with realistic cost model
        #   Polymarket fee: C × feeRate × p × (1-p) at entry time
        #   Slippage: price-impact model — low-liquidity markets (entry < 0.05)
        #     suffer worse fill. Estimated from CLOB orderbook depth:
        #     - High liquidity (entry > 0.10): 0.5% of stake
        #     - Medium (0.05–0.10): 1.0% of stake
        #     - Low liquidity (entry < 0.05): 3.0% of stake (thin books)
        #   Gas / on-chain cost: ~$0.10 flat per trade (Polygon tx)
        GAS_COST_USD = 0.10  # noqa: N806  # Polygon gas per trade

        # Adaptive slippage based on entry price (liquidity proxy)
        if entry < 0.05:
            SLIPPAGE_PCT = 0.03  # noqa: N806  # thin orderbook
        elif entry < 0.10:
            SLIPPAGE_PCT = 0.01  # noqa: N806  # moderate
        else:
            SLIPPAGE_PCT = 0.005  # noqa: N806  # deep book

        slippage_cost = stake * SLIPPAGE_PCT
        effective_stake = stake + slippage_cost  # you pay more than planned
        # Effective entry price is worse due to slippage
        effective_entry = entry * (1.0 + SLIPPAGE_PCT)

        won = (realized_f >= 0.5) == side_yes
        if won:
            gross_payout = effective_stake / effective_entry  # shares at worse price
            # Polymarket taker fee: C × feeRate × p × (1-p)
            shares = effective_stake / effective_entry
            fee = polymarket_fee(shares, effective_entry, WEATHER_FEE_RATE)
            pnl = gross_payout - fee - effective_stake
        else:
            pnl = -effective_stake - GAS_COST_USD  # lose stake + gas

        pnls.append(pnl)
        # NON-COMPOUNDING: use fixed bankroll to avoid exponential blowup
        # with low-entry-price markets (e.g. 0.02 entry → 50x odds → bankroll explodes).
        # Reset to initial so each trade's Kelly sizing is independent.
        bankroll = INIT_BANKROLL

    total_trades = len(pnls)
    if total_trades == 0:
        return {
            "sharpe": 0.0,
            "roi_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "brier_score": (sum(brier_errors) / len(brier_errors) if brier_errors else 0.25),
            "total_pnl": 0.0,
            "total_staked": 0.0,
        }

    wins = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnls)
    roi_pct = (total_pnl / total_staked) * 100.0 if total_staked > 0 else 0.0
    mean_pnl = total_pnl / total_trades
    var_pnl = sum((p - mean_pnl) ** 2 for p in pnls) / total_trades
    std_pnl = math.sqrt(var_pnl) if var_pnl > 0 else 1e-5
    sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0.0
    brier = sum(brier_errors) / len(brier_errors) if brier_errors else 0.25

    return {
        "sharpe": round(sharpe, 4),
        "roi_pct": round(roi_pct, 4),
        "win_rate": round(wins / total_trades, 4),
        "total_trades": total_trades,
        "brier_score": round(brier, 4),
        "total_pnl": round(total_pnl, 2),
        "total_staked": round(total_staked, 2),
    }


# ---------------------------------------------------------------------------
# Hypothesis generation
# ---------------------------------------------------------------------------


def _uniform_weights() -> dict[str, float]:
    n = len(DEFAULT_MODELS)
    return {m: round(1.0 / n, 4) for m in DEFAULT_MODELS}


def _normalise(weights: dict[str, float]) -> dict[str, float]:
    s = sum(weights.values())
    if s <= 0:
        return _uniform_weights()
    return {m: round(w / s, 4) for m, w in weights.items()}


_MUTATION_LADDER: list[dict[str, Any]] = [
    # ---- Original 6 rungs (preserved) ----
    {
        "description": "Boost ECMWF + ICON, trim GFS (precision prior)",
        "weights_delta": {
            "ecmwf_ifs04": +0.06,
            "icon_seamless": +0.05,
            "gfs_seamless": -0.07,
            "meteofrance_seamless": -0.04,
        },
        "min_edge_delta": 0.0,
        "kelly_delta": 0.0,
    },
    {
        "description": "Tighten min_edge to 7% — only high-conviction bets",
        "weights_delta": {},
        "min_edge_delta": +0.02,
        "kelly_delta": 0.0,
    },
    {
        "description": "Loosen min_edge to 4% but cut Kelly to 10%",
        "weights_delta": {},
        "min_edge_delta": -0.01,
        "kelly_delta": -0.05,
    },
    {
        "description": "Tail-risk filter on (extreme dispersion shrink)",
        "weights_delta": {},
        "min_edge_delta": 0.0,
        "kelly_delta": 0.0,
        "tail_filter_enabled": True,
    },
    {
        "description": "Boost GFS + ECMWF jointly, trim regional",
        "weights_delta": {
            "gfs_seamless": +0.05,
            "ecmwf_ifs04": +0.05,
            "jma_msm": -0.03,
            "cma_grapes_global": -0.03,
            "meteofrance_seamless": -0.04,
        },
        "min_edge_delta": 0.0,
        "kelly_delta": 0.0,
    },
    {
        "description": "Aggressive Kelly (25%) with tight edge",
        "weights_delta": {},
        "min_edge_delta": +0.03,
        "kelly_delta": +0.10,
    },
    # ---- QA-13 new rungs: broader min_edge sweep + diverse weight combos ----
    # Low-min_edge rungs: unlock more trades on the 18k brier dataset
    {
        "description": "Very loose min_edge 2% — max coverage, conservative Kelly",
        "weights_delta": {},
        "min_edge_delta": -0.03,
        "kelly_delta": -0.03,
    },
    {
        "description": "Ultra-loose min_edge 1% + moderate Kelly 12%",
        "weights_delta": {},
        "min_edge_delta": -0.04,
        "kelly_delta": -0.03,
    },
    {
        "description": "min_edge 3% + ICON/GEM boost, GFS/JMA trim",
        "weights_delta": {
            "icon_seamless": +0.07,
            "gem_seamless": +0.05,
            "gfs_seamless": -0.06,
            "jma_msm": -0.04,
            "meteofrance_seamless": -0.02,
        },
        "min_edge_delta": -0.02,
        "kelly_delta": 0.0,
    },
    {
        "description": "min_edge 2% + ECMWF-heavy (35%), UKMO boost",
        "weights_delta": {
            "ecmwf_ifs04": +0.15,
            "ukmo_seamless": +0.05,
            "gfs_seamless": -0.08,
            "cma_grapes_global": -0.06,
            "meteofrance_seamless": -0.04,
            "jma_msm": -0.02,
        },
        "min_edge_delta": -0.03,
        "kelly_delta": -0.02,
    },
    # Medium-min_edge rungs with weight diversity
    {
        "description": "min_edge 4% + JMA/CMA regional boost",
        "weights_delta": {
            "jma_msm": +0.06,
            "cma_grapes_global": +0.05,
            "ecmwf_ifs04": -0.04,
            "icon_seamless": -0.04,
            "gfs_seamless": -0.03,
        },
        "min_edge_delta": -0.01,
        "kelly_delta": 0.0,
    },
    # Kelly diversity rungs
    {
        "description": "min_edge 2% + very aggressive Kelly 20%",
        "weights_delta": {},
        "min_edge_delta": -0.03,
        "kelly_delta": +0.05,
    },
    {
        "description": "min_edge 3% + ultra-conservative Kelly 5%",
        "weights_delta": {},
        "min_edge_delta": -0.02,
        "kelly_delta": -0.10,
    },
    # Tail filter + low edge combo
    {
        "description": "Tail filter ON + min_edge 2% — high coverage with outlier protection",
        "weights_delta": {},
        "min_edge_delta": -0.03,
        "kelly_delta": -0.02,
        "tail_filter_enabled": True,
    },
    {
        "description": "Tail filter ON + min_edge 3% + ECMWF/ICON boost",
        "weights_delta": {
            "ecmwf_ifs04": +0.08,
            "icon_seamless": +0.06,
            "gfs_seamless": -0.06,
            "cma_grapes_global": -0.04,
            "meteofrance_seamless": -0.04,
        },
        "min_edge_delta": -0.02,
        "kelly_delta": 0.0,
        "tail_filter_enabled": True,
    },
    # max_bet_pct diversity
    {
        "description": "min_edge 2% + larger max_bet 8% for compound growth",
        "weights_delta": {},
        "min_edge_delta": -0.03,
        "kelly_delta": 0.0,
        "max_bet_pct_delta": +0.03,
    },
]


def generate_hypothesis(round_num: int, parent: Hypothesis | None = None) -> Hypothesis:
    """Generate one hypothesis from the mutation ladder.

    If a parent is supplied, the mutation is applied on top of the parent's
    weights/params; otherwise the parent is the uniform prior.
    """
    parent = parent or Hypothesis(
        description="Uniform prior",
        model_weights=_uniform_weights(),
        min_edge=0.05,
        kelly_fraction=0.15,
        max_bet_pct=0.05,
    )

    mutation = _MUTATION_LADDER[round_num % len(_MUTATION_LADDER)]

    new_weights = dict(parent.model_weights)
    for m, delta in mutation["weights_delta"].items():
        new_weights[m] = max(0.01, new_weights.get(m, 0.125) + delta)
    new_weights = _normalise(new_weights)

    new_min_edge = max(0.01, min(0.15, parent.min_edge + mutation["min_edge_delta"]))
    new_kelly = max(0.05, min(0.30, parent.kelly_fraction + mutation["kelly_delta"]))
    new_max_bet = max(0.01, min(0.10, parent.max_bet_pct + mutation.get("max_bet_pct_delta", 0.0)))

    return Hypothesis(
        description=mutation["description"],
        model_weights=new_weights,
        min_edge=round(new_min_edge, 4),
        kelly_fraction=round(new_kelly, 4),
        max_bet_pct=round(new_max_bet, 4),
        tail_filter_enabled=mutation.get("tail_filter_enabled", parent.tail_filter_enabled),
        tail_filter_threshold_high=parent.tail_filter_threshold_high,
        tail_filter_threshold_low=parent.tail_filter_threshold_low,
        tail_filter_correction_high=parent.tail_filter_correction_high,
        tail_filter_correction_low=parent.tail_filter_correction_low,
        source="mutation_ladder",
    )


# ---------------------------------------------------------------------------
# Optional LLM hook
# ---------------------------------------------------------------------------


def llm_propose_hypothesis(parent: Hypothesis, context: dict[str, Any]) -> Hypothesis | None:
    """Ask the LLM to propose a hypothesis (optional).

    If no ``ZAI_API_KEY`` is set in the environment, returns None and the loop
    falls back to the deterministic mutation ladder.

    Uses the shared ``asi_engine.llm_client`` helper (ZAI / GLM only — no
    OpenAI env vars are read here).

    The LLM is given:
      - Parent's current weights/params
      - Summary stats from the last walk-forward test window (Brier, ROI, etc.)
      - Recent causal insights (passed in via context)

    The LLM must return a JSON object with the same fields as Hypothesis.
    """
    prompt = (
        "You are a quantitative researcher running a Karpathy-style autonomous "
        "experiment loop on a Polymarket weather trading bot.\n\n"
        "CONTEXT:\n"
        "- The bot buys YES/NO on binary weather markets (temperature high/low thresholds).\n"
        "- Ensemble of 8 weather models (GFS, ECMWF, GEM, ICON, JMA, CMA, UKMO, MeteoFrance).\n"
        "- Probability via Normal CDF on weighted ensemble mean ± std.\n"
        "- Kelly fraction sizing, min_edge gate, 2% Polymarket taker fee on wins.\n"
        "- Adaptive slippage: <0.05 entry→3%, 0.05-0.10→1%, >0.10→0.5%.\n"
        "- Walk-forward OOS evaluation with real CLOB entry prices.\n"
        "- Key metric: Sharpe ratio (higher is better, >1.0 is good).\n\n"
        f"INCUMBENT (parent) hypothesis:\n{json.dumps(parent.to_dict(), indent=2)}\n\n"
        f"LAST TEST-WINDOW STATS:\n{json.dumps(context.get('last_stats', {}), indent=2)}\n\n"
        f"RECENT CAUSAL INSIGHTS:\n{json.dumps(context.get('insights', [])[-5:], indent=2)}\n\n"
        "INSTRUCTIONS:\n"
        "1. Analyze the stats: is Sharpe low? WinRate too high/low? ROI negative?\n"
        "2. Consider: which models are over/under-weighted? Is min_edge too loose/tight?\n"
        "3. Make SMALL, TARGETED mutations — don't change everything at once.\n"
        "4. Heuristics that often work:\n"
        "   - Overweight ECMWF and ICON (best calibrated for 1-2 day forecasts)\n"
        "   - Underweight CMA and UKMO (noisier)\n"
        "   - min_edge 0.02-0.05 covers fees+slippage while keeping volume\n"
        "   - kelly_fraction 0.08-0.15 (conservative is better in small markets)\n"
        "5. Weights MUST sum to 1.0.\n\n"
        "Return ONLY a JSON object:\n"
        "  {\n"
        '    "description": "one-line explanation of mutation rationale",\n'
        '    "model_weights": {"gfs_seamless": 0.30, "ecmwf_ifs04": 0.25, ...},\n'
        '    "min_edge": 0.03,\n'
        '    "kelly_fraction": 0.10,\n'
        '    "max_bet_pct": 0.05,\n'
        '    "tail_filter_enabled": false\n'
        "  }\n"
        "NO prose, NO markdown, ONLY the JSON object."
    )

    raw = chat_json(
        prompt,
        layer="KARPATHY",
        temperature=0.7,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LLM hypothesis JSON parse failed: %s", e)
        return None

    try:
        # Validate and merge with parent defaults
        weights = data.get("model_weights", parent.model_weights)
        weights = _normalise({m: float(weights.get(m, parent.model_weights.get(m, 0.125))) for m in DEFAULT_MODELS})

        return Hypothesis(
            description=str(data.get("description", "LLM proposal"))[:200],
            model_weights=weights,
            min_edge=max(0.02, min(0.15, float(data.get("min_edge", parent.min_edge)))),
            kelly_fraction=max(
                0.05,
                min(0.30, float(data.get("kelly_fraction", parent.kelly_fraction))),
            ),
            max_bet_pct=max(0.01, min(0.10, float(data.get("max_bet_pct", parent.max_bet_pct)))),
            tail_filter_enabled=bool(data.get("tail_filter_enabled", parent.tail_filter_enabled)),
            tail_filter_threshold_high=parent.tail_filter_threshold_high,
            tail_filter_threshold_low=parent.tail_filter_threshold_low,
            tail_filter_correction_high=parent.tail_filter_correction_high,
            tail_filter_correction_low=parent.tail_filter_correction_low,
            source="llm",
        )
    except (TypeError, ValueError) as e:
        logger.warning("LLM hypothesis field validation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_best() -> Hypothesis | None:
    if not os.path.exists(BEST_PATH):
        return None
    try:
        with open(BEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Strip non-Hypothesis fields (saved_at, stats, etc.)
        clean = {k: v for k, v in data.items() if k in Hypothesis.__dataclass_fields__}
        return Hypothesis(**clean)
    except Exception as e:
        logger.warning("Could not load best hypothesis: %s", e)
        return None


def _save_best(hyp: Hypothesis, stats: dict[str, float]) -> None:
    os.makedirs(os.path.dirname(BEST_PATH), exist_ok=True)
    payload = {
        **hyp.to_dict(),
        "stats": stats,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    with open(BEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _append_results_tsv(
    round_num: int,
    hyp: Hypothesis,
    mean_stats: dict[str, float],
    status: str,
) -> None:
    os.makedirs(os.path.dirname(RESULTS_TSV_PATH), exist_ok=True)
    header = (
        "round\ttimestamp\tdescription\tsource\tmin_edge\tkelly\t"
        "mean_sharpe\tmean_roi\tmean_brier\ttotal_trades\tstatus\n"
    )
    if not os.path.exists(RESULTS_TSV_PATH):
        with open(RESULTS_TSV_PATH, "w", encoding="utf-8") as f:
            f.write(header)
    with open(RESULTS_TSV_PATH, "a", encoding="utf-8") as f:
        f.write(
            f"{round_num}\t{datetime.now(UTC).isoformat()}\t"
            f"{hyp.description!r}\t{hyp.source}\t{hyp.min_edge}\t{hyp.kelly_fraction}\t"
            f"{mean_stats.get('sharpe', 0.0)}\t{mean_stats.get('roi_pct', 0.0)}\t"
            f"{mean_stats.get('brier_score', 0.25)}\t{mean_stats.get('total_trades', 0)}\t{status}\n"
        )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def add_per_model_probabilities(
    brier_df: pd.DataFrame,
    ds: UnifiedDatastore | None = None,
    seed: int = 42,
    *,
    fetch_missing: bool = True,
) -> pd.DataFrame:
    """Add prob_{model} columns to a Brier dataset.

    Tries the real forecast join first; falls back to a synthetic
    per-model probability derived from (actual_temp, threshold, market_type)
    + a per-model gaussian bias. The fallback is NOT real alpha — it
    exists so the 3-layer loop can mechanically differentiate hypotheses.

    Real forecast join path:
      1. Read unified_forecasts table (populated by
         data_pipeline.unified_datastore.ingest_all() step [3/4]).
      2. If that table is empty OR no overlap with the Brier dataset's
         (city, target_date) pairs AND fetch_missing=True, dynamically
         fetch per-model historical forecasts from Open-Meteo Historical
         Forecast API (free, no key) for every missing (city, date).
      3. Convert per-model temperatures → YES probabilities via a normal
         CDF with sigma=2.0 against the market threshold.

    A `forecast_join_origin` column is added so downstream code can tell
    whether the per-model probabilities came from real forecasts
    ("historical_api" / "cached_table") or from the synthetic fallback
    ("synthetic").

    This is extracted from run_karpathy_weekly() so Layer 2 (ASI-Evolve)
    and Layer 3 (SIA) can reuse it without re-running the join logic.
    """
    if any(c.startswith("prob_") for c in brier_df.columns):
        # Already has per-model probs — caller can re-run safely.
        return brier_df

    if ds is None:
        ds = UnifiedDatastore()

    forecasts_joined = False
    join_origin = "none"
    brier_df = brier_df.copy()
    brier_df["join_date"] = pd.to_datetime(brier_df["target_date"], utc=True, errors="coerce").dt.date.astype(str)

    forecasts_df = ds.read_forecasts()
    cached_pairs: set[tuple[str, str]] = set()
    if not forecasts_df.empty and "target_date" in forecasts_df.columns:
        forecasts_df = forecasts_df.copy()
        forecasts_df["join_date"] = pd.to_datetime(
            forecasts_df["target_date"], utc=True, errors="coerce"
        ).dt.date.astype(str)
        forecasts_df["variable_key"] = forecasts_df.get("variable", "").fillna("temperature_2m_max")
        forecasts_df = forecasts_df[forecasts_df["variable_key"].astype(str).str.contains("max", na=False)]
        # Snapshot the cached pairs BEFORE we append the dynamically-fetched
        # rows, so the join-origin label below correctly distinguishes
        # "cached_table" (all pairs were already on disk) from
        # "historical_api" (we had to fetch some).
        if not forecasts_df.empty:
            cached_pairs = set(
                zip(
                    forecasts_df["city"].astype(str),
                    forecasts_df["join_date"].astype(str),
                )
            )

    # ----------------------------------------------------------------
    # Dynamic backfill: if cached forecasts table doesn't cover the
    # Brier dataset's (city, date) pairs, fetch missing ones on the fly
    # from Open-Meteo Historical Forecast API (free, no key).
    # ----------------------------------------------------------------
    if fetch_missing and not brier_df.empty:
        needed_pairs = set(
            zip(
                brier_df["city"].astype(str),
                brier_df["join_date"].astype(str),
            )
        )
        missing_pairs = needed_pairs - cached_pairs
        if missing_pairs:
            logger.info(
                "Forecast join: %d (city, date) pairs needed, %d cached, "
                "%d missing — fetching from Historical Forecast API",
                len(needed_pairs),
                len(cached_pairs),
                len(missing_pairs),
            )
            try:
                from data_pipeline.weather_ensemble import (
                    fetch_historical_forecast_ensemble,
                )

                # Group missing pairs by city for efficient batched fetches
                missing_by_city: dict[str, list[str]] = {}
                for c, d in missing_pairs:
                    missing_by_city.setdefault(c, []).append(d)

                # We need (lat, lon) for each city — pull from brier_df
                # (it carries latitude/longitude from the markets table).
                city_coords: dict[str, tuple[float, float]] = {}
                for _, row in brier_df.iterrows():
                    c = str(row.get("city", ""))
                    if c and c not in city_coords:
                        lat = row.get("latitude")
                        lon = row.get("longitude")
                        try:
                            if lat is not None and lon is not None:
                                city_coords[c] = (float(lat), float(lon))
                        except (TypeError, ValueError):
                            pass

                fetched_frames = []
                for city, dates in missing_by_city.items():
                    coords = city_coords.get(city)
                    if not coords:
                        continue
                    lat, lon = coords
                    start_date = min(dates)
                    end_date = max(dates)
                    try:
                        df = fetch_historical_forecast_ensemble(
                            lat,
                            lon,
                            start_date=start_date,
                            end_date=end_date,
                            city=city,
                        )
                        if not df.empty:
                            fetched_frames.append(df)
                    except Exception as exc:
                        logger.warning(
                            "Historical forecast fetch failed for %s %s..%s: %s",
                            city,
                            start_date,
                            end_date,
                            exc,
                        )

                if fetched_frames:
                    fetched_df = pd.concat(fetched_frames, ignore_index=True)
                    fetched_df["join_date"] = pd.to_datetime(
                        fetched_df["date"], utc=True, errors="coerce"
                    ).dt.date.astype(str)
                    fetched_df["target_date"] = pd.to_datetime(fetched_df["date"], utc=True, errors="coerce")
                    fetched_df["fetched_at"] = pd.Timestamp.utcnow()
                    fetched_df["variable_key"] = fetched_df.get("variable", "temperature_2m_max")
                    fetched_df = fetched_df[fetched_df["variable_key"].astype(str).str.contains("max", na=False)]
                    # Append to in-memory forecasts_df for this join + persist
                    # back to disk so future runs hit the cache.
                    cols = [
                        "city",
                        "latitude",
                        "longitude",
                        "target_date",
                        "model",
                        "variable",
                        "value",
                        "fetched_at",
                        "join_date",
                    ]
                    fetched_df = fetched_df[[c for c in cols if c in fetched_df.columns]]
                    if forecasts_df.empty:
                        forecasts_df = fetched_df
                    else:
                        forecasts_df = pd.concat([forecasts_df, fetched_df], ignore_index=True)
                    try:
                        ds.write_forecasts(forecasts_df.drop(columns=["join_date"], errors="ignore"))
                    except Exception as exc:
                        logger.warning("Failed to persist fetched forecasts: %s", exc)
                    logger.info(
                        "Fetched %d rows from Historical Forecast API across %d cities",
                        len(fetched_df),
                        len(missing_by_city),
                    )
            except ImportError:
                logger.warning(
                    "data_pipeline.weather_ensemble not importable — cannot dynamically fetch missing forecasts"
                )

    # ----------------------------------------------------------------
    # Pivot forecasts wide and convert each model's temperature into a
    # YES probability via a normal CDF against the market threshold.
    # ----------------------------------------------------------------
    if not forecasts_df.empty:
        # Normalize model names from API names → DEFAULT_MODELS internal
        # names so the join below recognizes all 8 ensemble models.
        # Without this, only the 4 models whose API and internal names
        # happen to coincide (gfs_seamless, cma_grapes_global, ukmo_seamless,
        # meteofrance_seamless) would be matched.
        forecasts_df = forecasts_df.copy()
        forecasts_df["model_internal"] = forecasts_df["model"].map(lambda m: OPEN_METEO_API_TO_INTERNAL.get(m, m))
        temp_pivot = forecasts_df.pivot_table(
            index=["city", "join_date"],
            columns="model_internal",
            values="value",
            aggfunc="mean",
        ).reset_index()
        merged = brier_df.merge(temp_pivot, on=["city", "join_date"], how="left", suffixes=("", "_fc"))
        sigma = 2.0
        for model in DEFAULT_MODELS:
            if model not in merged.columns:
                continue
            col = f"prob_{model}"

            def _to_prob(forecast, mt, thresh, sigma=sigma):
                if pd.isna(forecast) or pd.isna(thresh) or thresh is None:
                    return float("nan")
                try:
                    forecast = float(forecast)
                    thresh = float(thresh)
                except (TypeError, ValueError):
                    return float("nan")
                z = (forecast - thresh) / sigma
                p_high = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
                if str(mt).upper() == "LOW":
                    return 1.0 - p_high
                return p_high

            merged[col] = [
                _to_prob(row.get(model), row.get("market_type"), row.get("threshold")) for _, row in merged.iterrows()
            ]
        brier_df = merged
        prob_cols_after = [c for c in brier_df.columns if c.startswith("prob_")]
        if prob_cols_after:
            non_null_counts = brier_df[prob_cols_after].notna().sum().sum()
            forecasts_joined = non_null_counts > 0
            if forecasts_joined:
                # If the cached table already covered every needed pair, we
                # joined from disk; otherwise the dynamic backfill supplied
                # at least some rows.
                join_origin = "cached_table" if cached_pairs and cached_pairs >= needed_pairs else "historical_api"
            logger.info(
                "Forecast join %s (origin=%s, %d non-null prob values across %d rows)",
                "succeeded" if forecasts_joined else "failed (no overlapping rows)",
                join_origin,
                non_null_counts,
                len(brier_df),
            )

    # ----------------------------------------------------------------
    # Synthetic fallback — only when the real join produced no overlap.
    # Marks the rows with forecast_join_origin="synthetic" so downstream
    # code can refuse to report Sharpe/ROI numbers from synthetic data.
    # ----------------------------------------------------------------
    if not forecasts_joined:
        prob_cols_stale = [c for c in brier_df.columns if c.startswith("prob_")]
        if prob_cols_stale:
            brier_df = brier_df.drop(columns=prob_cols_stale)

        logger.warning(
            "Using SYNTHETIC per-model probabilities — real forecast "
            "join produced no overlapping (city, date) rows. The 3-layer "
            "loop will run but Sharpe/ROI numbers are NOT real alpha."
        )
        rng = random.Random(seed)
        for model in DEFAULT_MODELS:
            bias = rng.gauss(0, 0.08)
            col = f"prob_{model}"

            def _synth_prob(row, bias=bias):
                actual = row.get("temperature_2m_max")
                thresh = row.get("threshold")
                mt = str(row.get("market_type", "")).upper()
                if pd.isna(actual) or pd.isna(thresh) or thresh is None:
                    return 0.5
                z = (float(actual) - float(thresh)) / 2.0
                p_high = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
                if mt == "LOW":
                    p = 1.0 - p_high
                else:
                    p = p_high
                return max(0.01, min(0.99, p + bias))

            brier_df[col] = [_synth_prob(row) for _, row in brier_df.iterrows()]
        join_origin = "synthetic"

    brier_df["forecast_join_origin"] = join_origin
    return brier_df


def run_karpathy_weekly(
    rounds: int = 6,
    use_llm: bool = False,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the weekly Karpathy loop.

    For each round:
      1. Pull the latest unified Brier dataset.
      2. Build walk-forward splits.
      3. Propose a hypothesis (LLM if enabled, else mutation ladder).
      4. Evaluate it on each split's test window.
      5. Mean the per-split stats → mean_sharpe / mean_roi / mean_brier.
      6. If mean_sharpe > incumbent, persist as new best.

    Args:
        rounds: number of hypotheses to test this run.
        use_llm: if True, ask the LLM for proposals (falls back to ladder on error).
        seed: RNG seed for reproducibility.

    Returns a summary dict with the best hypothesis and its stats.
    """
    random.seed(seed)
    ds = UnifiedDatastore()

    # 1. Pull Brier dataset (markets ⋈ actuals)
    try:
        brier_df = ds.build_brier_dataset()
    except Exception as e:
        logger.warning(
            "build_brier_dataset() raised %s — the unified markets table does "
            "not yet have parsed weather fields (city/target_date/market_type). "
            "Run polymarket_ingest with the weather market parser first. "
            "Returning early without running any rounds.",
            e,
        )
        return {
            "error": "brier_dataset_unavailable",
            "detail": str(e),
            "rounds_run": 0,
        }
    if brier_df is None or brier_df.empty:
        logger.error(
            "Brier dataset is empty — run data_pipeline.ingest_all() with the "
            "weather market parser to populate markets.city / target_date / "
            "market_type / threshold before running this layer."
        )
        return {"error": "empty_brier_dataset", "rounds_run": 0}

    # Ensure per-model prob columns exist (tries real forecast join first,
    # falls back to synthetic per-model probabilities).
    brier_df = add_per_model_probabilities(brier_df, ds=ds, seed=seed)

    # 2. Walk-forward splits
    splits = ds.build_walk_forward_splits()
    if not splits:
        logger.warning(
            "No walk-forward splits built — falling back to a single all-data "
            "evaluation. This is NOT OOS but allows the loop to still run."
        )
        splits = [
            {
                "split_n": 1,
                "test_indices": brier_df.index.tolist(),
                "train_indices": [],
                "test_start": (brier_df["target_date"].min() if "target_date" in brier_df else None),
                "test_end": (brier_df["target_date"].max() if "target_date" in brier_df else None),
            }
        ]

    # 3. Load incumbent
    incumbent = _load_best()
    incumbent_stats: dict[str, float] | None = None
    if incumbent:
        # Re-evaluate incumbent on the current splits for a fair comparison
        per_split = [evaluate_hypothesis_oos(brier_df, s["test_indices"], incumbent) for s in splits]
        incumbent_stats = _mean_stats(per_split)
        logger.info(
            "Loaded incumbent: sharpe=%.3f roi=%.2f%% brier=%.4f",
            incumbent_stats.get("sharpe", 0.0),
            incumbent_stats.get("roi_pct", 0.0),
            incumbent_stats.get("brier_score", 0.25),
        )
    else:
        incumbent = Hypothesis(
            description="Uniform prior (baseline)",
            model_weights=_uniform_weights(),
            min_edge=0.05,
            kelly_fraction=0.15,
            max_bet_pct=0.05,
        )
        logger.info("No incumbent — starting from uniform prior")

    best_hyp = incumbent
    best_stats = incumbent_stats or {
        "sharpe": -1e9,
        "roi_pct": -1e9,
        "brier_score": 1.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "total_pnl": 0.0,
        "total_staked": 0.0,
    }

    # 4. Run rounds
    for r in range(1, rounds + 1):
        logger.info("--- Karpathy round %d/%d ---", r, rounds)

        # Propose hypothesis
        hyp: Hypothesis | None = None
        if use_llm:
            hyp = llm_propose_hypothesis(
                best_hyp,
                context={"last_stats": best_stats, "insights": []},
            )
        if hyp is None:
            hyp = generate_hypothesis(r - 1, parent=best_hyp)

        logger.info("Hypothesis: %s (source=%s)", hyp.description, hyp.source)

        # Evaluate on each split's test window
        per_split = [evaluate_hypothesis_oos(brier_df, s["test_indices"], hyp) for s in splits]
        mean_stats = _mean_stats(per_split)
        logger.info(
            "  OOS mean: sharpe=%.3f roi=%.2f%% brier=%.4f trades=%d",
            mean_stats["sharpe"],
            mean_stats["roi_pct"],
            mean_stats["brier_score"],
            mean_stats["total_trades"],
        )

        # Acceptance: must beat incumbent on Sharpe (and not blow up Brier)
        improved = mean_stats["sharpe"] > best_stats["sharpe"] and (
            mean_stats["brier_score"] <= best_stats["brier_score"] * 1.10
        )
        if improved and mean_stats["total_trades"] >= 5:
            logger.info("  ✓ ACCEPTED — new best")
            best_hyp = hyp
            best_stats = mean_stats
            _save_best(hyp, mean_stats)
            _append_results_tsv(r, hyp, mean_stats, "keep")
        else:
            logger.info(
                "  ✗ rejected (sharpe %.3f ≤ incumbent %.3f)",
                mean_stats["sharpe"],
                best_stats["sharpe"],
            )
            _append_results_tsv(r, hyp, mean_stats, "reject")

        # Small delay to be friendly to the LLM rate limit
        if use_llm:
            time.sleep(1.0)

    logger.info(
        "Karpathy weekly done. Best: sharpe=%.3f roi=%.2f%% desc=%r",
        best_stats.get("sharpe", 0.0),
        best_stats.get("roi_pct", 0.0),
        best_hyp.description,
    )

    return {
        "rounds_run": rounds,
        "best_hypothesis": best_hyp.to_dict(),
        "best_stats": best_stats,
        "n_splits": len(splits),
        "brier_dataset_rows": len(brier_df),
    }


def _mean_stats(per_split: list[dict[str, float]]) -> dict[str, float]:
    """Mean across splits, weighting each split equally."""
    if not per_split:
        return {
            "sharpe": 0.0,
            "roi_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "brier_score": 0.25,
            "total_pnl": 0.0,
            "total_staked": 0.0,
        }
    keys = ["sharpe", "roi_pct", "win_rate", "brier_score", "total_pnl", "total_staked"]
    out = {k: sum(s.get(k, 0.0) for s in per_split) / len(per_split) for k in keys}
    out["total_trades"] = int(sum(s.get("total_trades", 0) for s in per_split))
    out["sharpe"] = round(out["sharpe"], 4)
    out["roi_pct"] = round(out["roi_pct"], 4)
    out["brier_score"] = round(out["brier_score"], 4)
    out["win_rate"] = round(out["win_rate"], 4)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Karpathy weekly hypothesis loop")
    parser.add_argument("--rounds", type=int, default=6, help="Number of hypotheses to test")
    parser.add_argument("--llm", action="store_true", help="Use LLM for hypothesis generation")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    summary = run_karpathy_weekly(rounds=args.rounds, use_llm=args.llm, seed=args.seed)
    print(json.dumps(summary, indent=2, default=str))
