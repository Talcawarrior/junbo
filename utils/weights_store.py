"""JSON weight persistence for SIA.

The SIA loop optimizes MODEL_WEIGHTS in memory and on ModelPerformance rows,
but the live config object is process-local. Without a durable copy, every
restart throws away the learned weights and goes back to the static defaults
in config/settings.py. This module adds a single-file load/save on top.

Design notes
------------
* The file is read at SIALoop.__init__ time, so any process that imports
  engine.strategy and instantiates SIALoop gets the latest learned weights
  before it computes its first probability.
* The file is written on every optimize_weights() call when the maximum
  absolute change vs. the previous persisted weights is >= 0.001 (0.1
  percentage points). Smaller updates are logged but not written so we
  do not spam the disk on cosmetic drift.
* Errors are never raised -- a missing file, a corrupted file, or a
  read-only filesystem all fall back to the in-memory defaults. The bot
  is paper-mode, but it should still survive accidental edits of the
  JSON file.
* **Diversification floor (QA-11)**: every save applies a per-model
  minimum weight (default 5%) and renormalizes. This prevents the
  pathological case where a backtest on a narrow date range (3 days)
  converges on a 2-model dominant solution (47% / 47% / 1% x 6) that
  does not generalize. The floor is enforced centrally here so all
  three writers (SIA loop, LLM loop deploy, karpathy_search) inherit
  it automatically without each having to remember to call _apply_floor.
"""

from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

# Project root: two parents up from utils/ -> repo root.
_WEIGHTS_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "data", "model_weights.json")
)
_STRATEGY_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "data", "strategy_params.json")
)

# Diversification floor — no model may drop below this weight after save.
# Set to 5% so the ensemble cannot collapse to a 1-2 model solution even
# when a narrow backtest window suggests that would maximize Sharpe.
# Rationale: a 3-day backtest optimum (overfit) is rarely the future
# optimum, so we enforce minimal participation from all 8 ensemble models.
MIN_MODEL_WEIGHT = 0.05

# All 8 ensemble models — if any are missing from the saved weights,
# they are re-added at the floor weight so the ensemble never collapses
# to a 2-model solution after karpathy_search or optimize_weights.
ALL_ENSEMBLE_MODELS = [
    "gfs_seamless",
    "ecmwf_ifs025",
    "gem_global",
    "icon_global",
    "jma_seamless",
    "cma_grapes_global",
    "ukmo_seamless",
    "meteofrance_seamless",
]

_lock = threading.Lock()


