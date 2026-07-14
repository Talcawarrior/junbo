"""Shared probability calculations for all market types.

Provides a single ``estimate_probability`` function that handles HIGH, LOW,
and RANGE market types, and a ``normal_cdf`` implementation used by all
callers in the codebase.

Usage::

    from utils.probability import estimate_probability, normal_cdf
"""

import logging
import math
from datetime import datetime, timezone

logger = logging.getLogger("PROBABILITY")

HAS_SCIPY: bool
try:
    from scipy.stats import norm as _scipy_norm  # type: ignore[import-untyped]

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    logger.debug("scipy not available, using Abramowitz & Stegun approximation")


# ── Normal CDF ────────────────────────────────────────────────────────────────


def normal_cdf(z: float) -> float:
    """Standard Normal CDF.

    Uses scipy when available; otherwise falls back to the
    Abramowitz & Stegun approximation (accurate to ~1e-7).
    """
    if HAS_SCIPY:
        return float(_scipy_norm.cdf(z))

    if z < -8:
        return 0.0
    if z > 8:
        return 1.0

    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429
    p = 0.2316419

    if z >= 0:
        t = 1.0 / (1.0 + p * z)
        poly = t * (b1 + t * (b2 + t * (b3 + t * (b4 + t * b5))))
        return 1.0 - (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-z * z / 2) * poly

    t = 1.0 / (1.0 - p * z)
    poly = t * (b1 + t * (b2 + t * (b3 + t * (b4 + t * b5))))
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-z * z / 2) * poly


# ── Market-type-aware probability ─────────────────────────────────────────────


def estimate_probability(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    mean: float,
    std: float,
    threshold: float,
    days_ahead: int = 0,
    market_type: str = "HIGH",
    range_low: float | None = None,
    range_high: float | None = None,
) -> float:
    """Estimate probability of the YES outcome for a binary weather market.

    Parameters
    ----------
    mean : float
        Mean of forecasted temperatures.
    std : float
        Standard deviation of forecasted temperatures (inter-model spread).
    threshold : float
        The strike temperature from the market question (e.g. 30°C).
    days_ahead : int
        Days until market resolution.  Adds 0.5°C uncertainty per day
        (minimum total_std=1.0).
    market_type : str
        ``"HIGH"``, ``"LOW"``, or ``"RANGE"``.

        * **HIGH** — YES = *at or above* threshold:
          ``P(T >= X) = 1 - CDF((X - mean)/σ)``

        * **LOW** — YES = *at or below* threshold:
          ``P(T <= X) = CDF((X - mean)/σ)``

        * **RANGE** — YES = *within a bucket* centred on threshold:
          ``P(X-0.5 ≤ T < X+0.5) = CDF(z_upper) - CDF(z_lower)``.
          Explicit *range_low*/*range_high* override the ±0.5 bucket.

    Returns
    -------
    float
        Probability clamped to ``[0.01, 0.99]``.
    """
    total_std = math.sqrt(std**2 + (days_ahead * 0.5) ** 2)
    total_std = max(total_std, 1.0)

    mt = market_type.upper()
    z = (threshold - mean) / total_std

    if mt == "HIGH":
        prob = 1.0 - normal_cdf(z)
    elif mt == "LOW":
        prob = normal_cdf(z)
    elif mt == "RANGE":
        low = (
            range_low
            if (range_low is not None and range_high is not None)
            else threshold - 0.5
        )
        high = (
            range_high
            if (range_low is not None and range_high is not None)
            else threshold + 0.5
        )
        prob = normal_cdf((high - mean) / total_std) - normal_cdf(
            (low - mean) / total_std
        )
    else:
        logger.warning("Unknown market_type=%r, falling back to HIGH", market_type)
        prob = 1.0 - normal_cdf(z)

    return max(0.01, min(0.99, prob))


# ── Time-to-close edge escalation ─────────────────────────────────────────────


def compute_effective_min_edge(market, std: float | None = None) -> float:
    """Time-to-close-scaled min_edge for a market.

    Linearly ramps from 1x bot_config.strategy.min_edge at
    edge_escalation_hours before resolution to
    edge_escalation_multiplier * min_edge at the moment of close.
    Clamps to the multiplier if we are already past resolution, and
    never divides by zero.

    If *std* is provided and > 2.5, the base min_edge is doubled
    (high-uncertainty guard for RANGE / high-spread markets).
    """
    from config.settings import bot_config

    s = bot_config.strategy

    # High-uncertainty guard: if inter-model spread > 2.5C, double
    # the min_edge requirement.
    if std is not None and std > 2.5:
        base = s.min_edge * 2.0
        logger.info(
            "High uncertainty guard: std=%.2f > 2.5, doubling min_edge to %.4f",
            std,
            base,
        )
    else:
        base = s.min_edge

    try:
        resolution = getattr(market, "resolution_date", None) or getattr(
            market, "target_date", None
        )
        if resolution is None:
            return base
        now = datetime.now(timezone.utc)
        if getattr(resolution, "tzinfo", None) is None:
            resolution = resolution.replace(tzinfo=timezone.utc)
        hours_left = (resolution - now).total_seconds() / 3600.0
    except Exception:
        return base

    # 60s tolerance for the boundary: a market created with
    # resolution_date=now+esc_h drifts microseconds by the time the
    # function runs, producing 0.01+1e-9 on CI. A 1-minute window
    # makes the boundary deterministic.
    if hours_left >= s.edge_escalation_hours - (60.0 / 3600.0):
        return base
    if hours_left <= 0:
        return base * s.edge_escalation_multiplier
    esc_h = max(1, s.edge_escalation_hours)
    fraction = hours_left / esc_h
    return base * (1.0 + (s.edge_escalation_multiplier - 1.0) * (1.0 - fraction))
