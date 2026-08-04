"""Bet placement executor making paper or live trades on Polymarket."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

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
    """SADECE bet acar. Karar vermez - engine karar verir."""

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
        self.risk_manager = RiskManager(cfg=Config)

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
        """Analiz sonucuna gore bet ac."""
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

            # YES-only guard: asla NO bahis acma
            side = (analysis.recommended_side or "").upper()
            d.check("yes_only", side == "YES", recommended_side=analysis.recommended_side)
            if not d.should_bet:
                logger.warning(
                    "YES-only guard: analysis %d recommends %s - rejected", analysis_id, analysis.recommended_side
                )
                d.log(logging.WARNING)
                return None

            # YES price gate: [0.10, 0.95).
            _min_entry = float(getattr(bot_config.strategy, "min_entry_price", 0.10))
            _max_entry = float(getattr(bot_config.strategy, "max_entry_price", 0.95))
            _yp = float(market.yes_price or 0.5)
            if not (_min_entry <= _yp < _max_entry):
                logger.info(
                    "Price gate: %s yes_price=%.3f outside [%.2f, %.2f) - bet refused",
                    market.id,
                    _yp,
                    _min_entry,
                    _max_entry,
                )
                d.check("max_entry_price", False, yes_price=_yp, max_entry=_max_entry)
                d.log(logging.INFO)
                return None

            # Guard: daily loss limit (circuit breaker) - DISABLED
            if False and self.risk_manager.is_bot_locked():
                d.check("daily_loss_limit", False, daily_pnl=self.risk_manager.daily_pnl)
                d.log(logging.WARNING)
                return None

            # Price sanity check - skip invalid binary markets
            price_valid = is_valid_binary_price(market.yes_price or 0, market.no_price or 0)
            d.check("price_valid", price_valid, yes=market.yes_price, no=market.no_price)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None

            # Guard: skip resolved markets (expiry check only).
            _now = datetime.now(timezone.utc).replace(tzinfo=None)
            date_ok = True
            if market.target_date:
                secs_left = (market.target_date - _now).total_seconds()
                if secs_left <= 0:
                    date_ok = False  # vadesi gecmis
            d.check("target_date_ok", date_ok, target_date=str(market.target_date) if market.target_date else None)
            if not d.should_bet:
                d.log(logging.DEBUG)
                return None

            # Zaten bu market'e AKTIF bir bahis acilmis mi?
            # Sadece aktif (placed/partial_fill/filled) bet'leri kontrol et.
            # Closed/settled/rejected/failed bet'leri engellemiyoruz —
            # ayni market'e yeni bahis acilabilir.
            existing = (
                session.query(Bet)
                .filter(
                    Bet.market_id == analysis.market_id,
                    Bet.status.in_(["placed", "partial_fill", "filled"]),
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
            # inflate portfolio -> raise 25% cap -> allow more bets -> etc.
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
            # utils/formulas.py -> max_bet_cap(). Kelly sizing in calculator.py
            # already enforces this, but we re-apply here as a hard ceiling.
            max_bet = max_bet_cap(
                float(self.risk_manager.portfolio_value),
                float(self.risk_manager.config.MAX_BET_PCT),
            )
            if proposed_amount > max_bet:
                logger.warning(
                    f"Risk cap: Market {market.id} amount ${proposed_amount:.2f} "
                    f"exceeds per-bet max ${max_bet:.2f} - clamping."
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
                    f"Risk cap: Market {market.id} rejected - exposure would "
                    f"reach ${current_exposure + proposed_amount:.2f}, "
                    f"exceeding cap ${max_exposure:.2f} "
                    f"(conservative=${conservative_value:.2f})."
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
                    error_message=(
                        f"Exposure cap: ${current_exposure:.2f} + ${proposed_amount:.2f} > "
                        f"${max_exposure:.2f} (conservative=${conservative_value:.2f})"
                    ),
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
            # Formula from utils/formulas.py -> bet_shares().
            shares = bet_shares(proposed_amount, fill_price)
            logger.info(
                f"Slippage adjustment: raw={raw_fill:.4f} -> fill={fill_price:.4f} "
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
            # Official formula: fee = stake x feeRate x (1-p)
            # This is charged at match time, NOT at settlement.
            # See utils/formulas.py -> polymarket_fee_from_stake().
            fee_rate = bot_config.strategy.current_fee_rate
            entry_fee = polymarket_fee_from_stake(proposed_amount, fill_price, fee_rate)

            # Bet objesi olustur
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
                strike_temp=float(market.threshold or 0.0),  # NEW: copy threshold from market
            )

            bet.potential_payout = bet.amount / bet.price if bet.price > 0 else 0

            # Single-fill execution; no deferred ladder orders.

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
                    return None
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

            # Deduct the complete single-fill stake from portfolio cash.
            from utils.accounting import debit_stake

            initial_stake = proposed_amount
            try:
                debit_stake(session, initial_stake, f"bet_open:{bet.market_id}")
                # Also debit the Polymarket taker fee paid at match time.
                # On Polymarket, fee = stake x feeRate x (1-p) is charged at
                # entry, NOT at settlement. See utils/formulas.py -> polymarket_fee*().
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
            # Final structured decision log - one JSON line per placed bet.
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
        raise ValueError(f"Token ID bulunamadi: {side}")

    def place_all_pending(self) -> int:
        """Tum acik Polymarket weather marketleri icin bet ac.
        Analiz sonucuna bakilmaz — Polymarket'te hangi derece en yuksek
        fiyatlanmissa ona bet acilir. Smart rotation: ayni grupta daha iyi
        fiyatlı market bulunursa eski bet kapatilir, yenisi acilir.
        """
        from collections import defaultdict

        placed = 0
        rotated = 0

        with get_session() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            # 1) Tum acik ve gelecek tarihli marketleri cek
            open_markets = (
                session.query(WeatherMarket)
                .filter(
                    WeatherMarket.status == "open",
                    WeatherMarket.target_date.isnot(None),
                    WeatherMarket.yes_price.isnot(None),
                    WeatherMarket.yes_price > 0,
                    WeatherMarket.target_date > now + timedelta(hours=8),
                    WeatherMarket.city != "Unknown",
                )
                .all()
            )
            logger.info("place_all_pending: %d open markets found", len(open_markets))

            # 2) (city, target_date, metric) -> list of (market, yes_price)
            by_group: dict[tuple, list] = defaultdict(list)
            for mkt in open_markets:
                td = mkt.target_date
                if getattr(td, "tzinfo", None):
                    td = td.replace(tzinfo=None)
                key = (mkt.city, td, mkt.metric or "unknown")
                by_group[key].append((mkt, float(mkt.yes_price or 0)))

            # 3) Her grupta en yuksek fiyatli market(ler)i sec.
            best_markets = []
            for (city, td, metric), candidates in by_group.items():
                best_price = max(p for _, p in candidates)
                if best_price > 0:
                    # En yuksek fiyatli marketi bul
                    best_mkt = next(m for m, p in candidates if abs(p - best_price) < 1e-9)
                    best_markets.append((city, td, metric, best_mkt, best_price))

            logger.info(
                "place_all_pending: %d groups, %d best markets selected",
                len(by_group),
                len(best_markets),
            )

            # 4) Mevcut aktif bet'leri yukle: (city, date, metric) -> [Bet]
            active_bets = session.query(Bet).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
            active_by_group: dict[tuple, list] = defaultdict(list)
            for b in active_bets:
                wm = session.query(WeatherMarket).filter_by(id=b.market_id).first()
                if wm and wm.target_date:
                    td = wm.target_date
                    if getattr(td, "tzinfo", None):
                        td = td.replace(tzinfo=None)
                    key = (wm.city, td, wm.metric or "unknown")
                    active_by_group[key].append(b)

            # 5) Her grup icin: EN YUKSEK fiyatli bet'i sec
            rotation_threshold = float(getattr(bot_config.strategy, "rotation_threshold", 0.05) or 0.05)
            for city, td, metric, best_mkt, best_price in best_markets:
                key = (city, td, metric)
                group_bets = active_by_group.get(key, [])

                # Mevcut bet'in market'ini kontrol et
                if group_bets:
                    old_bet = group_bets[0]
                    old_mkt = session.query(WeatherMarket).filter_by(id=old_bet.market_id).first()
                    old_price = float(old_mkt.yes_price or 0) if old_mkt else 0

                    # Ayni market veya fiyat iyilesmesi rotation_threshold altindaysa rotation yapma
                    same_market = str(old_bet.market_id) == str(best_mkt.id)
                    price_improvement = best_price - old_price if old_price > 0 else 0

                    if same_market:
                        logger.info(
                            "Rotation skipped: %s %s same market (price=%.4f)",
                            city,
                            str(td.date()),
                            best_price,
                        )
                        continue
                    if price_improvement < rotation_threshold:
                        logger.info(
                            "Rotation skipped: %s %s improvement %.4f < threshold %.4f",
                            city,
                            str(td.date()),
                            price_improvement,
                            rotation_threshold,
                        )
                        continue

                # Grubun tamamini kapat (varsa)
                for old_bet in group_bets:
                    old_mkt = session.query(WeatherMarket).filter_by(id=old_bet.market_id).first()
                    old_price = float(old_mkt.yes_price or 0) if old_mkt else 0
                    logger.info("Closing bet#%s %s %s (price=%.4f)", old_bet.id, city, str(td.date()), old_price)
                    self.close_bet_for_rotation(old_bet, old_price, session)
                    rotated += 1

                # Yeni bet ac
                bet = self.open_bet_on_market(best_mkt, session)
                if bet:
                    placed += 1
                    active_by_group[key].append(bet)

        logger.info(
            "place_all_pending done: %d placed, %d rotated (smart rotation)",
            placed,
            rotated,
        )
        return placed

    def close_losing_twin_bets(self, session=None) -> int:
        """Tie olarak acilan ikiz betlerden geride kalanini kapat.

        Ayni (city, target_date, metric) grubunda birden fazla acik bet varsa
        (tie acilimi nedeniyle) ve en yuksek fiyatli ile arasindaki fark
        ``tie_loser_gap``'i asiyorsa, geride olan bet kapatilir. Boylece sona
        dogru biri one gecince digeri otomatik satilir.
        """
        from collections import defaultdict

        if not bool(getattr(bot_config.strategy, "tie_bet_enabled", True)):
            return 0

        gap = float(getattr(bot_config.strategy, "tie_loser_gap", 0.10) or 0.10)
        if not gap or gap <= 0:
            return 0

        closed = 0
        with get_session() as s:
            active = s.query(Bet).filter(Bet.status.in_(OPEN_BET_STATUSES), Bet.side == "YES").all()
            if not active:
                return 0

            # (city, date, metric) -> [(bet, market)]
            groups: dict[tuple, list] = defaultdict(list)
            for b in active:
                wm = s.query(WeatherMarket).filter_by(id=b.market_id).first()
                if not wm or not wm.target_date:
                    continue
                td = wm.target_date
                if getattr(td, "tzinfo", None):
                    td = td.replace(tzinfo=None)
                key = (wm.city, td, wm.metric or "unknown")
                cur = float(wm.yes_price or 0)
                groups[key].append((b, wm, cur))

            for key, entries in groups.items():
                if len(entries) < 2:
                    continue
                entries.sort(key=lambda x: x[2], reverse=True)
                leader_price = entries[0][2]
                for bet, wm, cur in entries[1:]:
                    if leader_price - cur >= gap:
                        logger.info(
                            "Twin loser close: %s %s %s cur=%.4f leader=%.4f gap=%.2f",
                            key[0],
                            str(key[1].date()),
                            key[2],
                            cur,
                            leader_price,
                            leader_price - cur,
                        )
                        self.close_bet_for_rotation(bet, cur, s)
                        closed += 1

        if closed:
            logger.info("close_losing_twin_bets: %d positions closed", closed)
        return closed

    def open_bet_on_market(self, market: WeatherMarket, session) -> Bet | None:
        """Dogrudan bir market'e bet ac. Analysis gerektirmez."""

        # Zaten bu market'te aktif bet var mi?
        existing = (
            session.query(Bet)
            .filter(
                Bet.market_id == str(market.id),
                Bet.status.in_(OPEN_BET_STATUSES),
            )
            .first()
        )
        if existing:
            return None

        # Bet tutari — exposure room'a gore kisitla
        flat_bet = float(getattr(bot_config.strategy, "flat_bet_usd", 10.0) or 10.0)
        amount = flat_bet

        # Exposure kontrolu — kalan room'a gore bet boyutunu ayarla
        current_exposure = (
            session.query(func.coalesce(func.sum(Bet.amount), 0.0)).filter(Bet.status.in_(self._OPEN_STATUSES)).scalar()
        ) or 0.0
        current_exposure = float(current_exposure)
        conservative = self.risk_manager._conservative_portfolio_value()
        max_exposure = float(conservative) * float(self.risk_manager.config.TOTAL_EXPOSURE_PCT)
        remaining_room = max(0.0, max_exposure - current_exposure)

        if remaining_room <= 0:
            logger.warning(
                "open_bet_on_market: %s rejected — exposure full $%.2f/$%.2f",
                market.id, current_exposure, max_exposure,
            )
            return None

        # Bet boyutu kalan room'dan buyuk olamaz
        if amount > remaining_room:
            logger.info(
                "open_bet_on_market: %s amount capped $%.2f -> $%.2f (remaining room)",
                market.id, amount, remaining_room,
            )
            amount = remaining_room

        # Bet boyutu nakit bakiyesinden buyuk olamaz
        pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
        cash_balance = float(pf.cash_balance) if pf else 0.0
        if amount > cash_balance:
            logger.info(
                "open_bet_on_market: %s amount capped $%.2f -> $%.2f (cash balance)",
                market.id, amount, cash_balance,
            )
            amount = cash_balance

        if amount <= 0:
            logger.warning(
                "open_bet_on_market: %s rejected — amount $%.2f <= 0",
                market.id, amount,
            )
            return None

        # Fill price + slippage
        raw_fill = float(market.yes_price or 0.5)

        # Vadesi gecmis piyasalara bet acma
        if market.target_date:
            _now = datetime.now(timezone.utc).replace(tzinfo=None)
            if market.target_date <= _now:
                logger.info(
                    "open_bet_on_market: %s target=%s GECTI - skipped",
                    market.id,
                    market.target_date,
                )
                return None

        # YES price gate: [0.10, 0.95).
        _min_entry = float(getattr(bot_config.strategy, "min_entry_price", 0.10))
        _max_entry = float(getattr(bot_config.strategy, "max_entry_price", 0.95))
        if not (_min_entry <= raw_fill < _max_entry):
            logger.info(
                "open_bet_on_market: %s yes_price=%.3f outside [%.2f, %.2f) - skipped",
                market.id,
                raw_fill,
                _min_entry,
                _max_entry,
            )
            return None

        condition_id = None
        token_id = None
        try:
            raw = json.loads(market.raw_data) if market.raw_data else {}
            for tok in raw.get("tokens", []):
                if tok.get("outcome", "").upper() == "YES":
                    condition_id = tok.get("condition_id") or tok.get("token_id")
                    token_id = tok.get("token_id") or condition_id
                    break
        except (json.JSONDecodeError, TypeError):
            pass

        slip_est = estimate_slippage(raw_fill, stake_usd=amount, condition_id=condition_id, token_id=token_id)
        fill_price = raw_fill * (1.0 + slip_est.slippage_pct)
        fill_price = max(0.01, min(0.99, round(fill_price, 4)))
        shares = bet_shares(amount, fill_price)

        fee_rate = bot_config.strategy.current_fee_rate
        entry_fee = polymarket_fee_from_stake(amount, fill_price, fee_rate)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ts = int(now.timestamp())

        bet = Bet(
            market_id=str(market.id),
            city=market.city,
            city_code=market.city_code or "",
            side="YES",
            amount=amount,
            stake_amount=amount,
            price=fill_price,
            entry_price=fill_price,
            shares=shares,
            current_price=fill_price,
            status="placed",
            order_id=f"paper_{market.id}_{ts}",
            placed_at=now,
            entry_fee=round(entry_fee, 4),
            strike_temp=float(market.threshold or 0.0),
            potential_payout=amount / fill_price if fill_price > 0 else 0,
            fair_value=raw_fill,
        )

        # Stake dus
        from utils.accounting import debit_stake

        try:
            debit_stake(session, amount, f"bet_open:{market.id}")
            if entry_fee > 0:
                debit_stake(session, entry_fee, f"bet_fee:{market.id}")
        except ValueError as e:
            logger.warning("open_bet_on_market debit failed for %s: %s", market.id, e)
            return None

        # Market status guncelle
        wm = session.query(WeatherMarket).filter(WeatherMarket.id == market.id).first()
        if wm:
            wm.status = "bet_placed"

        # Portfolio guncelle
        pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
        if pf:
            open_amt = session.query(Bet.amount).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
            open_exposure = sum(float(a[0] or 0) for a in open_amt)
            pf.total_value = portfolio_total_value(float(pf.cash_balance or 0), open_exposure)
            pf.last_updated = now

        session.add(bet)
        session.commit()

        logger.info(
            "Bet opened: %s %s %s YES $%.2f @ %.3f (shares=%.2f)",
            market.city,
            str(market.target_date.date() if market.target_date else "?"),
            market.metric,
            amount,
            fill_price,
            shares,
        )
        return bet

    def close_bet_for_rotation(self, bet: Bet, current_price: float, session) -> bool:
        """Bet'i rotation icin kapat. Hisseyi current_price'den sat, portfolio'ya kredi yaz."""
        from utils.accounting import credit_sale

        shares = float(bet.shares or 0)
        sell_proceeds = shares * current_price
        entry_cost = float(bet.amount or 0) + float(bet.entry_fee or 0)
        pnl = sell_proceeds - entry_cost

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        bet.status = "closed"
        bet.close_reason = "rotation"
        bet.closed_at = now
        bet.realized_pnl = round(pnl, 4)

        # Krediyi portfolio'ya yaz
        credit_sale(session, sell_proceeds, f"rotation_close:{bet.market_id}")

        # Portfolio guncelle
        pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
        if pf:
            open_amt = session.query(Bet.amount).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
            open_exposure = sum(float(a[0] or 0) for a in open_amt)
            pf.total_value = portfolio_total_value(float(pf.cash_balance or 0), open_exposure)
            pf.last_updated = now

        # Market statusu geri ac
        wm = session.query(WeatherMarket).filter(WeatherMarket.id == bet.market_id).first()
        if wm:
            wm.status = "open"

        session.commit()

        logger.info(
            "Bet closed (rotation): %s %s $%.2f -> $%.2f (pnl=$%.2f)",
            bet.city,
            bet.market_id,
            entry_cost,
            sell_proceeds,
            pnl,
        )
        return True

    def exit_position(
        self,
        market: object,
        side: str,
        price: float,
        size: float,
        reason: str,
    ) -> dict:
        """Sell an existing position (paper or live).

        In dry-run mode this books a paper sell; in live mode it submits a
        real SELL order to the Polymarket CLOB client.
        """
        if Config.DRY_RUN or not self.ready:
            import uuid

            return {
                "paper": True,
                "orderID": f"paper_sell_{uuid.uuid4().hex[:12]}",
                "side": side,
                "size": size,
                "price": price,
                "reason": reason,
            }

        from py_clob_client.order_builder.constants import SELL

        payload = {
            "side": SELL,
            "size": size,
            "price": price,
            "token_id": self._get_token_id(market, side),
        }
        return self.client.create_and_post_order(payload)
