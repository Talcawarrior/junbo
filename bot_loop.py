"""Background bot loops: scan-and-bet, settlement, stale cleanup.

ASYNCIO safety: Each loop has a SINGLE try/except wrapping the entire body
so that no exception can silently kill the loop without logging.

Watchdog: settlement_loop monitors scan_loop health via state.last_scan.
"""

import asyncio
import logging
from datetime import date, datetime, timezone, timedelta

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
# Tarama (bet açma) döngüsü, Polymarket fiyat çekme temposuyla aynı: 5 dk.
# Önceden 15 dk'ydı; o süre yalnızca Open-Meteo'nin saatlik rate-limit'ini
# beklemek içindi. Artık meteo çekimi tarama döngüsünden ayrıldı (aşağıdaki
# "decouple" adımı), böylece bahisler Polymarket verisinin tazelendiği 5 dk
# temposunda açılabilir.
_NORMAL_SCAN_INTERVAL = 300  # 5 dakika (Polymarket fetch temposuyla hizali)

# Fiyat poller: 2 gün sonrası (yeni tarih) marketler açıldığında 30 dk boyunca
# her dakika fiyat çek, sonra tekrar 5 dk'ya dön. Tarih üzerinden tetikleme:
# açık marketlerin en güncel tarihi ilerlediğinde (örn. 20/7 -> 21/7) 1 kez tetiklenir.
_FAST_PRICE_INTERVAL = 60  # 1 dakika
_FAST_PRICE_WINDOW = 30 * 60  # 30 dakika

# Watchdog thresholds (seconds)
_WATCHDOG_WARNING = 900  # 15 dakika — warning
_WATCHDOG_DEAD = 1800  # 30 dakika — dead
_WATCHDOG_RESTART = 3600  # 1 saat — restart

# Polymarket fiyat poll dongusu — PnL ve UI fiyatlarini canli tutar
_PRICE_POLL_INTERVAL = 300  # 5 dakika

# Meteo tahmin dongusu — Open-Meteo saatlik guncellenir
_WEATHER_FETCH_INTERVAL = 3600  # 1 saat


def _get_market_count() -> int:
    with get_session() as db:
        return db.query(WeatherMarket).filter(WeatherMarket.status == "open").count()


def _get_open_target_dates() -> set:
    """Açık marketlerin hedef TARİH (takvim günü) kümesi.

    2-gün-sonrası taraması TARİH üzerinden yapılır: scan loop, açık
    marketlerin en güncel tarihinin ilerleyip ilerlemediğini takip eder.
    Örn. açık tarihler 18-19-20/7 iken 21/7 belirirse (gece yarısından
    saatler sonra bile) fiyat poller'ı 1 dakikaya alınır. Mevcut açık
    tarih değişmezse (hala 18-19-20/7) 5 dk'da kalınır.
    """
    dates: set = set()
    with get_session() as db:
        for row in db.query(WeatherMarket.target_date).filter(WeatherMarket.status == "open").all():
            td = row[0]
            if td is not None:
                dates.add(td.date())
    return dates


def _get_open_market_count_for_date(target_day: date) -> int:
    """Belirli bir takvim gününde açık olan market sayısı (log/tetikleme için)."""
    with get_session() as db:
        lo = datetime(target_day.year, target_day.month, target_day.day, 0, 0, 0)
        hi = lo + timedelta(days=1)
        return (
            db.query(WeatherMarket.id)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.target_date >= lo,
                WeatherMarket.target_date < hi,
            )
            .count()
        )


def _next_two_day_target(last_date: date | None, open_dates: set) -> tuple:
    """2-gün-sonrası tetikleme kararı (saf fonksiyon, test edilebilir).

    Açık marketlerin en güncel tarihi `last_date`'ten ileri taşınmışsa
    (yeni bir tarih belirdiğinde) (yeni_tarih, True) döner — tetikle.
    Aynı tarihte kalınıyorsa (yeni_tarih, False): zaten tetiklenmiş,
    tekrar tetikleme (yalnızca 1 kez). Açık market yoksa (None, False).
    """
    if not open_dates:
        return None, False
    max_date = max(open_dates)
    if last_date is None or max_date > last_date:
        return max_date, True
    return max_date, False


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


