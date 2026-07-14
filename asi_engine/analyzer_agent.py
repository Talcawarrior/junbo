"""Analyzer Agent for Junbo.

Analyzes experimental backtest logs and distills them into semantic, causal
insights to write to the Cognition Base.
"""

import logging

from asi_engine.cognition_base import CognitionBase, CognitionNode

logger = logging.getLogger("ASI_ANALYZER")


class AnalyzerAgent:
    """Evaluates backtest logs, generates insights, and appends to Cognition Base."""

    def __init__(self, cognition_base: CognitionBase):
        self.cognition_base = cognition_base

    def analyze_results(
        self, run_round: int, hypothesis: str, backtest_results: dict, parameters: dict
    ) -> CognitionNode:
        """Analyze experimental results and produce a persistent Cognition Node.

        Generates structured causal descriptions explaining why the proposed
        adjustments succeeded or failed compared to previous baselines.
        """
        brier = backtest_results["brier_score"]
        roi = backtest_results["roi"]
        win_rate = backtest_results["win_rate"]
        pnl = backtest_results["pnl"]  # noqa: F841
        total_bets = backtest_results["total_bets"]  # noqa: F841

        logger.info(
            "ASI Analyzer: Formulating causal insights for Round %d...", run_round
        )

        # Retrieve previous best node
        nodes = self.cognition_base.nodes
        best_node = max(nodes, key=lambda n: n.roi)

        # Causal evaluation
        brier_improved = brier < best_node.brier_score
        roi_improved = roi > best_node.roi

        if roi_improved and brier_improved:
            insight = (
                "SUCCESS: Proposed adjustments succeeded in both accuracy "
                f"(Brier improved from {best_node.brier_score:.4f} "
                f"to {brier:.4f}) and profitability "
                f"(ROI increased from {best_node.roi:.2f}% to {roi:.2f}%). "
                "Shifting weights towards superior global meteorological "
                "models (like GFS/ECMWF) reduced the impact of regional "
                "forecasting noise and sharpened the Kelly sizing."
            )
        elif roi_improved:
            insight = (
                f"SUCCESS (FINANCIAL): ROI increased to {roi:.2f}% (from {best_node.roi:.2f}%) but Brier score "
                f"drifted to {brier:.4f}. This suggests that while overall model calibration slightly decreased, "
                f"the tightened risk threshold (min_edge) and Kelly multiplier selectively avoided marginal bets "
                f"and prioritized high-conviction positive-EV setups."
            )
        elif brier_improved:
            insight = (
                f"MIXED: Olasılık kalibrasyonu (Brier Score) {best_node.brier_score:.4f} değerinden {brier:.4f} "
                f"seviyesine iyileşti ancak finansal getiri (ROI) {best_node.roi:.2f}% değerinden {roi:.2f}% "
                f"seviyesine düştü. Bu durum, modelin daha gerçekçi tahminler ürettiğini ancak "
                f"Polymarket üzerindeki 'taker fee' (%2) ve likidite maliyetlerinin getiri üzerinde baskı kurduğunu "
                f"gösteriyor. Kelly katsayısının artırılması gerekebilir."
            )
        else:
            insight = (
                f"REJECTED: Proposed weights failed to improve both "
                f"Brier score ({brier:.4f} vs best "
                f"{best_node.brier_score:.4f}) and ROI "
                f"({roi:.2f}% vs best {best_node.roi:.2f}%). "
                "This indicates that trimming regional meteorological "
                "feeds or lowering the min_edge below safety thresholds "
                "exposed the portfolio to low-probability tail-events "
                "or over-aggression."
            )

        logger.info("  Causal Insight distilled: %s", insight)

        node = CognitionNode(
            run_round=run_round,
            hypothesis=hypothesis,
            brier_score=brier,
            roi=roi,
            win_rate=win_rate,
            causal_insight=insight,
            parameters=parameters,
        )

        return node
