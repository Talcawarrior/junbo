"""Independent scheduled job executors."""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from database.db import get_session, get_session_or
from database.models import (
    OPEN_BET_STATUSES,
    Analysis,
    Bet,
    Portfolio,
    WeatherForecast,
    WeatherMarket,
)
from utils.formulas import (
    portfolio_total_value,
    unrealized_pnl as compute_unrealized_pnl,
)

logger = logging.getLogger("JOBS_SCHEDULER")


# --- Re-analysis throttle ---------------------------------------------------
# `analyze_market` is, for our purposes, a pure function of its inputs: the
# cached hourly weather forecast, the live market price, slippage, the
# time-to-close (days_ahead) and the number of forecast sources. If none of
# those changed since the last analysis, the output (edge / should_bet /
# side) is identical, so re-running it is pure wasted work.
#
# We therefore SKIP re-analyzing a market only when ALL of these hold:
#   - it was analyzed recently (within MAX_ANALYSIS_AGE_MIN), AND
#   - no new weather forecast has arrived since the last analysis, AND
#   - its price has not moved more than PRICE_REANALYZE_DELTA.
# Any of those being false forces a fresh analysis. This cannot drop bet
# quality: an unchanged input set yields an unchanged decision, and every
# path that could change the decision (new weather, a real price move, or
# the time-to-close window opening) still triggers a re-analysis.
PRICE_REANALYZE_DELTA = 0.005  # 0.5% price move forces re-analysis (well below min_edge 1%)
MAX_ANALYSIS_AGE_MIN = 30  # never go longer than this without re-analyzing


def _should_skip_analysis(sess, market, now):
    """Return True if re-analyzing `market` this cycle is safe to skip.

    Safe to skip only when the analysis inputs are unchanged since the last
    analysis (see PRICE_REANALYZE_DELTA / MAX_ANALYSIS_AGE_MIN above).
    """
    last = sess.query(Analysis).filter(Analysis.market_id == market.id).order_by(Analysis.analyzed_at.desc()).first()
    if last is None:
        return False  # never analyzed yet -> must analyze
    # Analysis.analyzed_at is stored tz-aware (UTC). `now` is tz-naive UTC,
    # so strip tzinfo before subtracting to avoid a naive/aware TypeError.
    last_at = last.analyzed_at
    if last_at.tzinfo is not None:
        last_at = last_at.replace(tzinfo=None)
    if (now - last_at) >= timedelta(minutes=MAX_ANALYSIS_AGE_MIN):
        return False  # too old -> refresh
    # New weather since the last analysis?
    new_weather = (
        sess.query(WeatherForecast)
        .filter(
            WeatherForecast.market_id == market.id,
            WeatherForecast.metric == market.metric,
            WeatherForecast.fetched_at > last.analyzed_at,
        )
        .first()
    )
    if new_weather is not None:
        return False  # fresh forecast -> re-analyze
    # Price moved enough to matter?
    last_price = last.market_implied_prob if last.market_implied_prob is not None else 0.5
    cur_price = market.yes_price if market.yes_price is not None else 0.5
    if abs(cur_price - last_price) >= PRICE_REANALYZE_DELTA:
        return False  # price moved -> re-analyze
    return True  # inputs unchanged -> safe to skip


def run_fetch_markets():
    """Fetch markets from Polymarket and save to raw weather_markets."""
    from scrapers.polymarket import PolymarketScraper

    scraper = PolymarketScraper()
    count = scraper.fetch_and_save()
    # 2026-08-19 AKTIVITE: gunde 1 kez poly vs db market/sehir sayisi
    # (helper idempotent — ayni gun tekrar yazmaz).
    try:
        from utils.activity_log import log_daily_market_summary

        log_daily_market_summary()
    except Exception:  # noqa: BLE001
        pass
    return f"{count} market cekildi ve kaydedildi"


def run_parse_markets():
    """Parse raw weather_markets to extract structured fields."""
    from engine.market_parser import MarketParser

    parser = MarketParser()
    count = parser.parse_all_unparsed()
    return f"{count} market parse edildi"


def run_fetch_weather():
    """Fetch forecast values for parsed weather_markets."""
    from scrapers.meteo import MeteoFetcher

    fetcher = MeteoFetcher()
    count = fetcher.fetch_all_markets()
    return f"{count} hava tahmini cekildi ve kaydedildi"


