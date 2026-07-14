"""Background bot loops: scan-and-bet, settlement, stale cleanup.

Extracted from main.py to reduce file size and separate concerns.
"""

import asyncio
import logging
from datetime import datetime, timezone

from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, WeatherMarket

logger = logging.getLogger("BOT_LOOP")


def _is_midnight_window(now: datetime) -> bool:
    """Check if *now* is within the midnight fast-scan window (00:00 .. N minutes)."""
    from config.settings import bot_config

    window_minutes = bot_config.midnight_scan_window
    return now.hour == 0 and now.minute < window_minutes


def _get_scan_interval(now: datetime) -> int:
    """Return scan interval: fast during midnight window, normal otherwise."""
    from config.settings import bot_config

    if _is_midnight_window(now):
        return bot_config.midnight_scan_interval
    return bot_config.scan_interval


async def scan_and_bet_loop(state):
    """Background loop: fetch, parse, forecast, then a single-cycle DB session for analyze/bet/update/risk.

    Midnight strategy:
    - After 00:00, use a shorter scan interval (midnight_scan_interval)
      for the first midnight_scan_window minutes to catch 2-day-ahead
      markets as early as possible (earlier = cheaper Polymarket prices).
    - The first cycle after midnight runs immediately (no initial sleep).
    """
    from jobs.scheduler import (
        run_cycle,
        run_fetch_markets,
        run_fetch_weather,
        run_parse_markets,
    )

    stale_check_counter = 0
    last_day = None  # Track day changes for immediate midnight scan

    while state.is_running:
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today = now.date()

            # Midnight detection: if day changed, run immediately
            # (skip initial sleep on first cycle after midnight)
            is_new_day = last_day is not None and today != last_day
            last_day = today

            if is_new_day:
                logger.info("Midnight detected — running immediate scan for 2-day-ahead markets")

            # Data fetching (each with its own session — I/O bound, no shared state needed)
            await asyncio.to_thread(run_fetch_markets)
            await asyncio.to_thread(run_parse_markets)
            await asyncio.to_thread(run_fetch_weather)
            # Core DB operations — single shared session for consistency
            await asyncio.to_thread(run_cycle)

            # Her 10 döngüde bir stale bet temizliği
            stale_check_counter += 1
            if stale_check_counter >= 10:
                stale_check_counter = 0
                await asyncio.to_thread(_cleanup_stale_bets)
        except Exception as e:
            logger.error("Scan error: %s", e)
        state.last_scan = datetime.now(timezone.utc).replace(tzinfo=None)

        # Dynamic interval: fast during midnight window, normal otherwise
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        interval = _get_scan_interval(now)
        if _is_midnight_window(now):
            logger.debug("Midnight window active — scanning every %ds", interval)
        await asyncio.sleep(interval)


def _cleanup_stale_bets():
    """Cancel open bets whose target_date has passed by >48h and market is unresolvable."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as session:
        stale = (
            session.query(Bet)
            .filter(
                Bet.status.in_(OPEN_BET_STATUSES),
                Bet.placed_at < cutoff,
            )
            .all()
        )
        cancelled = 0
        for bet in stale:
            market = session.query(WeatherMarket).filter(WeatherMarket.id == bet.market_id).first()
            # Only cancel if:
            # 1. Market doesn't exist (test market), OR
            # 2. target_date + 48h has passed and market still not resolved
            should_cancel = False
            if not market:
                should_cancel = True  # Test market — can never resolve
            elif market.target_date and (now - market.target_date).total_seconds() > 48 * 3600:
                should_cancel = True  # Too old, force cancel

            if should_cancel:
                from utils.accounting import credit_sale

                bet.status = "cancelled"
                bet.settled_at = now
                bet.close_reason = "stale_cleanup"
                amount = float(bet.amount or 0)
                if amount > 0:
                    credit_sale(session, amount, f"stale_cleanup:bet_{bet.id}")
                cancelled += 1

        if cancelled > 0:
            session.commit()
            logger.info("Stale cleanup: cancelled %d old bets", cancelled)


async def settlement_loop(state):
    """Background loop: run SIA optimization (hourly) and settle resolved bets."""
    from jobs.scheduler import run_settle

    last_cleanup_date = None

    while state.is_running:
        try:
            await asyncio.to_thread(run_settle)

            # Daily DB cleanup: archive old forecasts, VACUUM
            today = datetime.now(timezone.utc).date()
            if last_cleanup_date != today:
                from database.db_cleanup import auto_cleanup

                await asyncio.to_thread(auto_cleanup, hot_days=10, cold_days=120)
                last_cleanup_date = today

            # SIA optimization: hourly
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if state.sia_loop is not None and (
                state.sia_last_run is None
                or (now - state.sia_last_run).total_seconds() >= state.sia_interval_hours * 3600
            ):
                await asyncio.to_thread(state.sia_loop.run_optimization_cycle)
                state.sia_last_run = datetime.now(timezone.utc).replace(tzinfo=None)
        except Exception as e:
            logger.error("Settle error: %s", e)
        await asyncio.sleep(state.config.SETTLEMENT_INTERVAL)
