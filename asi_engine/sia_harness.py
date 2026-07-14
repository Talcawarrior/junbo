"""SIA harness — the function that turns per-model forecasts into a YES prob.

This file is the target of Layer 3 harness updates. The LLM may propose
patches to `predict_yes_probability`. Each patch must pass a syntax
check + smoke-eval before being accepted.
"""

from __future__ import annotations

import math


def predict_yes_probability(
    forecasts: dict[str, float],
    weights: dict[str, float],
    threshold: float,
    days_ahead: int = 1,
) -> float:
    """Compute P(YES) from per-model temperature forecasts.

    Default implementation: weighted mean of forecasts -> z-score vs
    threshold -> Normal CDF.

    Args:
        forecasts: dict of {model_name: forecasted_temperature}
        weights: dict of {model_name: weight} (must sum to 1.0)
        threshold: market threshold temperature
        days_ahead: forecast horizon (1-7)

    Returns:
        P(YES) in [0.01, 0.99]
    """
    if not forecasts:
        return 0.5

    wsum = sum(weights.get(m, 0.0) for m in forecasts)
    if wsum <= 0:
        return 0.5

    weighted_mean = sum(weights.get(m, 0.0) * f for m, f in forecasts.items()) / wsum
    variance = (
        sum(
            weights.get(m, 0.0) * (f - weighted_mean) ** 2 for m, f in forecasts.items()
        )
        / wsum
    )
    std = math.sqrt(variance) if variance > 0 else 1.0
    effective_std = std * math.sqrt(max(days_ahead, 1))

    z = (weighted_mean - threshold) / (effective_std + 1e-5)
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return min(max(p, 0.01), 0.99)