def run_analyze(session=None):
    """Run forecast analyses for open markets. Optional session for batched cycles.

    Paralel analiz: 4 worker ile ayni anda 4 market analiz edilir.
    Hesaplamalar birebir aynidir, sadece hizlanir.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from engine.calculator import Calculator

    analyzed = 0
    errors = 0

    with get_session_or(session) as sess:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Only analyze markets that actually have at least one matching-metric
        # forecast. Newly fetched markets (or ones whose open-meteo forecast
        # fetch timed out this cycle) have no forecasts written yet; analyzing
        # them produces a misleading "Az kaynak: 0" PASS row in the health feed
        # even though the data simply has not landed. Skipping them here means
        # they wait for the next cycle, when the forecast arrives and the
        # re-analysis gate (_should_skip_analysis -> new_weather) picks them up.
        has_matching_forecast = (
            sess.query(WeatherForecast.id)
            .filter(
                WeatherForecast.market_id == WeatherMarket.id,
                WeatherForecast.metric == WeatherMarket.metric,
            )
            .exists()
        )
        markets = (
            sess.query(WeatherMarket)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.city.isnot(None),
                WeatherMarket.target_date > now,
                has_matching_forecast,
            )
            .all()
        )
        market_ids = [m.id for m in markets if not _should_skip_analysis(sess, m, now)]
        skipped = len(markets) - len(market_ids)

    def analyze_single(mid):
        """Tek bir marketi analiz et (her thread kendi session'unu olusturur)."""
        try:
            calc = Calculator()
            result = calc.analyze_market(mid)  # Session yok → kendi session'unu olusturur
            return (mid, result, None)
        except Exception as e:
            return (mid, None, str(e))

    # Paralel analiz: 4 worker
    max_workers = min(4, len(market_ids)) if market_ids else 1
    logger.info(
        "Starting parallel analysis: %d to analyze, %d skipped (unchanged inputs), %d workers",
        len(market_ids),
        skipped,
        max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(analyze_single, mid): mid for mid in market_ids}
        for future in as_completed(futures):
            mid, result, error = future.result()
            if result is not None:
                analyzed += 1
            elif error:
                logger.error("Analysis error %s: %s", mid, error)
                errors += 1

    logger.info(
        "Parallel analysis complete: %d analyzed, %d errors, %d skipped, %d total",
        analyzed,
        errors,
        skipped,
        len(markets),
    )
    return f"{analyzed} market analiz edildi ({len(market_ids)} analiz edilen, {skipped} atlandi, {errors} hata)"


def run_place_bets():
    """Execute betting strategy and place live/paper bets."""
    from executor.bet_placer import BetPlacer

    placer = BetPlacer()
    count = placer.place_all_pending()
    return f"{count} adet yeni bet acildi"


def run_update_prices(session=None):
    """
    Refresh `current_price` and update `unrealized_pnl`
    on every open bet. Updates Portfolio.total_value at the end.
    Optional session for batched cycles.
    """
    open_statuses = OPEN_BET_STATUSES
    updated = 0
    with get_session_or(session) as sess:
        bets = sess.query(Bet).filter(Bet.status.in_(open_statuses)).all()

        # Pre-fetch price map: market_id -> prices
        market_ids = list(set(b.market_id for b in bets if b.market_id))
        price_map = {}
        if market_ids:
            markets = sess.query(WeatherMarket).filter(WeatherMarket.id.in_(market_ids)).all()
            for m in markets:
                price_map[m.id] = {
                    "yes": float(m.yes_price) if m.yes_price is not None else 0.5,
                    "no": float(m.no_price) if m.no_price is not None else 0.5,
                }

        total_unrealized = 0.0

        for bet in bets:
            if bet.market_id not in price_map:
                continue

            prices = price_map[bet.market_id]

            # current_price from market
            if bet.side and bet.side.upper() == "NO":
                current = max(0.0, min(1.0, 1.0 - prices["yes"]))
            else:
                current = max(0.0, min(1.0, prices["yes"]))

            entry = float(bet.entry_price or bet.price or 0.0)
            shares = float(bet.shares or 0.0)

            bet.current_price = current

            # 1. unrealized_pnl — entry fee dahil (gercek maliyet)
            # current_price is already in side terms (YES=yes_price, NO=no_price)
            # so the same (current - entry) * shares formula works for both sides.
            entry_fee = float(bet.entry_fee or 0.0)
            bet.unrealized_pnl = round(compute_unrealized_pnl(shares, current, entry) - entry_fee, 2)

            total_unrealized += bet.unrealized_pnl or 0.0

            updated += 1
            sess.add(bet)

        # 3. Portfolio: conservative current = cash + open_exposure
        # Unrealized PnL is paper money — don't bake it into total_value.
        portfolio = sess.query(Portfolio).filter(Portfolio.id == 1).first()
        if portfolio:
            realized_pnl_total = (
                sess.query(func.coalesce(func.sum(Bet.pnl), 0.0))
                .filter(Bet.status.in_(("won", "lost", "settled", "closed_early")))
                .scalar()
            ) or 0.0
            open_exposure = (
                sess.query(func.coalesce(func.sum(Bet.amount), 0.0)).filter(Bet.status.in_(OPEN_BET_STATUSES)).scalar()
            ) or 0.0
            # Conservative: cash + money locked in bets
            if portfolio.cash_balance is not None:
                cash = float(portfolio.cash_balance)
            else:
                cash = (portfolio.initial_value or 1000.0) + float(realized_pnl_total)
            portfolio.total_value = portfolio_total_value(cash, float(open_exposure))
            # current_value = mark-to-market: book value + unrealized (paper) PnL.
            # Distinct from total_value (book value, excludes paper PnL) so the
            # dashboard can show both the conservative book and the live value.
            portfolio.current_value = round(portfolio.total_value + float(total_unrealized), 2)
            portfolio.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            sess.add(portfolio)

        sess.commit()
    return f"{updated} acik bet guncellendi, total_unrealized={total_unrealized:.2f}"


def run_refresh_open_prices():
    """Refresh ``yes_price``/``no_price`` for markets we still hold open bets in.

    The main market fetch (``run_fetch_markets``) goes through Polymarket's
    relevance-ranked ``public-search``, which stops returning a market once it
    ends/expires.  Markets we still hold positions in therefore freeze at their
    last-fetched price even though the live ``outcomePrices`` have already moved
    to the resolved value — so the dashboard and PnL keep showing a stale,
    mid-range price instead of the clear ≥0.98 winner.

    This refreshes exactly those markets from the direct Gamma ``/markets/{id}``
    endpoint every poller cycle.  Only the price fields are touched — never
    ``status`` — so settled markets cannot be resurrected.
    """
    from executor.settler import SettlementEngine

    open_statuses = OPEN_BET_STATUSES
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    refreshed = 0
    with get_session() as session:
        market_ids = [
            r[0]
            for r in session.query(Bet.market_id)
            .filter(Bet.status.in_(open_statuses), Bet.market_id.isnot(None))
            .distinct()
        ]
        if not market_ids:
            return "0 market fiyati tazelendi (acik bet yok)"
        markets = session.query(WeatherMarket).filter(WeatherMarket.id.in_(market_ids)).all()
        engine = SettlementEngine()
        for m in markets:
            try:
                data = engine._call_gamma_api(m)
            except Exception as e:  # noqa: BLE001
                logger.warning("Price refresh failed for %s: %s", m.id, e)
                continue
            if not data:
                continue
            raw = data.get("outcomePrices")
            if not raw:
                continue
            try:
                prices = json.loads(raw) if isinstance(raw, str) else raw
                yes = float(prices[0])
                no = float(prices[1])
            except (TypeError, ValueError, json.JSONDecodeError, IndexError):
                continue
            m.yes_price = yes
            m.no_price = no
            m.last_updated = now
            refreshed += 1
            session.add(m)
    return f"{refreshed} market fiyati tazelendi"


def run_settle():
    """Settle resolved bets against actual weather data."""
    from executor.settler import SettlementEngine

    engine = SettlementEngine()
    results = engine.settle_all()
    return f"Sonuclandirilan -> Kazanan:{results['win']}, Kaybeden:{results['loss']}, Bekleyen:{results['pending']}"


def run_report():
    """Print daily consolidated PnL and trade report."""
    with get_session() as session:
        total_bets = session.query(Bet).count()
        won = session.query(Bet).filter(Bet.status == "won").count()
        lost = session.query(Bet).filter(Bet.status == "lost").count()
        open_markets = session.query(WeatherMarket).filter(WeatherMarket.status == "open").count()

        total_pnl = session.query(func.sum(Bet.pnl)).scalar() or 0.0

        report = (
            f"\n📊 GUNLUK CONSOLIDATED RAPOR\n"
            f"  Acik Marketler: {open_markets}\n"
            f"  Toplam Bahis: {total_bets}\n"
            f"  Kazanilan: {won} | Kaybedilen: {lost}\n"
            f"  Net PnL: ${total_pnl:+.2f}\n"
        )
        logger.info(report)
        return report


def run_cycle():
    """Run one full bot cycle with a SINGLE shared DB session.

    Combines analyze → place_bets → update_prices → risk_management
    into one session scope so all operations see consistent state and
    commit atomically at the end.
    """
    results = []
    with get_session() as session:
        try:
            results.append(run_analyze(session=session))
        except Exception as e:
            logger.error("Cycle analyze error: %s", e)
            results.append(f"analyze error: {e}")

        try:
            # M5: run_place_bets intentionally manages its own DB session
            # for bet placement atomicity — does NOT share the cycle session
            results.append(run_place_bets())
        except Exception as e:
            logger.error("Cycle place_bets error: %s", e)
            results.append(f"place_bets error: {e}")

        try:
            results.append(run_update_prices(session=session))
        except Exception as e:
            logger.error("Cycle update_prices error: %s", e)
            results.append(f"update_prices error: {e}")

        # Commit all changes atomically at end of cycle.
        # Individual run_* functions that used the shared session
        # skip their own commit (get_session_or doesn't auto-commit
        # when given an existing session).
        session.commit()

    return " | ".join(results)
