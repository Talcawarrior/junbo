"""Settlement engine: resolves bets via Polymarket Gamma API resolution."""

# pylint: disable=import-error,broad-exception-caught

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests  # pylint: disable=import-error

from sqlalchemy import func

from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, Portfolio, WeatherMarket
from engine.strategy import RiskManager
from utils.db_paths import get_data_dir
from utils.formulas import portfolio_total_value, settlement_payout, settlement_pnl

logger = logging.getLogger("EXECUTOR_SETTLER")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Gamma istek deneme sayisi (403/429 ve baglanti hatalari icin).
_GAMMA_MAX_ATTEMPTS = 3


class SettlementEngine:
    """Resolves open bets by reading Polymarket's official resolution data.

    Market resolution is fetched from the Gamma API (the same source the
    Polymarket UI uses).  Bets are settled only when the API indicates
    ``closed == true``, ``umaResolutionStatus == "resolved"``, and valid
    ``outcomePrices`` are available.
    """

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

            # 2026-10-22: Sadece ACIK BETI OLAN marketler Gamma'ya sorgulanir.
            # Onceki davranista her turda hedef tarihi gecmis TUM marketler
            # (~945 adet) isleniyordu. Beti olmayan marketler Gamma'ya hic
            # sorulmuyordu ama her biri "pending" sayilip 48h hata logu
            # basiyordu (24k+ log satiri tamamen gurultu). Artik beti olmayan
            # marketler toplu olarak 'expired' isaretlenir ve akistan cikar.
            market_ids_with_bets = {
                row[0]
                for row in session.query(Bet.market_id).filter(Bet.status.in_(OPEN_BET_STATUSES)).distinct().all()
                if row[0]
            }
            markets_with_bets = [m for m in markets_to_settle if m.id in market_ids_with_bets]

            # Beti olmayan marketler: kapanis (target_date + 12h) gectiyse expired.
            # Acik beti olan marketler ASLA expired yapilmaz.
            expired_ids = [
                m.id
                for m in markets_to_settle
                if m.id not in market_ids_with_bets
                and m.target_date
                and (m.target_date + timedelta(hours=12)) <= now_naive
            ]
            if expired_ids:
                session.query(WeatherMarket).filter(WeatherMarket.id.in_(expired_ids)).update(
                    {WeatherMarket.status: "expired"},
                    synchronize_session=False,
                )
                logger.info("Settlement: %d market expired (acik beti yok)", len(expired_ids))

            for market in markets_with_bets:
                try:
                    result = self._settle_market(session, market, risk_manager)
                    if result is None:
                        pending_count += 1
                        if market.target_date and (now_naive - market.target_date) > timedelta(hours=48):
                            logger.error(
                                "Market %s has been pending >48h (target=%s). Gamma API may not have resolved it yet.",
                                market.id,
                                market.target_date,
                            )
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
                    # current_value = mark-to-market: book value + unrealized
                    # (paper) PnL on remaining open bets.
                    open_unrealized = (
                        sync_session.query(func.coalesce(func.sum(Bet.unrealized_pnl), 0.0))
                        .filter(Bet.status.in_(OPEN_BET_STATUSES))
                        .scalar()
                    ) or 0.0
                    portfolio.current_value = round(portfolio.total_value + float(open_unrealized), 2)
                    portfolio.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)  # pyright: ignore
                    sync_session.commit()

        logger.info(
            "Settlement complete: %s won, %s lost, %s pending, total_pnl=%.2f",
            won_count,
            lost_count,
            pending_count,
            total_pnl,
        )
        # 2026-08-19 AKTIVITE: settlement ozeti akista (bet kapanislari).
        try:
            from utils.activity_log import log_event

            log_event(
                "daily_summary",
                None,
                f"settlement: {won_count} kazandi / {lost_count} kaybetti / {pending_count} bekleyen, "
                f"PNL=${total_pnl:+.2f}",
            )
        except Exception:  # noqa: BLE001
            pass
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
            # Kapanis gecmeden expired isaretlenmez; Gamma cozumlemesi yine
            # ayni dongude denenir.
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            if market.target_date:
                _close = market.target_date + timedelta(hours=12)  # 24:00 UTC kapanis
                if _close > now_naive:
                    return None  # market hala canli, dokunma (pending gibi)
            market.status = "expired"
            return None

        # ── Fetch resolution from Gamma API ────────────────────────────────
        outcome = self._fetch_market_resolution(market)
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

            # Poly settlement outcome: gercek sicakligi kaydet
            try:
                # 2026-10-27: gercek sicaklik MARKETIN gunune aittir, yerlesim
                # anina degil. UMA gec cozerse (D+1, D+2) onceki halde yanlis
                # gunun sicakligi yaziliyordu. target_date yoksa settled_at'e dus.
                temp_date = getattr(market, "target_date", None) or bet.settled_at
                actual_temp = self._fetch_actual_temp(bet.city, temp_date)
                outcome_info = {
                    "settlement_temp_c": actual_temp,
                    "settlement_temp_f": actual_temp * 9 / 5 + 32 if actual_temp else None,
                    "source": "wu_weather_underground",
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
                if actual_temp:
                    bet.outcome = json.dumps(outcome_info)
            except Exception:
                pass

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
                    credit_settlement(session, payout, 0.0, f"settle:{bet.market_id}:won")
                    portfolio.total_won = (portfolio.total_won or 0) + 1
                else:
                    portfolio.total_lost = (portfolio.total_lost or 0) + 1
                portfolio.total_realized_pnl = (portfolio.total_realized_pnl or 0) + realized_pnl

        if any_settled:
            market.status = "settled_win" if outcome == "YES" else "settled_loss"
            # raw_data populated inside _fetch_market_resolution

        return {"won": bet_won_count, "lost": bet_lost_count, "pnl": total_market_pnl}

    # ── Gercek sicaklik toplama ──────────────────────────────────────────────

    def _fetch_actual_temp(self, city: str, settled_at: datetime) -> float | None:
        """Poly settlement'ta kullanilan gercek sicakligi actuals.db'den ceker."""
        try:
            date_str = settled_at.strftime("%Y-%m-%d") if settled_at else None
            if not date_str or not city:
                return None
            import sqlite3 as _sqlite3

            conn = _sqlite3.connect(os.path.join(get_data_dir(), "actuals.db"))
            cur = conn.cursor()
            cur.execute(
                "SELECT temperature_2m_max FROM actual_temperatures WHERE city = ? AND date = ?",
                (city, date_str),
            )
            row = cur.fetchone()
            conn.close()
            return float(row[0]) if row and row[0] else None
        except Exception:
            return None

    # ── Gamma API resolution ───────────────────────────────────────────────

    def _fetch_market_resolution(self, market, force: bool = False) -> str | None:
        """Fetch market resolution from Polymarket Gamma API.

        Returns ``"YES"``, ``"NO"``, or ``None`` if not yet resolved.

        Only settles when Polymarket has OFFICIALLY resolved the market:
        ``closed == true`` AND ``umaResolutionStatus == "resolved"``.

        Price-based settlement (outcomePrices >= 0.98) is DISABLED —
        the market price reaching 0.98 does not mean the event is resolved.
        We wait for UMA's official resolution to avoid premature settlement.
        """
        data = self._call_gamma_api(market)
        if data is None:
            return None

        officially_resolved = bool(data.get("closed")) and data.get("umaResolutionStatus") == "resolved"
        if not officially_resolved:
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

        if yes_price >= 0.5:
            outcome = "YES"
        elif no_price >= 0.5:
            outcome = "NO"
        else:
            logger.warning(
                "Ambiguous resolution for %s: outcomePrices=%s (neither side >= 0.50)",
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
                "closed": data.get("closed"),
                "settled_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return outcome

    def _call_gamma_api(self, market) -> dict | None:
        """Make GET request to Gamma API and return parsed JSON.

        2026-10-22: User-Agent ekleniyor. Scraper 'Junbo/1.0' UA gonderiyor
        ve sorunsuz calisiyor; settler ise basliksiz istek gonderiyordu ve
        Cloudflare bazen 403 Forbidden donuyordu (log: 2127 adet). Ayrica
        403/429 yanitlarinda kisa bir bekleme + tekrar deneme yapilir.
        """
        # Rate limit korumasi (2026-08-16): Cloudflare 10054 ile baglanti
        # sifirliyordu (saniyede ~10 istek -> IP gecici blok). Istekler arasi
        # kisa bekleme + basarisizlikta daha uzun bekleme eklenir.
        import time as _time

        url = f"{GAMMA_API_BASE}/markets/{market.id}"
        from config.settings import bot_config

        headers = {
            "User-Agent": "Junbo/1.0 (+tier3-12)",
            "Accept": "application/json",
        }
        proxies = bot_config.polymarket.get_proxies()

        for attempt in range(1, _GAMMA_MAX_ATTEMPTS + 1):
            _time.sleep(0.25)
            try:
                resp = requests.get(url, headers=headers, timeout=15, proxies=proxies)
                if resp.status_code in (403, 429):
                    # Cloudflare bot korumasi / rate limit: daha uzun bekle.
                    retry_after = resp.headers.get("Retry-After") if resp.headers else None
                    wait = 2.0 * attempt
                    if retry_after and retry_after.strip().isdigit():
                        wait = float(retry_after)
                    logger.warning(
                        "Gamma API %s for %s, retry in %.1fs (attempt %d/%d)",
                        resp.status_code,
                        market.id,
                        wait,
                        attempt,
                        _GAMMA_MAX_ATTEMPTS,
                    )
                    _time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                logger.warning(
                    "Gamma API request failed for %s: %s",
                    market.id,
                    e,
                )
                _time.sleep(1.0 * attempt)  # basarisizlikta Cloudflare'e nefes payi
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
