"""Settlement engine: resolves bets via Polymarket Gamma API resolution."""

# pylint: disable=import-error,broad-exception-caught

import json
import logging
from datetime import datetime, timedelta, timezone

import requests  # pylint: disable=import-error

from sqlalchemy import func

from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, Portfolio, WeatherMarket
from engine.strategy import RiskManager
from utils.formulas import portfolio_total_value, settlement_payout, settlement_pnl

logger = logging.getLogger("EXECUTOR_SETTLER")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class SettlementEngine:
    """Resolves open bets by reading Polymarket's official resolution data.

    Market resolution is fetched from the Gamma API (the same source the
    Polymarket UI uses).  Bets are settled only when the API indicates
    ``closed == true``, ``umaResolutionStatus == "resolved"``, and valid
    ``outcomePrices`` are available.
    """

    def __init__(self):
        pass

    # ── Public API ─────────────────────────────────────────────────────────

    def settle_all(self) -> dict:
        """Settle all markets whose ``target_date`` has passed.

        Returns dict with keys: win, loss, pending, total_pnl.
        Markets that have been pending for >48 hours get an ERROR-level alert
        but keep their current status (they will be retried on the next cycle).
        """
        won_count = 0
        lost_count = 0
        pending_count = 0
        total_pnl = 0.0
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

        with get_session() as session:
            # Create RiskManager for daily PnL tracking (circuit breaker)
            risk_manager = RiskManager()
            risk_manager.db = session
            portfolio = session.query(Portfolio).filter(Portfolio.id == 1).first()
            if portfolio and portfolio.total_value:
                risk_manager.update_portfolio(float(portfolio.total_value))

            open_statuses = ("open", "bet_placed")
            markets_to_settle = (
                session.query(WeatherMarket)
                .filter(
                    WeatherMarket.status.in_(open_statuses),
                    # Compare DATE only: settle as soon as it's the target day,
                    # not waiting for 23:59 UTC. Polymarket resolution is checked
                    # separately in _settle_market().
                    func.date(WeatherMarket.target_date) <= func.date(now_naive),
                )
                .all()
            )

            if not markets_to_settle:
                logger.info("Settlement: No markets to settle")
                return {"win": 0, "loss": 0, "pending": 0, "total_pnl": 0.0}

            for market in markets_to_settle:
                try:
                    result = self._settle_market(session, market, risk_manager)
                    if result is None:
                        pending_count += 1
                        # 48-hour pending alert (status unchanged)
                        self._check_stale_pending(market, now_naive)
                    else:
                        won_count += result.get("won", 0)
                        lost_count += result.get("lost", 0)
                        total_pnl += result["pnl"]
                except Exception as e:
                    logger.error(
                        "Settlement error for market %s: %s",
                        market.id,
                        e,
                        exc_info=True,
                    )
                    pending_count += 1

            session.commit()

        # Post-settlement portfolio sync
        if markets_to_settle:
            with get_session() as sync_session:
                portfolio = sync_session.query(Portfolio).filter(Portfolio.id == 1).first()
                if portfolio:
                    open_exposure = (
                        sync_session.query(func.coalesce(func.sum(Bet.amount), 0.0))
                        .filter(Bet.status.in_(OPEN_BET_STATUSES))
                        .scalar()
                    ) or 0.0
                    cash = float(portfolio.cash_balance or 0)
                    portfolio.total_value = portfolio_total_value(cash, float(open_exposure))
                    portfolio.current_value = portfolio.total_value
                    portfolio.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)  # pyright: ignore
                    sync_session.commit()

        logger.info(
            "Settlement complete: %s won, %s lost, %s pending, total_pnl=%.2f",
            won_count,
            lost_count,
            pending_count,
            total_pnl,
        )
        return {
            "win": won_count,
            "loss": lost_count,
            "pending": pending_count,
            "total_pnl": total_pnl,
        }

    # ── Single-market settlement ───────────────────────────────────────────

    def _settle_market(self, session, market, risk_manager=None) -> dict | None:  # pylint: disable=too-many-locals
        """Settle a single market via Gamma API resolution.

        Returns ``{"won": bool, "pnl": float}`` on success,
        ``None`` if the market is not yet resolved (pending).
        """
        open_bets = (
            session.query(Bet)
            .filter(
                Bet.market_id == market.id,
                Bet.status.in_(OPEN_BET_STATUSES),
            )
            .all()
        )

        if not open_bets:
            market.status = "expired"
            return None

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

        # ── Fetch resolution from Gamma API ────────────────────────────────
        outcome = self._fetch_market_resolution(market)
        if outcome is None and market.target_date and (now_naive - market.target_date) > timedelta(hours=24):
            # Fallback: resolution tarihi +48h geçmişse, outcomePrices'a bak
            outcome = self._fallback_price_resolution(market)
            if outcome:
                logger.warning(
                    "Market %s resolved via fallback price resolution (48h+ past target): outcome=%s",
                    market.id,
                    outcome,
                )
        if outcome is None:
            logger.warning(
                "Market %s not yet resolved by Polymarket, will retry",
                market.id,
            )
            return None  # pending, try again next cycle

        logger.info(
            "Market %s resolved by Polymarket: outcome=%s",
            market.id,
            outcome,
        )

        # ── Settle bets ────────────────────────────────────────────────────
        total_market_pnl = 0.0
        any_settled = False
        bet_won_count = 0
        bet_lost_count = 0

        for bet in open_bets:
            bet_won = bet.side == outcome
            if bet_won:
                bet_won_count += 1
            else:
                bet_lost_count += 1
            bet.status = "won" if bet_won else "lost"
            bet.settled_at = datetime.now(timezone.utc).replace(tzinfo=None)

            stake = float(bet.amount or 0)
            entry_price = float(bet.entry_price or bet.price or 0.5)
            # Load entry_fee stored at bet placement time (default 0 for pre-migration bets)
            entry_fee = float(getattr(bet, "entry_fee", 0.0) or 0.0)

            # Settlement PnL — Polymarket charges fee at entry, NOT at settlement.
            # At settlement (p→1) the fee formula gives 0: p×(1-p) = 1×0 = 0.
            realized_pnl = settlement_pnl(stake, entry_price, entry_fee, bet_won)
            payout = settlement_payout(stake, entry_price) if bet_won else 0.0

            bet.realized_pnl = round(realized_pnl, 2)
            bet.pnl = round(realized_pnl, 2)
            bet.unrealized_pnl = 0.0
            total_market_pnl += realized_pnl
            any_settled = True

            # Update daily PnL for circuit breaker (strategy.py)
            if risk_manager:
                risk_manager.update_daily_pnl(realized_pnl)

            # Update portfolio via central accounting
            from utils.accounting import credit_settlement

            portfolio = session.query(Portfolio).filter(Portfolio.id == 1).first()
            if portfolio:
                if bet_won:
                    # Credit FULL payout — the entry fee was already debited
                    # at bet placement time (bet_placer.py :: debit_stake for fee).
                    # Settlement fee is always 0 (mathematical zero at p→1).
                    credit_settlement(session, payout, 0.0, f"settle:{bet.market_id}:won")
                    portfolio.total_won = (portfolio.total_won or 0) + 1
                else:
                    portfolio.total_lost = (portfolio.total_lost or 0) + 1
                portfolio.total_realized_pnl = (portfolio.total_realized_pnl or 0) + realized_pnl

        if any_settled:
            market.status = "settled_win" if outcome == "YES" else "settled_loss"
            # raw_data populated inside _fetch_market_resolution

        return {"won": bet_won_count, "lost": bet_lost_count, "pnl": total_market_pnl}

    # ── Gamma API resolution ───────────────────────────────────────────────

    def _fetch_market_resolution(self, market) -> str | None:
        """Fetch market resolution from Polymarket Gamma API.

        Returns ``"YES"``, ``"NO"``, or ``None`` if not yet resolved.

        Resolution criteria (ALL must hold):
          1. ``closed == true``
          2. ``umaResolutionStatus == "resolved"``
          3. ``outcomePrices`` is a parseable JSON string list
        """
        data = self._call_gamma_api(market)
        if data is None:
            return None

        if not data.get("closed") or data.get("umaResolutionStatus") != "resolved":
            return None

        prices = self._parse_outcome_prices(market, data.get("outcomePrices"))
        if prices is None:
            return None

        try:
            yes_price = float(prices[0])
            no_price = float(prices[1])
        except (TypeError, ValueError):
            logger.warning(
                "Non-numeric outcomePrices for %s: %s",
                market.id,
                prices,
            )
            return None

        if yes_price >= 0.99:
            outcome = "YES"
        elif no_price >= 0.99:
            outcome = "NO"
        else:
            logger.warning(
                "Split/no-clear resolution for %s: outcomePrices=%s (neither side >= 0.99)",
                market.id,
                prices,
            )
            return None

        market.raw_data = json.dumps(
            {
                "source": "polymarket",
                "outcome": outcome,
                "outcomePrices": prices,
                "umaResolutionStatus": data.get("umaResolutionStatus"),
                "settled_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return outcome

    def _fallback_price_resolution(self, market) -> str | None:
        """Fallback: if target_date +48h passed but Gamma API not resolved,
        use current outcomePrices to determine winner.
        YES >= 0.98 → YES won, NO >= 0.98 → NO won."""
        data = self._call_gamma_api(market)
        if data is None:
            return None
        prices = self._parse_outcome_prices(market, data.get("outcomePrices"))
        if prices is None:
            return None
        try:
            yes_price = float(prices[0])
            no_price = float(prices[1])
        except (TypeError, ValueError):
            return None
        if yes_price >= 0.98:
            outcome = "YES"
        elif no_price >= 0.98:
            outcome = "NO"
        else:
            return None
        market.raw_data = json.dumps(
            {
                "source": "fallback_price",
                "outcome": outcome,
                "outcomePrices": prices,
                "settled_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return outcome

    def _call_gamma_api(self, market) -> dict | None:
        """Make GET request to Gamma API and return parsed JSON."""
        try:
            url = f"{GAMMA_API_BASE}/markets/{market.id}"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(
                "Gamma API request failed for %s: %s",
                market.id,
                e,
            )
            return None

    @staticmethod
    def _parse_outcome_prices(market, raw_prices) -> list | None:
        """Parse outcomePrices into a validated list."""
        if not raw_prices:
            return None
        try:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Cannot parse outcomePrices for %s: %s",
                market.id,
                raw_prices,
            )
            return None
        if not isinstance(prices, (list, tuple)) or len(prices) < 2:
            logger.warning(
                "Invalid outcomePrices format for %s: %s",
                market.id,
                prices,
            )
            return None
        return list(prices)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _check_stale_pending(market, now_naive: datetime) -> None:
        """Log an ERROR if a market has been pending longer than 48 hours."""
        if market.target_date and (now_naive - market.target_date) > timedelta(hours=48):
            logger.error(
                "Market %s has been pending >48h (target=%s). Gamma API may not have resolved it yet.",
                market.id,
                market.target_date,
            )
