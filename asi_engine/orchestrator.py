"""Orchestrator for Junbo.

Triggers the complete closed-loop self-improving cycle:
Learn -> Design -> Experiment -> Analyze -> Deploy.
"""

import logging

from asi_engine.analyzer_agent import AnalyzerAgent
from asi_engine.backtest_simulator import BacktestSimulator
from asi_engine.cognition_base import CognitionBase
from asi_engine.researcher_agent import ResearcherAgent
from config.settings import bot_config, config
from utils.weights_store import save_strategy_params, save_weights

logger = logging.getLogger("ASI_ORCHESTRATOR")


class JunboOrchestrator:
    """The central manager running the self-evolving ASI-Evolve framework."""

    def __init__(self):
        self.cognition_base = CognitionBase()
        self.researcher = ResearcherAgent(self.cognition_base)
        self.simulator = BacktestSimulator()
        self.analyzer = AnalyzerAgent(self.cognition_base)

    def run_evolution_pipeline(self, rounds: int = 5) -> dict:
        """Run the complete evolutionary research loop for N rounds.

        Each round generates a new model weight/risk parameters proposal,
        tests it over historical DB trades, distills causal insights, and
        persists the learning.
        """
        logger.info("==================================================")
        logger.info("   Junbo: STARTING AUTONOMOUS EVOLUTION LOOP    ")
        logger.info("==================================================")

        current_round_start = len(self.cognition_base.nodes)

        for r in range(1, rounds + 1):
            run_round = current_round_start + r
            logger.info("\n--- EVOLUTION ROUND %d ---", run_round)

            # 1. DESIGN: Propose a new hypothesis and parameter set
            hypothesis, proposed_params = self.researcher.propose_hypothesis(run_round)

            # 2. EXPERIMENT: Run the proposed parameters through backtest simulator
            results = self.simulator.run_backtest(proposed_params)

            # 3. ANALYZE & LEARN: Formulate causal insights and update memory
            node = self.analyzer.analyze_results(
                run_round, hypothesis, results, proposed_params
            )
            self.cognition_base.add_node(node)

        # 4. DEPLOY: Find the best parameters in all history and write to active config files
        best_params = self.cognition_base.get_best_parameters()

        logger.info("\n==================================================")
        logger.info("   Junbo: DEPLOYING BEST EVOLVED STRATEGY      ")
        logger.info("==================================================")

        best_weights = best_params["model_weights"]
        best_min_edge = best_params["min_edge"]
        best_kelly = best_params["kelly_fraction"]

        logger.info("Evolved Model Weights deployed:")
        for m, w in best_weights.items():
            logger.info("  %s: %.2f%%", m, w * 100)

        logger.info("Evolved Risk Strategy parameters deployed:")
        logger.info("  min_edge: %.4f", best_min_edge)
        logger.info("  kelly_fraction: %.4f", best_kelly)

        # Update disk storage so next process restart loads them
        save_weights(best_weights)
        save_strategy_params({"min_edge": best_min_edge, "kelly_fraction": best_kelly})

        # Dynamically apply to in-memory active configs
        config.MODEL_WEIGHTS = best_weights
        bot_config.strategy.min_edge = best_min_edge
        bot_config.strategy.kelly_fraction = best_kelly

        logger.info(
            "Junbo: Live trading models and configurations updated successfully!"
        )

        # Return best stats
        best_node = max(self.cognition_base.nodes, key=lambda n: n.roi)
        return {
            "round": best_node.round,
            "roi": best_node.roi,
            "brier_score": best_node.brier_score,
            "parameters": best_params,
        }
