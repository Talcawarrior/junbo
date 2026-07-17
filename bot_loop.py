"""Background bot loops: scan-and-bet, settlement, stale cleanup.

ASYNCIO safety: Each loop has a SINGLE try/except wrapping the entire body
so that no exception can silently kill the loop without logging.

Watchdog: settlement_loop monitors scan_loop health via state.last_scan.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, WeatherMarket

logger = logging.getLogger("BOT_LOOP")

# Timeout values (seconds)
_FETCH_TIMEOUT = 180
_CYCLE_TIMEOUT = 600
_CLEANUP_TIMEOUT = 60

# Akıllı tarama ayarları
_FAST_MODE_MINUTES = 30
_FAST_SCAN_INTERVAL = 60
_NORMAL_SCAN_INTERVAL = 900

# Watchdog thresholds (seconds)
_WATCHDOG_WARNING = 900    # 15 dakika — warning
_WATCHDOG_DEAD = 1800      # 30 dakika — dead
_WATCHDOG_RESTART = 3600   # 1 saat — restart


def _get_market_count() -> int:
    with get_session() as db:
        return db.query(WeatherMarket).filter(WeatherMarket.status == "open").count()


def _is_midnight_window(now: datetime) -> bool:
    from config.settings import bot_config
    window_minutes = bot_config.midnight_scan_window
    return now.hour == 0 and now.minute < window_minutes


def _get_scan_interval(now: datetime, fast_mode_until: datetime | None) -> int:
    if fast_mode_until and now < fast_mode_until:
        return _FAST_SCAN_INTERVAL
    from config.settings import bot_config
    if _is_midnight_window(now):
        return bot_config.midnight_scan_interval
    return _NORMAL_SCAN_INTERVAL


async def scan_and_bet_loop(state):
    """Scan loop — akıllı tarama ile.

    TEK try/except ile tüm while body'si korunuyor.
    Hata durumunda loop çökmez, 60sn recovery ile devam eder.
    """
    from jobs.scheduler import (
        run_cycle,
        run_fetch_markets,
        run_fetch_weather,
        run_parse_markets,
    )

    stale_check_counter = 0
    last_day = None
    previous_market_count = 0
    fast_mode_until = None

    try:
        previous_market_count = _get_market_count()
        logger.info("Initial market count: %d", previous_market_count)
    except Exception as e:
        logger.warning("Could not get initial market count: %s", e)

    while state.is_running:
        try:  # ← TEK TRY — her şey içeride
            state.last_scan = datetime.now(timezone.utc).replace(tzinfo=None)
            scan_start = datetime.now(timezone.utc)

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today = now.date()
            is_new_day = last_day is not None and today != last_day
            last_day = today

            if is_new_day:
                logger.info("Midnight detected — running immediate scan")

            # STEP 1: Fetch markets
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

            # STEP 3: Run cycle
            await asyncio.wait_for(asyncio.to_thread(run_cycle), timeout=_CYCLE_TIMEOUT)

            # Yeni market algılama
            try:
                current_count = _get_market_count()
                if current_count > previous_market_count:
                    new_markets = current_count - previous_market_count
                    fast_mode_until = (datetime.now(timezone.utc) + timedelta(minutes=_FAST_MODE_MINUTES)).replace(tzinfo=None)
                    logger.info(
                        "NEW MARKETS DETECTED: +%d (total: %d) — FAST MODE for %d min",
                        new_markets, current_count, _FAST_MODE_MINUTES
                    )
                previous_market_count = current_count
            except Exception as e:
                logger.warning("Market count check failed: %s", e)

            # Stale cleanup her 10 döngüde
            stale_check_counter += 1
            if stale_check_counter >= 10:
                stale_check_counter = 0
                try:
                    await asyncio.wait_for(asyncio.to_thread(_cleanup_stale_bets), timeout=_CLEANUP_TIMEOUT)
                except Exception as e:
                    logger.warning("Stale cleanup failed: %s", e)

            # Scan duration log
            scan_duration = (datetime.now(timezone.utc) - scan_start).total_seconds()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            interval = _get_scan_interval(now, fast_mode_until)
            mode = "FAST" if fast_mode_until and now < fast_mode_until else "NORMAL"
            logger.info("Scan completed in %.1fs [%s mode], next in %ds", scan_duration, mode, interval)

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.info("Scan loop cancelled — shutting down")
            break
        except asyncio.TimeoutError:
            logger.error("Scan step timed out — retry in 60s")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error("Scan error: %s — retry in 60s", e, exc_info=True)
            await asyncio.sleep(60)

    logger.info("Scan loop exited (is_running=%s)", state.is_running)


async def settlement_loop(state):
    """Settlement loop + scan loop watchdog.

    Scan loop 30dk+ süredir çalışmıyorsa log yazıyor.
    1 saati aşkın süredir çalışmıyorsa bot'u durduruyor.
    """
    from jobs.scheduler import run_settle

    last_cleanup_date = None
    scan_healthy = True

    while state.is_running:
        try:
            # ── Watchdog: scan loop sağlık kontrolü ──
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            if state.last_scan:
                elapsed = (now_utc - state.last_scan).total_seconds()
                if elapsed > _WATCHDOG_DEAD:
                    if scan_healthy:
                        logger.error(
                            "SCAN LOOP WATCHDOG: No scan for %.1f minutes! last_scan=%s",
                            elapsed / 60, state.last_scan
                        )
                        scan_healthy = False
                    # 1 saatten fazlaysa bot'u durdur
                    if elapsed > _WATCHDOG_RESTART:
                        logger.critical(
                            "SCAN LOOP DEAD for >%.0f min — stopping bot for restart",
                            elapsed / 60
                        )
                        state.is_running = False
                        break
                elif elapsed > _WATCHDOG_WARNING:
                    logger.warning(
                        "SCAN LOOP WATCHDOG: Last scan %.1f min ago (warning)",
                        elapsed / 60
                    )
                else:
                    if not scan_healthy:
                        logger.info("Scan loop recovered — healthy again")
                    scan_healthy = True
            else:
                if scan_healthy:
                    logger.warning("SCAN LOOP WATCHDOG: last_scan is None (never ran?)")
                    scan_healthy = False

            # ── Normal settlement işlemi ──
            await asyncio.to_thread(run_settle)

            today = datetime.now(timezone.utc).date()
            if last_cleanup_date != today:
                from database.db_cleanup import auto_cleanup
                await asyncio.to_thread(auto_cleanup, hot_days=10, cold_days=120)
                last_cleanup_date = today

            if state.sia_loop is not None and (
                state.sia_last_run is None
                or (now_utc - state.sia_last_run).total_seconds() >= state.sia_interval_hours * 3600
            ):
                await asyncio.to_thread(state.sia_loop.run_optimization_cycle)
                state.sia_last_run = datetime.now(timezone.utc).replace(tzinfo=None)

        except asyncio.CancelledError:
            logger.info("Settlement loop cancelled")
            break
        except Exception as e:
            logger.error("Settle error: %s", e, exc_info=True)

        await asyncio.sleep(state.config.SETTLEMENT_INTERVAL)

    logger.info("Settlement loop exited (is_running=%s)", state.is_running)


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
