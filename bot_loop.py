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

_verify_ui: object | None = None
_verify_poly: object | None = None
try:
    from scripts.verify_ui_markets import verify_all_open_dates as _verify_ui
    from scripts.verify_ui_markets import verify_db_vs_poly as _verify_poly
except ImportError:
    pass

logger = logging.getLogger("BOT_LOOP")

# Timeout values (seconds)
_FETCH_TIMEOUT = 60
_CYCLE_TIMEOUT = 180
_CLEANUP_TIMEOUT = 60

# Akilli tarama ayarlari
_FAST_MODE_MINUTES = 30
_FAST_SCAN_INTERVAL = 60
# Tarama (bet acma) dongusu, Polymarket fiyat cekme temposuyla ayni: 5 dk.
# Onceden 15 dk'ydi; o sure yalnizca Open-Meteo'nin saatlik rate-limit'ini
# beklemek icindi. Artik meteo cekimi tarama dongusunden ayrildi (asagidaki
# "decouple" adimi), boylece bahisler Polymarket verisinin tazelendigi 5 dk
# temposunda acilabilir.
_NORMAL_SCAN_INTERVAL = 300  # 5 dakika (Polymarket fetch temposuyla hizali)

# Fiyat poller: 2 gun sonrasi (yeni tarih) marketler acildiginda 30 dk boyunca
# her dakika fiyat cek, sonra tekrar 5 dk'ya don. Tarih uzerinden tetikleme:
# acik marketlerin en guncel tarihi ilerlediginde (orn. 20/7 -> 21/7) 1 kez tetiklenir.
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
    """Acik marketlerin hedef TARIH (takvim gunu) kumesi.

    2-gun-sonrasi taramasi TARIH uzerinden yapilir: scan loop, acik
    marketlerin en guncel tarihinin ilerleyip ilerlemedigini takip eder.
    Orn. acik tarihler 18-19-20/7 iken 21/7 belirirse (gece yarisindan
    saatler sonra bile) fiyat poller'i 1 dakikaya alinir. Mevcut acik
    tarih degismezse (hala 18-19-20/7) 5 dk'da kalinir.
    """
    dates: set = set()
    with get_session() as db:
        for row in db.query(WeatherMarket.target_date).filter(WeatherMarket.status == "open").all():
            td = row[0]
            if td is not None:
                dates.add(td.date())
    return dates


