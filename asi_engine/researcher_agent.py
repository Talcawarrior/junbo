"""Researcher Agent for Junbo.

Responsible for generating new strategy hypotheses and parameter proposals
by reviewing historical successes and failures stored in the Cognition Base.

The previous version of this file hardcoded `gfs_seamless` as the model to
boost and `meteofrance_seamless` as the model to trim. That defeated the
entire point of an "evolutionary" loop — the result was deterministic and
called itself "self-evolving" while doing no such thing.

This version reads the per-model Brier scores from the `model_performance`
DB table (populated by `SIALoop.analyze_model_performance`) and shifts
weight from high-Brier (worse) to low-Brier (better) models. If no DB
data is available, it falls back to a *random* pair of models — never a
hardcoded pair.
"""

import logging
import random

from asi_engine.cognition_base import CognitionBase

logger = logging.getLogger("ASI_RESEARCHER")


def _load_model_brier_scores() -> dict[str, float]:
    """Read per-model Brier scores from the `model_performance` table.

    Returns an empty dict if the table is empty or the DB is unavailable.
    Lower Brier = better model.
    """
    try:
        from database.db import get_session
        from database.models import ModelPerformance
    except Exception:
        return {}

    scores: dict[str, float] = {}
    try:
        with get_session() as session:
            rows = session.query(ModelPerformance).all()
            for row in rows:
                if row.model_name and row.num_predictions and row.num_predictions >= 10:
                    scores[row.model_name] = float(row.brier_score or 0.5)
    except Exception as e:
        logger.debug("Could not load model_performance: %s", e)
    return scores


class ResearcherAgent:
    """Generates evolutionary hypotheses and model weight configurations."""

    def __init__(self, cognition_base: CognitionBase):
        self.cognition_base = cognition_base

    def propose_hypothesis(self, run_round: int) -> tuple[str, dict]:
        """Propose a new strategy hypothesis and parameter set.

        Builds the mutation from the per-model Brier scores stored in the
        `model_performance` table (populated by SIALoop). If those are not
        available, falls back to a uniformly random pair of models.
        """
        nodes = self.cognition_base.nodes
        best_node = max(nodes, key=lambda n: n.roi)

        logger.info("ASI Researcher: Reviewing previous round insights...")
        logger.info(
            "  Best historical node (Round %d): ROI=%.2f%%, Brier=%.4f",
            best_node.round,
            best_node.roi,
            best_node.brier_score,
        )

        best_params = best_node.parameters
        base_weights = best_params["model_weights"].copy()
        models = list(base_weights.keys())

        # ── Brier-driven boost/trim selection ──────────────────────────────
        # The previous implementation hardcoded GFS as boost and MeteoFrance
        # as trim, which is not "evolution" — it's a fixed prior dressed up
        # as learning. We now read real per-model Brier scores from the DB
        # (when available) and shift weight from the worst to the best.
        brier_scores = _load_model_brier_scores()
        mutation_amount = random.uniform(0.02, 0.05)

        # Filter Brier scores to models that exist in the current weight map.
        known_briers = {m: brier_scores[m] for m in models if m in brier_scores}

        if len(known_briers) >= 2:
            # Real data path: boost the lowest-Brier model, trim the highest.
            boost_model = min(known_briers, key=known_briers.get)
            trim_model = max(known_briers, key=known_briers.get)
            rationale = (
                f"Brier-driven shift: '{boost_model}' (Brier="
                f"{known_briers[boost_model]:.4f}) is the most accurate model; "
                f"'{trim_model}' (Brier={known_briers[trim_model]:.4f}) is the least."
            )
        else:
            # Fallback: random pair — never a hardcoded "GFS good / MF bad" pair.
            if len(models) < 2:
                # Degenerate case: nothing to evolve.
                return (
                    f"Evolved Candidate (Round {run_round}): No mutation (insufficient models).",
                    best_params,
                )
            boost_model, trim_model = random.sample(models, 2)
            rationale = (
                "Random exploration (no Brier data available yet): picked "
                f"'{boost_model}' to boost and '{trim_model}' to trim."
            )

        if boost_model == trim_model:
            trim_model = random.choice([m for m in models if m != boost_model])

        adjusted_weights = {}
        for model, weight in base_weights.items():
            if model == boost_model:
                adjusted_weights[model] = max(0.01, min(0.50, weight + mutation_amount))
            elif model == trim_model:
                adjusted_weights[model] = max(0.01, min(0.50, weight - mutation_amount))
            else:
                adjusted_weights[model] = weight

        # Normalize weights so they sum to exactly 1.0
        total_w = sum(adjusted_weights.values())
        for model in adjusted_weights:
            adjusted_weights[model] = round(adjusted_weights[model] / total_w, 4)

        # Mutate strategy parameters (min_edge, kelly_fraction)
        best_min_edge = best_params.get("min_edge", 0.05)
        best_kelly = best_params.get("kelly_fraction", 0.15)

        # Ramped change
        new_min_edge = round(
            max(0.02, min(0.15, best_min_edge + random.choice([-0.01, 0.0, 0.01]))), 3
        )
        new_kelly = round(
            max(0.05, min(0.25, best_kelly + random.choice([-0.02, 0.0, 0.02]))), 3
        )

        new_params = {
            "model_weights": adjusted_weights,
            "min_edge": new_min_edge,
            "kelly_fraction": new_kelly,
        }

        # Formulate semantic hypothesis
        hypothesis = (
            f"Evolved Candidate (Round {run_round}): {rationale} "
            f"Shift {mutation_amount * 100:.1f}% weight. "
            f"Tune min_edge to {new_min_edge * 100:.1f}% and Kelly fraction to {new_kelly * 100:.1f}%."
        )

        logger.info("ASI Researcher: Proposed Hypothesis -> %s", hypothesis)
        return hypothesis, new_params