async def price_poller_loop(state):
    """Polymarket fiyat poll dongusu — her 5 dakikada bir.

    run_fetch_markets ile Polymarket fiyatlarini ceker (WeatherMarket
     cache'i tazelenir) ve run_update_prices ile acik betlerin
     current_price + unrealized_pnl degerlerini gunceller.
     Boylece UI ve PnL tarama dongusunden bagimsiz olarak canli kalir.
    """
    from jobs.scheduler import (
        run_fetch_markets,
        run_risk_management,
        run_update_prices,
    )

    logger.info("Price poller loop basladi (interval=%ds)", _PRICE_POLL_INTERVAL)
    while state.is_running:
        try:
            await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)
            await asyncio.wait_for(asyncio.to_thread(run_update_prices), timeout=_FETCH_TIMEOUT)
            # Risk yönetimini de fiyat poller'a bağla: stop-loss / take-profit /
            # trailing stop kontrolleri artık her 5 dakikada bir (fiyat
            # tazelemeyle aynı döngüde) çalışır. Böylece son dakikalarda hızla
            # düşen, vadeye yakın bahisler tarama döngüsünden
            # kaçıp settlement'e gitmez.
            await asyncio.wait_for(asyncio.to_thread(run_risk_management), timeout=_FETCH_TIMEOUT)
            state.last_price_update = datetime.now(timezone.utc).replace(tzinfo=None)
        except asyncio.CancelledError:
            logger.info("Price poller cancelled")
            break
        except asyncio.TimeoutError:
            logger.error("Price poll timed out — retry in 60s")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error("Price poll error: %s — retry in 60s", e)
            await asyncio.sleep(60)
        else:
            # 2 gün sonrası bahisler açıldıysa 20 dk boyunca her dakika fiyat
            # çek (state.fast_price_until), sonra tekrar 5 dk'ya dön.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            interval = (
                _FAST_PRICE_INTERVAL
                if state.fast_price_until and now < state.fast_price_until
                else _PRICE_POLL_INTERVAL
            )
            await asyncio.sleep(interval)
    logger.info("Price poller loop exited (is_running=%s)", state.is_running)


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
    last_weather_fetch = None  # Son weather fetch zamanı
    last_two_day_date = None  # En son tetiklenen 2-gün (yeni tarih) açık market tarihi

    try:
        previous_market_count = _get_market_count()
        logger.info("Initial market count: %d", previous_market_count)
        last_two_day_date = max(_get_open_target_dates(), default=None)
    except Exception as e:
        logger.warning("Could not get initial market state: %s", e)

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

            # STEP 1: Fetch markets (Polymarket) — her döngü
            await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)

            # STEP 2: Parse — her döngü (cache'lenmiş meteo verisiyle)
            try:
                await asyncio.wait_for(asyncio.to_thread(run_parse_markets), timeout=_FETCH_TIMEOUT)
            except Exception as e:
                logger.error("Parse step error: %s", e)

            # STEP 3: Run cycle (analyze -> place bets). Meteo çekimini BEKLEMEDEN
            # hemen cache'den açılır. Böylece bahisler Polymarket verisinin
            # tazelendiği 5 dk temposunda açılır; meteo saatte 1 kez yenilenir
            # ve bahis açılımını bloklamaz.
            await asyncio.wait_for(asyncio.to_thread(run_cycle), timeout=_CYCLE_TIMEOUT)

            # STEP 4: Meteo tazeleme — SADECE saatte 1 kez ve bahis açılımından
            # SONRA (bet opening'ı bloklamaz). Önceki saatlik veri zaten cache'te,
            # dolayısıyla meteo kaydı çekmekle vakit kaybedilmez.
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            should_fetch_weather = (
                last_weather_fetch is None or (now_utc - last_weather_fetch).total_seconds() >= _WEATHER_FETCH_INTERVAL
            )
            if should_fetch_weather:
                try:
                    weather_res = await asyncio.wait_for(asyncio.to_thread(run_fetch_weather), timeout=_FETCH_TIMEOUT)
                    last_weather_fetch = datetime.now(timezone.utc).replace(tzinfo=None)
                    logger.info("Weather fetch complete: %s", weather_res)
                except Exception as e:
                    # Don't advance last_weather_fetch — retry next cycle so a
                    # transient failure can't silently starve markets of weather.
                    logger.error("Weather fetch FAILED: %s — will retry next cycle", e, exc_info=e)

            # Yeni market algılama (scan hızlı modu için)
            try:
                current_count = _get_market_count()
                if current_count > previous_market_count:
                    new_markets = current_count - previous_market_count
                    fast_mode_until = (datetime.now(timezone.utc) + timedelta(minutes=_FAST_MODE_MINUTES)).replace(
                        tzinfo=None
                    )
                    logger.info(
                        "NEW MARKETS DETECTED: +%d (total: %d) — FAST MODE for %d min",
                        new_markets,
                        current_count,
                        _FAST_MODE_MINUTES,
                    )
                previous_market_count = current_count
            except Exception as e:
                logger.warning("Market count check failed: %s", e)

            # 2 gün sonrası (yeni tarih) marketler 'açılır açılmaz' fiyat poller'ını
            # 30 dk boyunca her dakika çalıştır. TARİH üzerinden: açık marketlerin
            # en güncel tarihi ilerlediğinde (örn. 20/7 -> 21/7) tetiklenir, yalnızca
            # 1 kez (gece yarısından saatler sonra bile). Mevcut açık tarih değişmezse
            # (hala 18-19-20/7) 5 dk'da kalır.
            try:
                open_dates = _get_open_target_dates()
                new_date, trigger = _next_two_day_target(last_two_day_date, open_dates)
                if trigger:
                    new_count = _get_open_market_count_for_date(new_date)
                    state.fast_price_until = (
                        datetime.now(timezone.utc) + timedelta(seconds=_FAST_PRICE_WINDOW)
                    ).replace(tzinfo=None)
                    logger.info(
                        "2-day-ahead date %s opened (%d markets) — price poller FAST (1min) for %d min",
                        new_date.isoformat(),
                        new_count,
                        _FAST_PRICE_WINDOW // 60,
                    )
                    last_two_day_date = new_date
                elif new_date is not None:
                    last_two_day_date = new_date
            except Exception as e:
                logger.warning("2-day-ahead detection failed: %s", e)

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
                            "SCAN LOOP WATCHDOG: No scan for %.1f minutes! last_scan=%s", elapsed / 60, state.last_scan
                        )
                        scan_healthy = False
                    # 1 saatten fazlaysa bot'u durdur
                    if elapsed > _WATCHDOG_RESTART:
                        logger.critical("SCAN LOOP DEAD for >%.0f min — stopping bot for restart", elapsed / 60)
                        state.is_running = False
                        break
                elif elapsed > _WATCHDOG_WARNING:
                    logger.warning("SCAN LOOP WATCHDOG: Last scan %.1f min ago (warning)", elapsed / 60)
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