def _get_open_market_count_for_date(target_day: date) -> int:
    """Belirli bir takvim gununde acik olan market sayisi (log/tetikleme icin)."""
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
    """2-gun-sonrasi tetikleme karari (saf fonksiyon, test edilebilir).

    Acik marketlerin en guncel tarihi `last_date`'ten ileri tasinmissa
    (yeni bir tarih belirdiginde) (yeni_tarih, True) doner — tetikle.
    Ayni tarihte kaliniyorsa (yeni_tarih, False): zaten tetiklenmis,
    tekrar tetikleme (yalnizca 1 kez). Acik market yoksa (None, False).
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
    # Forecast Latency Arbitrage: scan faster during model run data windows
    from utils.model_run_detector import get_model_run_fast_interval

    model_interval = get_model_run_fast_interval(now)
    if model_interval is not None:
        return model_interval
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
        run_refresh_open_prices,
        run_risk_management,
        run_update_prices,
    )

    logger.info("Price poller loop basladi (interval=%ds)", _PRICE_POLL_INTERVAL)
    while state.is_running:
        try:
            await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)
            # Refresh prices for markets we still hold (public-search stops
            # returning ended markets, so their stored price freezes). This
            # keeps the dashboard / PnL live through resolution.
            await asyncio.wait_for(asyncio.to_thread(run_refresh_open_prices), timeout=_FETCH_TIMEOUT)
            await asyncio.wait_for(asyncio.to_thread(run_update_prices), timeout=_FETCH_TIMEOUT)
            # Risk yonetimini de fiyat poller'a bagla: stop-loss / take-profit /
            # trailing stop kontrolleri artik her 5 dakikada bir (fiyat
            # tazelemeyle ayni dongude) calisir. Boylece son dakikalarda hizla
            # dusen, vadeye yakin bahisler tarama dongusunden
            # kacip settlement'e gitmez.
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
            # 2 gun sonrasi bahisler acildiysa 20 dk boyunca her dakika fiyat
            # cek (state.fast_price_until), sonra tekrar 5 dk'ya don.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            interval = (
                _FAST_PRICE_INTERVAL
                if state.fast_price_until and now < state.fast_price_until
                else _PRICE_POLL_INTERVAL
            )
            await asyncio.sleep(interval)
    logger.info("Price poller loop exited (is_running=%s)", state.is_running)


async def scan_and_bet_loop(state):
    """Scan loop — akilli tarama ile.

    TEK try/except ile tum while body'si korunuyor.
    Hata durumunda loop cokmez, 60sn recovery ile devam eder.
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
    last_weather_fetch = None  # Son weather fetch zamani
    last_two_day_date = None  # En son tetiklenen 2-gun (yeni tarih) acik market tarihi
    model_run_fast_until: datetime | None = None  # Model run fast mode end time
    poly_verify_counter = 0  # 2 saatte bir DB vs Polymarket kontrolu
    _POLY_VERIFY_INTERVAL = 24  # 5 dk dongu × 24 = 120 dk (2 saat)

    try:
        previous_market_count = _get_market_count()
        logger.info("Initial market count: %d", previous_market_count)
        last_two_day_date = max(_get_open_target_dates(), default=None)
    except Exception as e:
        logger.warning("Could not get initial market state: %s", e)

    while state.is_running:
        try:  # ← TEK TRY — her sey iceride
            state.last_scan = datetime.now(timezone.utc).replace(tzinfo=None)
            scan_start = datetime.now(timezone.utc)

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today = now.date()
            is_new_day = last_day is not None and today != last_day
            last_day = today

            if is_new_day:
                logger.info("Midnight detected — running immediate scan")
                # UI dogrulama: Polymarket'teki yeni gun marketlerini DB ile kiyasla
                if _verify_ui:
                    try:
                        _verify_ui()
                    except Exception as e:
                        logger.warning("UI dogrulama hatasi (yeni gun): %s", e)

            # STEP 1: Fetch markets (Polymarket) — her dongu
            # Ag hatasi (DNS/timeout) cycle'in geri kalanini DURDURMASIN:
            # analiz/bet eski veriyle devam edebilir, sadece market listesi tazelenmez.
            try:
                await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("Fetch markets timed out (%ds) — continuing with stale data", _FETCH_TIMEOUT)
            except Exception as e:
                logger.error("Fetch markets failed (%s) — continuing with stale data", e)

            # STEP 2: Parse — her dongu (cache'lenmis meteo verisiyle)
            try:
                await asyncio.wait_for(asyncio.to_thread(run_parse_markets), timeout=_FETCH_TIMEOUT)
            except Exception as e:
                logger.error("Parse step error: %s", e)

            # STEP 3: Run cycle (analyze -> place bets). Meteo cekimini BEKLEMEDEN
            # hemen cache'den acilir. Boylece bahisler Polymarket verisinin
            # tazelendigi 5 dk temposunda acilir; meteo saatte 1 kez yenilenir
            # ve bahis acilimini bloklamaz.
            await asyncio.wait_for(asyncio.to_thread(run_cycle), timeout=_CYCLE_TIMEOUT)

            # STEP 4: Meteo tazeleme — SADECE saatte 1 kez ve bahis acilimindan
            # SONRA (bet opening'i bloklamaz). Onceki saatlik veri zaten cache'te,
            # dolayisiyla meteo kaydi cekmekle vakit kaybedilmez.
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

            # Forecast Latency Arbitrage: detect model run windows & fast scan
            try:
                from utils.model_run_detector import (
                    is_in_model_run_window,
                    log_model_run_status,
                    MODEL_RUN_FAST_WINDOW,
                )

                now_utc_arb = datetime.now(timezone.utc).replace(tzinfo=None)
                if is_in_model_run_window(now_utc_arb):
                    # Activate fast mode for model run window
                    if model_run_fast_until is None or now_utc_arb >= model_run_fast_until:
                        model_run_fast_until = now_utc_arb + timedelta(seconds=MODEL_RUN_FAST_WINDOW)
                        log_model_run_status(now_utc_arb)
                        logger.info(
                            "MODEL RUN WINDOW — FAST MODE for %d min",
                            MODEL_RUN_FAST_WINDOW // 60,
                        )
                else:
                    model_run_fast_until = None
            except Exception as e:
                logger.debug("Model run detection error: %s", e)

            # Yeni market algilama (scan hizli modu icin)
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

            # 2 gun sonrasi (yeni tarih) marketler 'acilir acilmaz' fiyat poller'ini
            # 30 dk boyunca her dakika calistir. TARIH uzerinden: acik marketlerin
            # en guncel tarihi ilerlediginde (orn. 20/7 -> 21/7) tetiklenir, yalnizca
            # 1 kez (gece yarisindan saatler sonra bile). Mevcut acik tarih degismezse
            # (hala 18-19-20/7) 5 dk'da kalir.
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
                    # UI dogrulama: yeni tarih icin Polymarket marketlerini kontrol et
                    if _verify_ui:
                        try:
                            _verify_ui()
                        except Exception as e:
                            logger.warning("UI dogrulama hatasi (tarih=%s): %s", new_date.isoformat(), e)
                elif new_date is not None:
                    last_two_day_date = new_date
            except Exception as e:
                logger.warning("2-day-ahead detection failed: %s", e)

            # Stale cleanup her 10 dongude
            stale_check_counter += 1
            if stale_check_counter >= 10:
                stale_check_counter = 0
                try:
                    await asyncio.wait_for(asyncio.to_thread(_cleanup_stale_bets), timeout=_CLEANUP_TIMEOUT)
                except Exception as e:
                    logger.warning("Stale cleanup failed: %s", e)

            # DB vs Polymarket karsilastirma: 2 saatte bir
            poly_verify_counter += 1
            if poly_verify_counter >= _POLY_VERIFY_INTERVAL and _verify_poly:
                poly_verify_counter = 0
                try:
                    report = await asyncio.wait_for(asyncio.to_thread(_verify_poly), timeout=120)
                    if report:
                        logger.warning("DB vs Polymarket uyumsuzluk:\n%s", report)
                    else:
                        logger.info("DB vs Polymarket: tum bet'ler eslesiyor")
                except Exception as e:
                    logger.warning("DB vs Polymarket kontrol hatasi: %s", e)

            # Scan duration log
            scan_duration = (datetime.now(timezone.utc) - scan_start).total_seconds()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            interval = _get_scan_interval(now, fast_mode_until)
            # Also check model run fast mode
            if model_run_fast_until and now < model_run_fast_until:
                interval = min(interval, _FAST_SCAN_INTERVAL)
            mode = (
                "FAST"
                if (fast_mode_until and now < fast_mode_until) or (model_run_fast_until and now < model_run_fast_until)
                else "NORMAL"
            )
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

    Scan loop 30dk+ suredir calismiyorsa log yaziyor.
    1 saati askin suredir calismiyorsa bot'u durduruyor.
    """
    from jobs.scheduler import run_settle

    last_cleanup_date = None
    scan_healthy = True

    while state.is_running:
        try:
            # ── Watchdog: scan loop saglik kontrolu ──
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

            # ── Normal settlement islemi ──
            await asyncio.to_thread(run_settle)

            today = datetime.now(timezone.utc).date()
            if last_cleanup_date != today:
                from database.db_cleanup import auto_cleanup

                await asyncio.to_thread(auto_cleanup, hot_days=10, cold_days=120)
                last_cleanup_date = today

            await _run_daily_maintenance()

        except asyncio.CancelledError:
            logger.info("Settlement loop cancelled")
            break
        except Exception as e:
            logger.error("Settle error: %s", e, exc_info=True)

        await asyncio.sleep(state.config.SETTLEMENT_INTERVAL)

    logger.info("Settlement loop exited (is_running=%s)", state.is_running)


_SNAPSHOT_INTERVAL = 1800  # 30 dakika — first-peak analizi icin daha yuksek cokozunurluk


async def snapshot_loop(state):
    """30 dakikada bir bet snapshot dongusu — giris zamani analizi icin."""
    from jobs.snapshot_job import take_market_snapshots, cleanup_old_snapshots

    last_cleanup_date = None
    logger.info("Snapshot loop started")

    while state.is_running:
        try:
            saved = await asyncio.to_thread(take_market_snapshots)
            logger.info("Snapshot loop: %d snapshots saved", saved)

            today = datetime.now(timezone.utc).date()
            if last_cleanup_date != today:
                await asyncio.to_thread(cleanup_old_snapshots, days=365)
                last_cleanup_date = today

        except asyncio.CancelledError:
            logger.info("Snapshot loop cancelled")
            break
        except Exception as e:
            logger.error("Snapshot error: %s", e, exc_info=True)

        await asyncio.sleep(_SNAPSHOT_INTERVAL)

    logger.info("Snapshot loop exited (is_running=%s)", state.is_running)


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


async def _run_daily_maintenance() -> None:
    """Daily self-evolution + verified DB backup, at most once per UTC day.

    Both jobs use a persisted marker so restarts don't double-run them.
    """
    from jobs.evolution_job import run_evolution_cycle, should_run
    from jobs.backup_job import run_backup_once

    if should_run():
        await asyncio.to_thread(run_evolution_cycle)

    # ── Pre-flight safety check (logs warnings if strategy params unsafe) ─

    try:
        await asyncio.to_thread(run_backup_once)
    except Exception as e:
        logger.error("Scheduled backup failed: %s", e)
