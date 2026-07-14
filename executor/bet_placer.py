"""Bet placement executor making paper or live trades on Polymarket."""

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import func

from config.settings import Config, bot_config
from database.db import get_session
from database.models import OPEN_BET_STATUSES, Analysis, Bet, Portfolio, WeatherMarket
from engine.decision import BetDecision
from utils.formulas import (
    bet_shares,
    max_bet_cap,
    polymarket_fee_from_stake,
    portfolio_total_value,
)
from utils.price_sanity import is_valid_binary_price
from utils.slippage import check_orderbook_depth, estimate_slippage

logger = logging.getLogger("EXECUTOR_BET_PLACER")


class BetPlacer:
    """SADECE bet açar. Karar vermez - engine karar verir."""

    # Statuses that count as "open" for risk/exposure accounting.
    _OPEN_STATUSES = OPEN_BET_STATUSES

    def __init__(self):
        # Lazy-import risk manager to break import cycle:
        #   engine/strategy.py  ->  imports from this module
        #   executor/bet_placer.py  ->  uses engine.strategy.RiskManager
        from engine.strategy import RiskManager

        # NOTE: RiskManager is created WITHOUT a db_session here.
        # The session is bound per-call in place_bet() so that
        # _conservative_portfolio_value() always sees fresh committed data
        # instead of falling back to INITIAL_PORTFOLIO ($1000).
        self.risk_manager = RiskManager()

        # Hard guard: the user requires paper-only mode.
        if Config.DRY_RUN:
            self.ready = False
            logger.info(
                "DRY_RUN=true is enforced. BetPlacer will ONLY emit paper/simulated "
                "orders; the live Polymarket CLOB client is not initialized."
            )
        else:
            self._init_polymarket_client()

    def _init_polymarket_client(self):
        """Polymarket CLOB client hazirla (sadece DRY_RUN=false ise cagrilir)."""
        try:
            from py_clob_client.client import (
                ClobClient,  # pylint: disable=import-error,no-name-in-module
            )

            if not bot_config.polymarket.private_key:
                self.ready = False
                logger.info("Polymarket credentials not found, running in PAPER/SIMULATION trade mode.")
                return

            self.client = ClobClient(
                bot_config.polymarket.api_url,
                key=bot_config.polymarket.private_key,
                chain_id=137,
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self.ready = True
            logger.warning(
                "LIVE TRADING ARMED -- DRY_RUN=false and credentials present. Real orders will be sent to Polymarket."
            )
        except Exception as e:
            logger.warning(f"Polymarket client kurulamadi (PAPER TRADE ACTIVE): {e}")
            self.ready = False

    def place_bet(self, analysis_id: int) -> Bet | None:
        """Analiz sonucuna göre bet aç."""
        d = BetDecision(market_id=f"analysis:{analysis_id}")
        with get_session() as session:
            # Bind session to risk manager so _conservative_portfolio_value()
            # queries DB instead of returning stale INITIAL_PORTFOLIO.
            self.risk_manager.db = session
            analysis = session.query(Analysis).filter_by(id=analysis_id).first()
            d.check("analysis_exists", analysis is not None and analysis.should_bet)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None

            market = session.query(WeatherMarket).filter_by(id=analysis.market_id).first()
            d.check("market_exists", market is not None)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None
            d.market_id = market.id

            # Guard: daily loss limit (circuit breaker)
            if self.risk_manager.is_bot_locked():
                d.check("daily_loss_limit", False, daily_pnl=self.risk_manager.daily_pnl)
                d.log(logging.WARNING)
                return None

            # Price sanity check - skip invalid binary markets
            price_valid = is_valid_binary_price(market.yes_price or 0, market.no_price or 0)
            d.check("price_valid", price_valid, yes=market.yes_price, no=market.no_price)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None

            # Guard: skip resolved markets
            _now = datetime.now(timezone.utc).replace(tzinfo=None)
            date_ok = not (market.target_date and market.target_date <= _now)
            d.check("target_date_ok", date_ok, target_date=str(market.target_date) if market.target_date else None)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None

            # Guard: skip markets with no real liquidity (Karpathy-search-
            # discovered min_entry_price filter — long-shot bets are the
            # source of the asymmetric-payoff bleed).
            market_price = float(market.yes_price or 0.5)
            # Prefer the Karpathy-tuned strategy value; fall back to legacy
            # Config.MIN_ENTRY_PRICE for backwards compatibility.
            strategy_min_price = getattr(self.risk_manager.config, "strategy", None)
            if strategy_min_price is not None and hasattr(strategy_min_price, "min_entry_price"):
                min_price = float(strategy_min_price.min_entry_price)
            else:
                min_price = float(getattr(self.risk_manager.config, "MIN_ENTRY_PRICE", 0.01))
            d.check("min_entry_price", market_price >= min_price, price=market_price, min_price=min_price)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None

            # Zaten bu market'e herhangi bir bahis açılmış mı?
            # NOT: Sadece OPENstatuses kontrol etmiyoruz — closed_early,
            # settled, won, lost durumları da dahil. Ayni market'e
            # tekrar bahis açilmasini engelliyoruz.
            existing = (
                session.query(Bet)
                .filter(
                    Bet.market_id == analysis.market_id,
                    Bet.status != "rejected",
                )
                .first()
            )
            d.check(
                "no_existing_bet",
                existing is None,
                existing_id=existing.id if existing else None,
                existing_status=existing.status if existing else None,
            )
            if not d.should_bet:
                d.log(logging.INFO)
                return None

            # ------------------------------------------------------------------
            # Sync RiskManager portfolio_value from DB so risk caps reflect
            # actual portfolio state (ONLY realized PnL, no unrealized).
            # This prevents the feedback loop where unrealized profits
            # inflate portfolio → raise 25% cap → allow more bets → etc.
            _pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            if _pf and _pf.total_value is not None:
                # Use conservative value (initial + realized only)
                self.risk_manager.update_portfolio(self.risk_manager._conservative_portfolio_value())

            # Risk checks. These are enforced HERE (not in run_place_bets)
            # so every entry point "" scheduler, manual API call, CLI "" is
            # guarded by the same hard caps. A previous version of this
            # module skipped all caps and let exposure balloon to 35x the
            # smart-pool ceiling, which is what surfaced the
            # "$14,000 exposure vs $400 smart pool" dashboard disconnect.
            # ------------------------------------------------------------------
            proposed_amount = float(analysis.recommended_amount or 0.0)
            d.proposed_amount = proposed_amount

            # Optional flat-bet override: when Config.FLAT_BET_USD > 0,
            # every bet is exactly that many USD, ignoring Kelly sizing.
            # Useful for backtests and small-portfolio testing where
            # Kelly-derived sizes would otherwise be too small to matter.
            # Risk caps below still apply on top.
            flat_bet = float(getattr(self.risk_manager.config, "FLAT_BET_USD", 0.0) or 0.0)
            if flat_bet > 0.0:
                logger.info(
                    f"Flat-bet override active: ${flat_bet:.2f} per bet (was ${proposed_amount:.2f} from Kelly)."
                )
                proposed_amount = flat_bet
                d.set_param("flat_bet_override", True)

            # Cap 1: per-bet cap (MAX_BET_PCT * portfolio). Formula from
            # utils/formulas.py → max_bet_cap(). Kelly sizing in calculator.py
            # already enforces this, but we re-apply here as a hard ceiling.
            max_bet = max_bet_cap(
                float(self.risk_manager.portfolio_value),
                float(self.risk_manager.config.MAX_BET_PCT),
            )
            if proposed_amount > max_bet:
                logger.warning(
                    f"Risk cap: Market {market.id} amount ${proposed_amount:.2f} "
                    f"exceeds per-bet max ${max_bet:.2f} — clamping."
                )
                proposed_amount = max_bet
            d.set_param("max_bet_cap", max_bet)

            # Cap 2: total exposure cap (TOTAL_EXPOSURE_PCT * conservative portfolio).
            # check_exposure_cap now dynamically computes conservative value
            # (cash + open_exposure) from DB, so no stale portfolio_value.
            current_exposure = (
                session.query(func.coalesce(func.sum(Bet.amount), 0.0))
                .filter(Bet.status.in_(self._OPEN_STATUSES))
                .scalar()
            ) or 0.0
            current_exposure = float(current_exposure)
            exposure_ok = self.risk_manager.check_exposure_cap(current_exposure, proposed_amount)
            conservative_value = self.risk_manager._conservative_portfolio_value()
            max_exposure = float(conservative_value) * float(self.risk_manager.config.TOTAL_EXPOSURE_PCT)
            d.check(
                "exposure_cap",
                exposure_ok,
                current=current_exposure,
                proposed=proposed_amount,
                max_exposure=max_exposure,
                conservative=conservative_value,
            )
            if not exposure_ok:
                logger.warning(
                    f"Risk cap: Market {market.id} rejected — exposure would "
                    f"reach ${current_exposure + proposed_amount:.2f}, "
                    f"exceeding cap ${max_exposure:.2f} "
                    f"(conservative=${conservative_value:.2f})."
                )
                # Record a synthetic "rejected" bet row for audit visibility
                # so the user can see WHY exposure is being held back.
                rejected = Bet(
                    market_id=analysis.market_id,
                    analysis_id=analysis_id,
                    city=market.city,
                    city_code=market.city_code,
                    side=analysis.recommended_side,
                    amount=proposed_amount,
                    price=(market.yes_price if analysis.recommended_side == "YES" else market.no_price),
                    status="rejected",
                    error_message=(
                        f"Exposure cap: ${current_exposure:.2f} + ${proposed_amount:.2f} > "
                        f"${max_exposure:.2f} (conservative=${conservative_value:.2f})"
                    ),
                )
                session.add(rejected)
                session.commit()
                d.log(logging.WARNING)
                return None

            # Cap 3: city cap (CITY_CAP per city).
            city_key = (market.city or "").lower()
            city_open_count = (
                session.query(func.count(Bet.id))  # pylint: disable=not-callable
                .join(WeatherMarket, Bet.market_id == WeatherMarket.id)
                .filter(
                    Bet.status.in_(self._OPEN_STATUSES),
                    func.lower(WeatherMarket.city) == city_key,
                )
                .scalar()
            ) or 0
            city_cap = int(self.risk_manager.config.CITY_CAP)
            city_ok = int(city_open_count) < city_cap
            d.check("city_cap", city_ok, city=market.city, open_count=int(city_open_count), max_city=city_cap)
            if not city_ok:
                logger.warning(
                    f"Risk cap: Market {market.id} rejected — city cap "
                    f"({city_open_count}/{city_cap}) "
                    f"reached for {market.city}."
                )
                rejected = Bet(
                    market_id=analysis.market_id,
                    analysis_id=analysis_id,
                    city=market.city,
                    city_code=market.city_code,
                    side=analysis.recommended_side,
                    amount=proposed_amount,
                    price=(market.yes_price if analysis.recommended_side == "YES" else market.no_price),
                    status="rejected",
                    error_message=f"City cap: {city_open_count}/{city_cap} for {market.city}",
                )
                session.add(rejected)
                session.commit()
                d.log(logging.WARNING)
                return None

            # Extract condition_id from market.raw_data for slippage & depth check
            condition_id = None
            try:
                raw = json.loads(market.raw_data) if market.raw_data else {}
                for tok in raw.get("tokens", []):
                    if tok.get("outcome", "").upper() == (analysis.recommended_side or "").upper():
                        condition_id = tok.get("condition_id") or tok.get("token_id")
                        break
            except (json.JSONDecodeError, TypeError):
                pass

            # Resolve fill price for the chosen side, adjusted for slippage
            raw_fill = market.yes_price if analysis.recommended_side == "YES" else market.no_price
            raw_fill = float(raw_fill) if raw_fill is not None else 0.0
            slip_est = estimate_slippage(raw_fill, stake_usd=proposed_amount, condition_id=condition_id)
            fill_price = raw_fill * (1.0 + slip_est.slippage_pct)
            fill_price = max(0.01, min(0.99, round(fill_price, 4)))
            # Shares = amount / price (position size in contracts).
            # Formula from utils/formulas.py → bet_shares().
            shares = bet_shares(proposed_amount, fill_price)
            logger.info(
                f"Slippage adjustment: raw={raw_fill:.4f} → fill={fill_price:.4f} "
                f"(slip={slip_est.slippage_pct:.2%}, model={slip_est.model_used})"
            )
            d.set_param("slippage_pct", slip_est.slippage_pct)
            d.set_param("slippage_model", slip_est.model_used)

            min_depth = float(getattr(bot_config.strategy, "min_depth_usd", 0.0) or 0.0)
            depth_ok, depth_usd = check_orderbook_depth(
                condition_id,
                analysis.recommended_side or "YES",
                fill_price,
                proposed_amount,
                min_depth_usd=min_depth,
            )
            if not depth_ok:
                logger.warning(f"Market {market.id}: depth filter rejected (${depth_usd:.2f} < ${min_depth:.2f} min)")
                d.check("depth_ok", False, depth_usd=depth_usd, min_depth=min_depth)
                rejected = Bet(
                    market_id=analysis.market_id,
                    analysis_id=analysis_id,
                    city=market.city,
                    city_code=market.city_code,
                    side=analysis.recommended_side,
                    amount=proposed_amount,
                    price=fill_price,
                    status="rejected",
                    error_message=f"Depth filter: ${depth_usd:.2f} < ${min_depth:.2f}",
                )
                session.add(rejected)
                session.commit()
                d.log(logging.WARNING)
                return None
            d.check("depth_ok", True, depth_usd=depth_usd, min_depth=min_depth)

            # Calculate Polymarket taker fee at entry time.
            # Official formula: fee = stake × feeRate × (1-p)
            # This is charged at match time, NOT at settlement.
            # See utils/formulas.py → polymarket_fee_from_stake().
            fee_rate = Config.WEATHER_FEE_RATE
            entry_fee = polymarket_fee_from_stake(proposed_amount, fill_price, fee_rate)

            # Bet objesi oluştur
            fair_value = float(analysis.estimated_probability or 0.5)
            bet = Bet(
                market_id=analysis.market_id,
                analysis_id=analysis_id,
                city=market.city,  # FIX: copy city from market so the
                city_code=market.city_code,  # dashboard "City" column is populated
                side=analysis.recommended_side,
                amount=proposed_amount,
                stake_amount=proposed_amount,  # FIX: set stake_amount for ROI calculations
                price=fill_price,
                entry_price=fill_price,  # NEW: source of truth for PNL math
                shares=shares,  # NEW: needed for unrealized_pnl
                current_price=fill_price,  # NEW: starts equal to entry, refreshed by run_update_prices
                status="pending",
                fair_value=fair_value,
                expected_value=float(analysis.edge or 0.0),
                entry_fee=round(entry_fee, 4),
            )

            bet.potential_payout = bet.amount / bet.price if bet.price > 0 else 0

            # Paper ladder: if edge >= 0.05, create a 3-level ladder
            ladder_orders = []
            edge_val = float(analysis.edge or 0.0)
            if abs(edge_val) >= 0.05:
                for lvl, pct in [(1, 0.50), (2, 0.30), (3, 0.20)]:
                    lvl_amount = round(proposed_amount * pct, 2)
                    if lvl == 1:
                        lvl_price = fill_price
                    elif lvl == 2:
                        lvl_price = fill_price * 0.98
                    else:
                        lvl_price = fill_price * 0.95
                    # Clamp price to [0.01, 0.99]
                    lvl_price = max(0.01, min(0.99, round(lvl_price, 4)))
                    lvl_shares = round(lvl_amount / lvl_price, 4) if lvl_price > 0 else 0.0
                    ladder_orders.append(
                        {
                            "level": lvl,
                            "price": lvl_price,
                            "amount": lvl_amount,
                            "shares": lvl_shares,
                            "status": "pending",
                        }
                    )
            bet.ladder_data = json.dumps(ladder_orders) if ladder_orders else "[]"

            # Live vs Paper execution logic
            # HARD GUARD: always paper unless LIVE_TRADING_ENABLED=true
            _live_allowed = (not Config.DRY_RUN) and os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
            if self.ready and _live_allowed:
                try:
                    from py_clob_client.order_builder.constants import (
                        BUY,  # pylint: disable=import-error,no-name-in-module
                    )

                    order = self.client.create_and_post_order(
                        {
                            "token_id": self._get_token_id(market, analysis.recommended_side),
                            "price": bet.price,
                            "size": bet.amount / bet.price,
                            "side": BUY,
                        }
                    )

                    bet.order_id = order.get("orderID")
                    bet.status = "placed"
                    bet.placed_at = datetime.now(timezone.utc).replace(tzinfo=None)

                    market.status = "bet_placed"
                    logger.info(
                        f"LIVE BET OPENED: {market.id} | {analysis.recommended_side} ${bet.amount:.2f} @ {bet.price}"
                    )
                except Exception as e:
                    bet.status = "failed"
                    bet.error_message = str(e)
                    logger.error(f"Live Bet failed {market.id}: {e}")
            else:
                # Simulated / Paper trade fallback. Also covers the case
                # where Config.DRY_RUN is true (defense-in-depth).
                now_ts = int(datetime.now(timezone.utc).replace(tzinfo=None).timestamp())
                bet.order_id = f"paper_order_{market.id}_{now_ts}"
                bet.status = "placed"
                bet.placed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                market.status = "bet_placed"
                logger.info(
                    f"PAPER BET OPENED: {market.id} | "
                    f"{analysis.recommended_side} ${bet.amount:.2f} @ {bet.price} "
                    f"({shares:.2f} shares)"
                )

            # Deduct stake from portfolio cash — via central accounting API.
            # Ladder: L1 is filled immediately; L2/L3 stay pending.
            from utils.accounting import debit_stake

            initial_stake = proposed_amount
            if ladder_orders:
                l1_amount = ladder_orders[0].get("amount") if isinstance(ladder_orders[0], dict) else None
                if l1_amount and l1_amount > 0:
                    initial_stake = l1_amount
                    # Mark L1 as filled immediately (prevents double-debit in run_update_prices)
                    ladder_orders[0]["status"] = "filled"
                    ladder_orders[0]["filled_at"] = datetime.now(timezone.utc).isoformat()
                    # Persist updated ladder back to bet.ladder_data
                    bet.ladder_data = json.dumps(ladder_orders)
            try:
                debit_stake(session, initial_stake, f"bet_open:{bet.market_id}")
                # Also debit the Polymarket taker fee paid at match time.
                # On Polymarket, fee = stake × feeRate × (1-p) is charged at
                # entry, NOT at settlement. See utils/formulas.py → polymarket_fee*().
                if entry_fee > 0:
                    debit_stake(session, entry_fee, f"bet_fee:{bet.market_id}")
            except ValueError as e:
                logger.error("Cannot open bet %s: %s", bet.market_id, e)
                bet.status = "failed"
                bet.error_message = str(e)
                session.add(bet)
                session.commit()
                return bet
            portfolio = session.query(Portfolio).filter(Portfolio.id == 1).first()
            if portfolio:
                # Include unrealized PnL from other open bets in current_value
                open_exposure = (
                    session.query(func.coalesce(func.sum(Bet.amount), 0.0))
                    .filter(Bet.status.in_(OPEN_BET_STATUSES))
                    .scalar()
                ) or 0.0
                portfolio.current_value = portfolio_total_value(portfolio.cash_balance or 0.0, float(open_exposure))
                portfolio.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(bet)
            session.commit()
            # Final structured decision log — one JSON line per placed bet.
            d.final_amount = proposed_amount
            d.set_param("entry_fee", round(entry_fee, 4))
            d.set_param("fill_price", fill_price)
            d.set_param("shares", shares)
            d.set_param("side", analysis.recommended_side)
            d.set_param("status", bet.status)
            d.log(logging.INFO)
            return bet

    def _get_token_id(self, market, side: str) -> str:
        """Market'ten token ID al."""
        raw = json.loads(market.raw_data) if market.raw_data else {}
        tokens = raw.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == side.upper():
                return token.get("token_id")
        raise ValueError(f"Token ID bulunamadı: {side}")

    def place_all_pending(self) -> int:
        """should_bet=True olan tum analizler icin bet ac."""
        placed = 0
        # Build mapping of analysis_id -> market_id + set of markets that
        # already have bets, inside a single session.
        aid_to_market: dict[int, str] = {}
        markets_with_bets: set[str] = set()

        with get_session() as session:
            # Only use the LATEST analysis per market (highest id).
            # This prevents old analyses with stale recommended_amount
            # (e.g. pre-config-change $29.70) from being placed.
            from sqlalchemy import func as sa_func

            subq = (
                session.query(
                    Analysis.market_id,
                    sa_func.max(Analysis.id).label("max_id"),
                )
                .filter(Analysis.should_bet.is_(True))
                .group_by(Analysis.market_id)
                .subquery()
            )
            pending = session.query(Analysis).join(subq, Analysis.id == subq.c.max_id).all()

            # Dedup: skip market_ids that already have ANY non-rejected Bet.
            # Previous logic deduped by analysis_id which was useless — SIA
            # creates a new analysis (new ID) each cycle for the same market,
            # so the old check never caught duplicates.
            market_ids = {a.market_id for a in pending}
            if market_ids:
                existing_rows = (
                    session.query(Bet.market_id)
                    .filter(
                        Bet.market_id.in_(list(market_ids)),
                        Bet.status != "rejected",
                    )
                    .all()
                )
                markets_with_bets = {row[0] for row in existing_rows if row[0] is not None}

            for a in pending:
                aid_to_market[a.id] = a.market_id

        for aid, mkt_id in aid_to_market.items():
            if mkt_id in markets_with_bets:
                logger.debug(
                    "Market %s already has a bet, skipping analysis %d",
                    mkt_id,
                    aid,
                )
                continue
            try:
                bet = self.place_bet(aid)
                if bet is not None:
                    placed += 1
                    # Track this market to skip duplicate analyses in same batch
                    markets_with_bets.add(mkt_id)
            except Exception as e:
                logger.error(f"Bet hatasi (analysis {aid}): {e}")
                continue

        return placed
