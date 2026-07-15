"""Background bot loops: scan-and-bet, settlement, stale cleanup.

ASIAbot ile aynı yapıda: asyncio.gather + semaphore + timeout.
"""

import asyncio
import logging
from datetime import datetime, timezone

from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, WeatherMarket

logger = logging.getLogger("BOT_LOOP")

# Timeout values (seconds)
_FETCH_TIMEOUT = 120
_CYCLE_TIMEOUT = 300
_CLEANUP_TIMEOUT = 60


def _is_midnight_window(now: datetime) -> bool:
    from config.settings import bot_config
    window_minutes = bot_config.midnight_scan_window
    return now.hour == 0 and now.minute < window_minutes


def _get_scan_interval(now: datetime) -> int:
    from config.settings import bot_config
    if _is_midnight_window(now):
        return bot_config.midnight_scan_interval
    return bot_config.scan_interval


async def scan_and_bet_loop(state):
    """Scan loop - ASIAbot ile aynı yapıda.

    1. fetch_markets (senkron)
    2. parse + weather (asyncio.gather ile paralel)
    3. run_cycle (senkron)
    """
    from jobs.scheduler import (
        run_cycle,
        run_fetch_markets,
        run_fetch_weather,
        run_parse_markets,
    )

    stale_check_counter = 0
    last_day = None

    while state.is_running:
        state.last_scan = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today = now.date()

            is_new_day = last_day is not None and today != last_day
            last_day = today

            if is_new_day:
                logger.info("Midnight detected — running immediate scan")

            # STEP 1: Fetch markets (senkron)
            await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)

            # STEP 2: Parse + Weather PARALEL
            parse_and_weather = await asyncio.gather(
                asyncio.wait_for(asyncio.to_thread(run_parse_markets), timeout=_FETCH_TIMEOUT),
                asyncio.wait_for(asyncio.to_thread(run_fetch_weather), timeout=_FETCH_TIMEOUT),
                return_exceptions=True,
            )
            for result in parse_and_weather:
                if isinstance(result, Exception):
                    logger.error("Parallel step error: %s", result)

            # STEP 3: Run cycle (senkron)
            await asyncio.wait_for(asyncio.to_thread(run_cycle), timeout=_CYCLE_TIMEOUT)

            # Stale cleanup her 10 döngüde
            stale_check_counter += 1
            if stale_check_counter >= 10:
                stale_check_counter = 0
                try:
                    await asyncio.wait_for(asyncio.to_thread(_cleanup_stale_bets), timeout=_CLEANUP_TIMEOUT)
                except Exception as e:
                    logger.warning("Stale cleanup failed: %s", e)

        except asyncio.TimeoutError:
            logger.error("Scan step timed out - recovering")
        except Exception as e:
            logger.error("Scan error: %s", e)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        interval = _get_scan_interval(now)
        await asyncio.sleep(interval)


async def settlement_loop(state):
    """Settlement loop."""
    from jobs.scheduler import run_settle

    last_cleanup_date = None

    while state.is_running:
        try:
            await asyncio.to_thread(run_settle)

            today = datetime.now(timezone.utc).date()
            if last_cleanup_date != today:
                from database.db_cleanup import auto_cleanup
                await asyncio.to_thread(auto_cleanup, hot_days=10, cold_days=120)
                last_cleanup_date = today

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


def _cleanup_stale_bets():
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
            should_cancel = False
            if not market:
                should_cancel = True
            elif market.target_date and (now - market.target_date).total_seconds() > 48 * 3600:
                should_cancel = True

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
