"""Backtest Simulator for ASIbot.

Simulates the performance of proposed model weights and strategy parameters
over the historical bets and analyses saved in the SQLite database, as well as
deep backtests over the full backfilled meteorology calibration tables.
"""

import json
import logging
import sqlite3

from database.db import DB_PATH, get_session
from database.models import Analysis, Bet, WeatherMarket
from utils.formulas import polymarket_fee
from utils.kelly import kelly_bet_amount
from utils.probability import estimate_probability

# Weather category fee rate (Polymarket official: fee = C × feeRate × p × (1-p))
WEATHER_FEE_RATE = 0.05

logger = logging.getLogger("ASI_BACKTESTER")


class BacktestSimulator:
    """Evaluates strategy parameters over historical operations in SQLite."""

    def run_backtest(self, parameters: dict) -> dict:
        """Run a backtest using the proposed model weights and parameters.

        Recalculates the consensus probabilities, checks if bets would have
        been opened, sizes them with Kelly, and reports overall metrics.
        """
        model_weights = parameters["model_weights"]
        min_edge = parameters["min_edge"]
        kelly_fraction = parameters["kelly_fraction"]

        logger.info("ASI Backtester: Starting simulation over database records...")

        simulated_pnl = 0.0
        total_bets_opened = 0
        bets_won = 0
        bets_lost = 0
        total_wagered = 0.0
        brier_errors = []

        with get_session() as session:
            # Query all settled bets with their analysis & market details
            settled_records = (
                session.query(Bet, Analysis, WeatherMarket)
                .join(Analysis, Bet.analysis_id == Analysis.id, isouter=True)
                .join(WeatherMarket, Bet.market_id == WeatherMarket.id, isouter=True)
                .filter(Bet.status.in_(["won", "lost"]))
                .all()
            )

            if not settled_records:
                # Fall back to extended backtest if no manual bets in DB
                return self.run_extended_backtest(parameters)

            # Initialize a virtual bankroll starting at $10000
            bankroll = 10000.0

            for _bet, analysis, market in settled_records:
                if not analysis or not analysis.model_predictions:
                    continue

                try:
                    mp = json.loads(analysis.model_predictions)
                except Exception:
                    continue

                model_probs = mp.get("model_probs", {})
                if not model_probs:
                    continue

                # 1. Recalculate consensus probability using proposed model weights
                weight_sum = sum(model_weights.get(m, 0.0) for m in model_probs)
                if weight_sum <= 0:
                    continue

                recalculated_prob = (
                    sum(model_weights.get(m, 0.0) * float(prob) for m, prob in model_probs.items()) / weight_sum
                )

                # 2. Check if the market outcome matches the YES direction
                outcome_yes = self._resolve_outcome(market)
                if outcome_yes is None:
                    continue

                # Add to Brier Score calculation (YES probability vs actual outcome)
                brier_errors.append((recalculated_prob - (1.0 if outcome_yes else 0.0)) ** 2)

                # 3. Simulate bet eligibility and sizing
                yes_price = float(market.yes_price or 0.5)
                no_price = 1.0 - yes_price

                yes_edge = recalculated_prob - yes_price
                no_edge = (1.0 - recalculated_prob) - no_price

                # Determine side and edge
                if yes_edge > no_edge:
                    sim_side = "YES"
                    sim_edge = yes_edge
                    entry_price = yes_price
                else:
                    sim_side = "NO"
                    sim_edge = no_edge
                    entry_price = no_price

                # Check if edge exceeds proposed min_edge (plus 2% fee_drag)
                ev = sim_edge - 0.02
                if sim_edge >= min_edge and ev > 0:
                    # Yes, would place a bet!
                    total_bets_opened += 1

                    # Kelly size it
                    prob_win = recalculated_prob if sim_side == "YES" else (1.0 - recalculated_prob)
                    bet_size = kelly_bet_amount(
                        bankroll,
                        prob_win,
                        entry_price,
                        fraction=kelly_fraction,
                        min_bet=1.0,
                        max_bet_pct=0.03,
                    )

                    # Evaluate bet outcome
                    won = (sim_side == "YES" and outcome_yes) or (sim_side == "NO" and not outcome_yes)
                    total_wagered += bet_size

                    if won:
                        bets_won += 1
                        payout = bet_size / entry_price
                        # Polymarket taker fee: C × feeRate × p × (1-p)
                        shares = bet_size / entry_price
                        fee = polymarket_fee(shares, entry_price, WEATHER_FEE_RATE)
                        pnl = payout - bet_size - fee
                    else:
                        bets_lost += 1
                        pnl = -bet_size

                    simulated_pnl += pnl
                    bankroll += pnl
                    if bankroll <= 0:
                        logger.info("Bankroll depleted — ending simulation")
                        break

        # Compile metrics
        final_brier = sum(brier_errors) / len(brier_errors) if brier_errors else 0.25
        roi = (simulated_pnl / total_wagered * 100) if total_wagered > 0 else 0.0
        win_rate = (bets_won / total_bets_opened) if total_bets_opened > 0 else 0.0

        logger.info(
            "  Backtest Results -> Brier=%.4f, ROI=%.2f%%, Opened Bets=%d",
            final_brier,
            roi,
            total_bets_opened,
        )

        return {
            "brier_score": round(final_brier, 4),
            "roi": round(roi, 2),
            "win_rate": round(win_rate, 4),
            "total_bets": total_bets_opened,
            "pnl": round(simulated_pnl, 2),
        }

    def run_extended_backtest(self, parameters: dict) -> dict:
        """Runs a deep out-of-sample backtest over ALL backfilled records in `historical_calibrations` table.

        Goes back as far as there is data (e.g. past 90 or 365 days of weather).

        ── Honesty notes ───────────────────────────────────────────────────
        The previous version of this method had two structural biases that
        made the backtest results misleadingly positive:

          1. Strike price was set to `actual_value - 0.5`, which GUARANTEED
             `outcome_yes = True` for every scenario. The bot's YES bets
             always won and its NO bets always lost, so the apparent
             "edge" was just an artefact of the strike generator.

          2. The market price was a hardcoded 0.60, giving the bot a
             constant 0.40 inefficiency to exploit — completely unrealistic.

        This version:
          * Draws strike prices from a uniform grid covering the realistic
            temperature range, so roughly half the scenarios resolve YES
            and half resolve NO.
          * Builds the market price from a *naive* consensus of the same
            forecasts the bot uses, plus an independent inefficiency noise
            term (mean 0, std 7pp) so the bot can only profit by being
            smarter than the naive average — not by peeking at the answer.
        """
        import random as _random

        logger.info(
            "ASI Backtester: No manual bets found. Running deep backtest over 'historical_calibrations' dataset..."
        )

        model_weights = parameters["model_weights"]
        min_edge = parameters["min_edge"]
        kelly_fraction = parameters["kelly_fraction"]

        # Deterministic seed so the backtest is reproducible across runs.
        # The seed does NOT touch the ground truth or the market price
        # generation — only the noise added on top of an honest market.
        rng = _random.Random(42)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Query all distinct dates, cities, metrics, and actual values
        query = """
            SELECT city_code, city, date, metric, actual_value
            FROM historical_calibrations
            GROUP BY city_code, date, metric
            ORDER BY date ASC
        """
        try:
            cursor.execute(query)
            groups = cursor.fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return {
                "brier_score": 0.25,
                "roi": 0.0,
                "win_rate": 0.0,
                "total_bets": 0,
                "pnl": 0.0,
            }

        sim_pnl = 0.0
        total_trades = 0
        trades_won = 0
        trades_lost = 0
        total_wagered = 0.0
        brier_errors = []
        bankroll = 10000.0

        # Strike grid — covers a realistic temperature range in °C.
        # Using a fixed grid (instead of drawing from a distribution) keeps
        # the YES/NO ratio close to 50/50 over the long run.
        strike_grid = [round(0.5 * k, 1) for k in range(-20, 80)]  # -10.0 .. +39.5

        for city_code, _city_name, date_str, metric, actual_val in groups:
            # Query all model predictions for this specific group
            cursor.execute(
                """
                SELECT model, predicted_value
                FROM historical_calibrations
                WHERE city_code = ? AND date = ? AND metric = ?
            """,
                (city_code, date_str, metric),
            )
            preds = dict(cursor.fetchall())
            if not preds:
                continue

            # Calculate weighted average temperature prediction
            weight_sum = sum(model_weights.get(m, 0.0) for m in preds)
            if weight_sum <= 0:
                continue

            weighted_temp = sum(model_weights.get(m, 0.0) * val for m, val in preds.items()) / weight_sum

            # ── HONEST STRIKE PRICE ────────────────────────────────────────
            # Pick a strike from a wide grid. Half the scenarios will resolve
            # YES, half NO. The bot does not see the strike in advance.
            strike = rng.choice(strike_grid)
            outcome_yes = actual_val > strike

            # Calculate probability of YES using normal distribution estimate
            pred_vals = list(preds.values())
            mean = sum(pred_vals) / len(pred_vals)
            std = (
                max(
                    (sum((x - mean) ** 2 for x in pred_vals) / (len(pred_vals) - 1)) ** 0.5,
                    1.0,
                )
                if len(pred_vals) > 1
                else 1.5
            )

            prob = estimate_probability(
                mean=weighted_temp,
                std=std,
                threshold=strike,
                days_ahead=2,
                market_type="HIGH",
            )

            brier_errors.append((prob - (1.0 if outcome_yes else 0.0)) ** 2)

            # ── HONEST MARKET PRICE ────────────────────────────────────────
            # The market price is built from the same ensemble the bot uses
            # (a naive average), plus an independent inefficiency noise term
            # that DOES NOT depend on `outcome_yes`. The bot can only beat
            # the market by reading the ensemble better than the naive
            # average did — exactly the question we want the backtest to
            # answer.
            naive_z = (strike - mean) / max(std, 1.0)
            import math as _math

            naive_p_yes = 0.5 * (1.0 + _math.erf(-naive_z / _math.sqrt(2.0)))
            inefficiency = rng.gauss(0.0, 0.07)
            # Apply a small vig by shrinking the price toward 0.5.
            raw_price = naive_p_yes + inefficiency
            raw_price = 0.5 + (raw_price - 0.5) * 0.98
            yes_price = max(0.05, min(0.95, raw_price))
            no_price = 1.0 - yes_price

            yes_edge = prob - yes_price
            no_edge = (1.0 - prob) - no_price

            if yes_edge > no_edge:
                sim_side = "YES"
                sim_edge = yes_edge
                entry_price = yes_price
            else:
                sim_side = "NO"
                sim_edge = no_edge
                entry_price = no_price

            ev = sim_edge - 0.02
            if sim_edge >= min_edge and ev > 0:
                total_trades += 1
                prob_win = prob if sim_side == "YES" else (1.0 - prob)
                bet_size = kelly_bet_amount(
                    bankroll,
                    prob_win,
                    entry_price,
                    fraction=kelly_fraction,
                    min_bet=1.0,
                    max_bet_pct=0.03,
                )

                won = (sim_side == "YES" and outcome_yes) or (sim_side == "NO" and not outcome_yes)
                total_wagered += bet_size

                if won:
                    trades_won += 1
                    payout = bet_size / entry_price
                    # Polymarket taker fee: C × feeRate × p × (1-p)
                    shares = bet_size / entry_price
                    fee = polymarket_fee(shares, entry_price, WEATHER_FEE_RATE)
                    pnl = payout - bet_size - fee
                else:
                    trades_lost += 1
                    pnl = -bet_size

                sim_pnl += pnl
                bankroll += pnl

        conn.close()

        final_brier = sum(brier_errors) / len(brier_errors) if brier_errors else 0.25
        roi = (sim_pnl / total_wagered * 100) if total_wagered > 0 else 0.0
        win_rate = (trades_won / total_trades) if total_trades > 0 else 0.0

        logger.info(
            "  Extended Backtest Results [%d records] -> Brier=%.4f, ROI=%.2f%%, Trades=%d",
            len(groups),
            final_brier,
            roi,
            total_trades,
        )

        return {
            "brier_score": round(final_brier, 4),
            "roi": round(roi, 2),
            "win_rate": round(win_rate, 4),
            "total_bets": total_trades,
            "pnl": round(sim_pnl, 2),
        }

    @staticmethod
    def _resolve_outcome(market) -> bool | None:
        if market is None:
            return None
        raw = getattr(market, "raw_data", None)
        if not raw:
            return None
        try:
            rd = json.loads(raw) if isinstance(raw, str) else raw
            outcome = rd.get("outcome", "")
            if outcome == "YES":
                return True
            if outcome == "NO":
                return False
        except Exception:
            pass
        return None
