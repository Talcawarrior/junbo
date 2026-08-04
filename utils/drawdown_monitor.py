"""Drawdown tracking with high-water-mark and progressive risk tiers.

A long-running bot must de-risk automatically as the bankroll falls from its
peak. This module is a pure, injectable state machine (no DB, no clock
dependency) so it can be unit-tested deterministically and reused by the live
trader, the backtester, and the evolution simulator.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskTier:
    name: str
    alpha_multiplier: float
    halt: bool


GREEN = RiskTier("green", 1.0, False)
YELLOW = RiskTier("yellow", 0.5, False)
RED = RiskTier("red", 0.0, True)
CRITICAL = RiskTier("critical", 0.0, True)

COLD_STREAK_LEN = 5
COLD_STREAK_CONF = 0.70


class DrawdownMonitor:
    """Track drawdown from a high-water mark and expose risk tiers.

    Thresholds are drawdown fractions from the peak (0..1). Defaults:
        yellow >= 0.10, red >= 0.20, critical >= 0.30.
    """

    def __init__(
        self,
        yellow: float = 0.10,
        red: float = 0.20,
        critical: float = 0.30,
        peak: float = 0.0,
    ) -> None:
        self.yellow = yellow
        self.red = red
        self.critical = critical
        self._peak = float(peak) if peak > 0 else 0.0
        self._current = self._peak
        self._cold_streak = 0

    @property
    def peak(self) -> float:
        return self._peak

    @property
    def current_equity(self) -> float:
        return self._current

    def drawdown(self) -> float:
        if self._peak <= 0:
            return 0.0
        return max(0.0, (self._peak - self._current) / self._peak)

    def tier(self) -> RiskTier:
        dd = self.drawdown()
        if dd >= self.critical:
            return CRITICAL
        if dd >= self.red:
            return RED
        if dd >= self.yellow or self._cold_streak >= COLD_STREAK_LEN:
            return YELLOW
        return GREEN

    def halt(self) -> bool:
        return self.tier().halt

    def alpha_multiplier(self) -> float:
        return self.tier().alpha_multiplier

    def update(self, equity: float) -> RiskTier:
        """Feed the latest equity; advance the high-water mark; return tier."""
        self._current = float(equity)
        if self._peak <= 0 or equity > self._peak:
            self._peak = equity
        return self.tier()

    def record_outcome(self, confidence: float, won: bool) -> None:
        """Update the cold-streak counter.

        A *high-confidence miss* (conf >= COLD_STREAK_CONF and lost) increments
        the streak; any other outcome (win, or low-confidence miss) resets it.
        Low-confidence misses are expected noise, not model failure.
        """
        if confidence >= COLD_STREAK_CONF and not won:
            self._cold_streak += 1
        else:
            self._cold_streak = 0