def load_strategy_params() -> dict[str, float] | None:
    """Read strategy parameters from disk."""
    try:
        with open(_STRATEGY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_strategy_params(params: dict[str, float]):
    """Save strategy parameters to disk."""
    with _lock:
        try:
            os.makedirs(os.path.dirname(_STRATEGY_PATH), exist_ok=True)
            with open(_STRATEGY_PATH, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=2)
            logger.info("Strategy parameters persisted to %s", _STRATEGY_PATH)
        except Exception as e:
            logger.warning("Could not save strategy parameters: %s", e)


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    """Return weights as plain floats, dropping unknown keys.

    A new model appearing in code but missing from the persisted file
    will still be picked up from the in-memory defaults because the
    caller (SIALoop.__init__) merges this dict over `self.model_weights`.
    """
    out: dict[str, float] = {}
    for k, v in weights.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _apply_floor(
    weights: dict[str, float],
    floor: float = MIN_MODEL_WEIGHT,
) -> dict[str, float]:
    """Apply a per-model minimum weight floor via water-filling.

    This is the single source of truth for the diversification guarantee.
    Every writer (SIA loop, LLM loop deploy, karpathy_search) calls
    save_weights(), which calls this internally. No caller needs to
    remember to apply the floor — it just happens.

    Algorithm (water-filling):
      1. Identify all models below `floor`.
      2. Pin them to `floor` (consuming `floor * k` of the 1.0 budget).
      3. Redistribute the remaining `(1 - floor * k)` budget among the
         above-floor models, preserving their relative ratios.
      4. Repeat until no model is below floor (a single redistribution
         can push another model below floor — we iterate until stable).

    Edge case: if `floor * n >= 1.0` (e.g. floor=0.20 with 8 models =
    1.6), the floor is not enforceable. In that case we silently fall
    back to uniform weights (1/N each) and log a warning.

    Why water-filling instead of `max(w, floor) / sum(...)`?
      The naive approach can violate the floor again after renormalize.
      Example with floor=0.05 and 8 models where 6 are at 0.01 and 2
      are at 0.47: naive gives 0.05 to the 6 small ones, then renormalize
      divides by 1.30, sending the small ones back to 0.038 — below floor.
      Water-filling avoids this by reserving the floor budget first.
    """
    if not weights:
        return {}
    n = len(weights)
    if floor * n >= 1.0:
        logger.warning(
            "MIN_MODEL_WEIGHT=%.4f * n=%d >= 1.0 — floor not enforceable, "
            "falling back to uniform 1/%d",
            floor,
            n,
            n,
        )
        uniform = round(1.0 / n, 4)
        return {k: uniform for k in weights}

    items = {k: float(v) for k, v in weights.items()}

    # Iterate water-filling until stable (typically converges in 1-2 rounds).
    for _ in range(n + 1):  # safety bound — converges in <= n iterations
        # Pinned = models at or below floor (they consume `floor` each
        # of the 1.0 budget, whether they were originally below or at).
        pinned = [k for k, v in items.items() if v <= floor]
        free = [k for k, v in items.items() if v > floor]

        if not pinned:
            break  # all above floor — done

        k_count = len(pinned)
        remaining_budget = 1.0 - floor * k_count
        if remaining_budget <= 0:
            # floor * k_count == 1.0 — all pinned at floor exactly.
            uniform = round(1.0 / n, 4)
            return {k: uniform for k in weights}

        # Pin all pinned models at floor (idempotent for those already there)
        for k in pinned:
            items[k] = floor

        # Redistribute remaining_budget among free models
        # proportionally to their current values.
        free_vals = {k: items[k] for k in free}
        total_free = sum(free_vals.values())
        if total_free <= 0 or not free:
            # No free models left — uniform fallback
            uniform = round(1.0 / n, 4)
            return {k: uniform for k in weights}
        for k in free:
            items[k] = (free_vals[k] / total_free) * remaining_budget
        # Loop again — proportional redistribution may have pushed
        # another model below floor.

    # Final renormalize for floating-point drift + rounding
    total = sum(items.values())
    if total <= 0:
        uniform = round(1.0 / n, 4)
        return {k: uniform for k in weights}
    return {k: round(v / total, 4) for k, v in items.items()}


def load_weights(path: str | None = None) -> dict[str, float] | None:
    """Read model weights from disk.

    Returns the dict on success, or None if the file is missing, empty,
    unparseable, or the data directory is unreadable. Callers should
    fall back to the in-memory defaults on None.
    """
    p = path or _WEIGHTS_PATH
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load model weights from %s: %s", p, exc)
        return None
    if not isinstance(raw, dict):
        return None
    norm = _normalize(raw)
    return norm or None


def save_weights(
    weights: dict[str, float],
    path: str | None = None,
    *,
    min_change: float = 0.001,
    apply_floor: bool = True,
) -> bool:
    """Persist model weights if they changed enough to matter.

    Returns True if a write happened, False if the change was too small
    or the write failed (in which case a warning is logged but no
    exception is raised -- the in-memory update still took effect).

    Parameters
    ----------
    weights : dict[str, float]
        Raw weights from the caller. May be unnormalized.
    path : str, optional
        Override path (used by tests).
    min_change : float
        Skip the write if the maximum per-model delta vs the previously
        persisted file is below this threshold. Default 0.001 (0.1pp).
    apply_floor : bool
        If True (default), apply MIN_MODEL_WEIGHT floor + renormalize
        before persisting. Set to False only for tests that want to
        verify raw optimizer output.
    """
    p = path or _WEIGHTS_PATH
    norm = _normalize(weights)
    if not norm:
        return False

    # Re-add any ensemble models that are missing (e.g. karpathy_search
    # wrote only 2 models). Give them the floor weight so they survive
    # the normalization step below.
    for m in ALL_ENSEMBLE_MODELS:
        if m not in norm:
            norm[m] = MIN_MODEL_WEIGHT

    if apply_floor:
        norm = _apply_floor(norm)

    with _lock:
        prev = load_weights(p)
        if prev is not None:
            # Compare union of keys so a newly-tracked model still triggers
            # a write on its first appearance.
            keys = set(prev) | set(norm)
            max_delta = max(abs(norm.get(k, 0.0) - prev.get(k, 0.0)) for k in keys)
            if max_delta < min_change:
                logger.info(
                    "SIA weight change %.4f below threshold %.4f, skipping write",
                    max_delta,
                    min_change,
                )
                return False
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(norm, f, indent=2, sort_keys=True)
            os.replace(tmp, p)
            logger.info("SIA weights persisted to %s", p)
            return True
        except OSError as exc:
            logger.warning("Could not save model weights to %s: %s", p, exc)
            return False
