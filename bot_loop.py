"""Background bot loops: scan-and-bet, settlement, stale cleanup.

ASYNCIO safety: Each loop has a SINGLE try/except wrapping the entire body
so that no exception can silently kill the loop without logging.

Watchdog: settlement_loop monitors scan_loop health via state.last_scan.
"""

import asyncio
import logging
import os
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

# METAR canli sicaklik dongusu — aviationweather.gov 30dk'da bir guncellenir
_METAR_POLL_INTERVAL = 1800  # 30 dakika


def _get_price_poll_interval(state, now: datetime) -> int:
    """Price poller interval karari (saf fonksiyon, test edilebilir).

    2 gun sonrasi bahisler acildiysa 20 dk boyunca her dakika fiyat cek
    (state.fast_price_until), sonra tekrar 5 dk'ya don.
    """
    return _FAST_PRICE_INTERVAL if state.fast_price_until and now < state.fast_price_until else _PRICE_POLL_INTERVAL


def _get_market_count() -> int:
    with get_session() as db:
        return db.query(WeatherMarket).filter(WeatherMarket.status == "open").count()


def _get_open_target_dates() -> set:
    """Acik marketlerin hedef TARIH (takvim gunu) kumesi — sadece BUGUN ve sonrasi.

    2-gun-sonrasi taramasi TARIH uzerinden yapilir: scan loop, acik
    marketlerin en guncel tarihinin ilerleyip ilerlemedigini takip eder.
    Orn. acik tarihler 18-19-20/7 iken 21/7 belirirse (gece yarisindan
    saatler sonra bile) fiyat poller'i 1 dakikaya alinir. Mevcut acik
    tarih degismezse (hala 18-19-20/7) 5 dk'da kalinir.

    GECMIS tarihler (bugun oncesi) DAHIL EDILMEZ: settlement pending olan
    eski gun marketleri hala open gorunebilir ama "yeni tarih" sayilmamali
    (2026-08-12: +['2026-08-10'] yanlis pozitif FAST mode tetikliyordu).
    """
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    dates: set = set()
    with get_session() as db:
        for row in db.query(WeatherMarket.target_date).filter(WeatherMarket.status == "open").all():
            td = row[0]
            if td is not None and td.date() >= today:
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
    """Yeni gun marketleri acilis penceresi: 00:00 - 13:00 UTC.

    Snapshot verisi (2026-08-11 analiz): ilk market acilislari 04:00-12:30
    UTC arasina yayiliyor (sabit bir gece yarisi acilisi yok). Kullanici
    karari: 0-13 arasi hizli tarama.
    """
    from config.settings import bot_config

    window_hours = bot_config.midnight_scan_window
    return 0 <= now.hour < window_hours


def _probe_new_target_date(last_date: date | None) -> tuple[date | None, bool]:
    """Polymarket Gamma'dan HAFIF tek sorgu ile DB'deki max acik tarihten ileri
    bir tarih var mi diye bakar (tam cekis DEGIL, ~5 kayitlik probe).

    Yeni tarih bulunursa (new_date, True) doner; bulunamazsa (None, False).
    Bu, 0-13 UTC acilis penceresinde her ~1 sn'de cagrilir — fiyat henuz
    dusukken (acilis aninda) yeni gunun marketlerini yakalamak icin.
    """
    from config.settings import bot_config

    try:
        from scrapers.async_client import AsyncHttpClient as _ProbeClient

        client = _ProbeClient()
        host = bot_config.polymarket.gamma_url.split("//")[-1].split("/")[0]
        data = client.fetch_one_blocking(
            f"{bot_config.polymarket.gamma_url}/public-search",
            params={"q": "highest temperature", "limit_per_type": 5, "order": "endDate desc"},
            host=host,
        )
    except Exception as e:  # noqa: BLE001 - probe asla loop'u oldurmesin
        logger.warning("Probe new-date failed: %s", e)
        return None, False
    if not data:
        return None, False

    # En yeni event tarihlerini topla (title/endDate), DB'deki max ile karsilastir
    try:
        from datetime import datetime as _dt

        events = data.get("events", []) or []
        for ev in events:
            raw_end = ev.get("end_date_iso") or ev.get("endDate")
            title = ev.get("title", "")
            if not raw_end:
                continue
            try:
                end_dt = _dt.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except ValueError:
                end_dt = None
            if end_dt is None:
                continue
            end_day = end_dt.date()
            if last_date is not None and end_day > last_date:
                logger.info("Probe: yeni tarih bulundu %s (title=%s)", end_day, title[:60])
                return end_day, True
    except Exception as e:  # noqa: BLE001
        logger.warning("Probe parse failed: %s", e)
    return None, False


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
            interval = _get_price_poll_interval(state, now)
            await asyncio.sleep(interval)
    logger.info("Price poller loop exited (is_running=%s)", state.is_running)


async def metar_loop(state):
    """METAR canli sicaklik dongusu — her 30dk'da bir.

    Aviationweather.gov (NOAA, bedava) istasyon sicakligini 30dk'da bir
    gunceller. Bu dongu her 30dk'da bir acik marketlerin METAR'ini ceker;
    sicaklik max'a cikip 2 kez arka arkaya dustuyse zirve kilitlenir ve
    o sehrin kazanan bucket'ina TEK ESIK YES bet acilir.

    Kullanici karari (2026-08-14): "acik bet sehirleri listesini al, gun
    icinde metardan takip et, sicaklik dustugunu teyit ettiginde beti
    yapistir. %100 tutturmamiz onemli degil, tek esik olacagi icin kayip
    cok olmayacaktir."
    """
    from jobs.metar_peak import run_metar_peak_bets

    logger.info("METAR loop basladi (interval=%ds)", _METAR_POLL_INTERVAL)
    while state.is_running:
        try:
            await asyncio.wait_for(asyncio.to_thread(run_metar_peak_bets), timeout=_FETCH_TIMEOUT)
        except asyncio.CancelledError:
            logger.info("METAR loop cancelled")
            break
        except asyncio.TimeoutError:
            logger.error("METAR poll timed out — retry in 5min")
            await asyncio.sleep(300)
        except Exception as e:
            logger.error("METAR loop error: %s — retry in 5min", e)
            await asyncio.sleep(300)
        else:
            await asyncio.sleep(_METAR_POLL_INTERVAL)
    logger.info("METAR loop exited (is_running=%s)", state.is_running)


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
    from config.settings import bot_config

    last_day = None
    last_open_dates: set = set()
    fast_mode_until = None
    last_weather_fetch = None  # Son weather fetch zamani
    last_two_day_date = None  # En son tetiklenen 2-gun (yeni tarih) acik market tarihi
    model_run_fast_until: datetime | None = None  # Model run fast mode end time
    poly_verify_counter = 0  # 2 saatte bir DB vs Polymarket kontrolu
    _POLY_VERIFY_INTERVAL = 24  # 5 dk dongu × 24 = 120 dk (2 saat)
    spread_retry_counter = 0  # Yeni acilan marketleri yakalamak icin periyodik spread retry
    _SPREAD_RETRY_INTERVAL = 12  # her 12 dongu (~60 dk) bir

    try:
        last_open_dates = _get_open_target_dates()
        logger.info("Initial open target dates: %d", len(last_open_dates))
        last_two_day_date = max(last_open_dates, default=None)
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

            # STEP 1: Fetch markets (Polymarket).
            # 0-13 UTC acilis penceresinde HER dongude tam cekis (100+ sorgu)
            # rate limit'i patlatir. Bu yuzden pencere icinde ONCE hafif probe
            # (tek sorgu) ile yeni tarih var mi bakilir; yeni tarih VARSA tam
            # cekis + spread yapilir, YOKSA cekis atlanir (loop 1 sn'de doner).
            # Pencere disinda (13:00+) normal 5 dk tarama (her dongude cekis).
            # Ag hatasi cycle'i DURDURMASIN.
            now_fetch = datetime.now(timezone.utc).replace(tzinfo=None)
            _probe_target = None
            if _is_midnight_window(now_fetch):
                try:
                    _probe_target, _trigger = await asyncio.wait_for(
                        asyncio.to_thread(_probe_new_target_date, last_two_day_date), timeout=10
                    )
                except asyncio.TimeoutError:
                    _probe_target = None
                except Exception as e:
                    logger.warning("Probe new-date error: %s", e)
                    _probe_target = None
            if _probe_target is not None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.error("Fetch markets timed out (%ds)", _FETCH_TIMEOUT)
                except Exception as e:
                    logger.error("Fetch markets failed (%s)", e)
            elif not _is_midnight_window(now_fetch):
                try:
                    await asyncio.wait_for(asyncio.to_thread(run_fetch_markets), timeout=_FETCH_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.error("Fetch markets timed out (%ds) — continuing with stale data", _FETCH_TIMEOUT)
                except Exception as e:
                    logger.error("Fetch markets failed (%s) — continuing with stale data", e)

            # STEP 2: Parse — her dongu (cache'lenmis meteo verisiyle).
            # 0-13 penceresinde probe yeni tarih bulamadiysa parse'a gerek yok
            # (veri degismedi); 1 sn'lik hizli donguyu bloklamamak icin atlanir.
            if not (_is_midnight_window(now_fetch) and _probe_target is None):
                try:
                    await asyncio.wait_for(asyncio.to_thread(run_parse_markets), timeout=_FETCH_TIMEOUT)
                except Exception as e:
                    logger.error("Parse step error: %s", e)

            # STEP 3: Run cycle (analyze -> place bets). Meteo cekimini BEKLEMEDEN
            # hemen cache'den acilir. Boylece bahisler Polymarket verisinin
            # tazelendigi 5 dk temposunda acilir; meteo saatte 1 kez yenilenir
            # ve bahis acilimini bloklamaz.
            #
            # Ana mod (spread): edge tabanli run_cycle cagirilmaz — bet acma
            # isi spread_placer'da (2-gun-sonrasi tarih acildiginda) yapilir.
            # Edge modu (eski davranis) BETTING_STRATEGY=edge ile korunur.
            strategy = getattr(bot_config.strategy, "betting_strategy", "edge")
            if strategy != "spread":
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

            # Yeni market algilama (scan hizli modu icin).
            # GUN BAZLI (kullanici tespiti 2026-08-11): market SAYISI degil,
            # acik TARIH kumesinde yeni bir gunun belirmesi tetikler. Gun
            # dongusunde bugunun marketleri kapanir, 2-gun-sonrasi acilir —
            # toplam sayi YAKLASIK AYNI kalir, sayi artisi YANLIS sinyaldir.
            # Dogru sinyal: acik tarih kumesine yeni bir takvim gunu eklenmesi.
            try:
                current_dates = _get_open_target_dates()
                new_dates = current_dates - last_open_dates
                if new_dates:
                    fast_mode_until = (datetime.now(timezone.utc) + timedelta(minutes=_FAST_MODE_MINUTES)).replace(
                        tzinfo=None
                    )
                    logger.info(
                        "NEW MARKET DATES DETECTED: +%s (total dates: %d) — FAST MODE for %d min",
                        sorted(d.isoformat() for d in new_dates),
                        len(current_dates),
                        _FAST_MODE_MINUTES,
                    )
                last_open_dates = current_dates
            except Exception as e:
                logger.warning("Open-date check failed: %s", e)

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
                    # ── Ana mod: spread stratejisi (BETTING_STRATEGY=spread) ──
                    # Yeni 2-gun-sonrasi tarih acildi -> en son meteo tahmini
                    # etrafinda +/- radius dereceye YES bet ac (ilk snapshot fiyati).
                    # Edge modunda (eski davranis) sadece FAST price poller aktif.
                    strategy = getattr(bot_config.strategy, "betting_strategy", "edge")
                    if strategy == "spread":
                        try:
                            from executor.spread_placer import place_spread_bets

                            res = await asyncio.wait_for(
                                asyncio.to_thread(place_spread_bets, new_date),
                                timeout=_CYCLE_TIMEOUT,
                            )
                            logger.info(
                                "SPREAD strategy: %s date opened -> %s",
                                new_date.isoformat(),
                                res,
                            )
                        except Exception as e:
                            logger.error("Spread placement failed for %s: %s", new_date.isoformat(), e)
                    else:
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

            # PERIYODIK SPREAD RETRY (2026-08-11 kullanici istegi):
            # Polymarket marketleri tek seferde degil, zamana yayilarak acilir.
            # Yeni tarih tetiklemesi SADECE ilk acilista calisir; sonradan acilan
            # esikler (orn. Ankara 32C "NEW") hic yakalanmazdi. Bu yuzden spread
            # modunda her ~12 dongu (60 dk) bir, en yeni acik tarih icin
            # place_spread_bets yeniden cagrilir. Dup kontrolu oldugu icin
            # mevcut betler tekrar acilmaz; sadece eksik esikler tamamlanir.
            spread_retry_counter += 1
            if spread_retry_counter >= _SPREAD_RETRY_INTERVAL:
                spread_retry_counter = 0
                strategy = getattr(bot_config.strategy, "betting_strategy", "edge")
                if strategy == "spread":
                    try:
                        from executor.spread_placer import place_spread_bets

                        retry_dates = _get_open_target_dates()
                        if retry_dates:
                            retry_target = max(retry_dates)
                            res = await asyncio.wait_for(
                                asyncio.to_thread(place_spread_bets, retry_target),
                                timeout=_CYCLE_TIMEOUT,
                            )
                            logger.info("SPREAD periodic retry %s -> %s", retry_target.isoformat(), res)
                    except Exception as e:
                        logger.error("SPREAD periodic retry failed: %s", e)

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
                        # Process'i GERCEKTEN sonlandir: sadece state.is_running=False yapmak
                        # loop'lari kapatir ama process ayakta kalir (port tutulur, servis
                        # "Running" gorunur) ve restart HIC gerceklesmez. Windows servisi
                        # FAILURE_ACTIONS RESTART (5s/10s/30s) ile cikan process'i otomatik
                        # yeniden baslatir — bu yuzden hard exit sart.
                        logging.shutdown()
                        os._exit(1)
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


def _archive_clob_price(wm, price: float) -> None:
    """CLOB fiyat olayini orderbook.db'ye kalici arsivler (backtest icin).

    clob_stream WebSocket her fiyat degisimini aninda verir. Bu deger
    orderbook_snapshots'a best_ask olarak yazilir; zamanla gercek CLOB fiyat
    gecmisi birikir (backtest'ler bu seriyi kullanir). Best-effort: hata
    sessiz gecilir, fiyat guncellemesini bloklamaz.
    """
    import sqlite3

    try:
        _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ob_path = os.path.join(_repo_root, "data", "orderbook.db")
        conn = sqlite3.connect(ob_path, timeout=5)
        try:
            conn.execute(
                "INSERT INTO orderbook_snapshots "
                "(market_id, token_id, city, metric, target_date, best_ask, snapshot_time, created_at) "
                "VALUES (?,?,?,?,?,?,?, datetime('now'))",
                (
                    str(wm.id),
                    "0",
                    getattr(wm, "city", None),
                    getattr(wm, "metric", None),
                    getattr(wm, "target_date", None),
                    float(price),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("CLOB arsiv hatasi %s: %s", getattr(wm, "id", "?"), exc)


async def clob_stream_loop(state):
    """Polymarket CLOB WebSocket — acik betlerin marketlerini gercek zamanli dinler.

    Kullanici karari 2026-08-11: "millet milisaniyelerle islem yapiyor" — polling
    (5 dk) yerine WebSocket ile fiyat degisimlerini ANINDA almak icin. Acik
    betlerin YES token'larina abone olur; fiyat olayi gelince ilgili
    WeatherMarket.yes_price guncellenir (polling'e gerek kalmaz).

    Yeni bet acildikca asset listesi yenilenir; kapali betler cikarilir.
    """
    from scrapers.clob_stream import CLOBMarketStream

    def _asset_ids():
        with get_session() as s:
            rows = (
                s.query(WeatherMarket.id)
                .filter(WeatherMarket.id.isnot(None))
                .join(Bet, Bet.market_id == WeatherMarket.id)
                .filter(Bet.status.in_(OPEN_BET_STATUSES))
                .distinct()
                .all()
            )
            return [str(r[0]) for r in rows]

    async def _on_event(payload: dict) -> None:
        # CLOB market event: {"event_type": "price_change", "market": "<id>",
        # "price": 0.42, "side": "BUY", ...} gibi. Sadece fiyat guncellemesi
        # islenir; status/resolution gibi olaylar run_settle'a birakilir.
        try:
            mkt = payload.get("market") or payload.get("asset_id")
            price = payload.get("price")
            if not mkt or price is None:
                return
            with get_session() as s:
                wm = s.query(WeatherMarket).filter_by(id=str(mkt)).first()
                if wm is not None and wm.status == "open":
                    wm.yes_price = float(price)
                    wm.no_price = max(0.0, min(1.0, 1.0 - float(price)))
                    wm.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
                    # CLOB fiyatini orderbook.db'ye arsivle (backtest icin kalici).
                    # WebSocket fiyat olayi ~anlik: best_ask olarak kaydedilir.
                    _archive_clob_price(wm, float(price))
        except Exception as e:  # noqa: BLE001
            logger.warning("CLOB event isleme hatasi: %s", e)

    logger.info("CLOB stream loop basladi")
    while state.is_running:
        try:
            assets = _asset_ids()
            if not assets:
                logger.info("CLOB stream: acik bet yok, 60 sn bekliyorum")
                await asyncio.sleep(60)
                continue
            stream = CLOBMarketStream(assets, _on_event)
            await stream.run(stop=asyncio.Event(), max_retries=None)
        except asyncio.CancelledError:
            logger.info("CLOB stream loop cancelled")
            break
        except Exception as e:  # noqa: BLE001
            logger.error("CLOB stream loop error: %s — retry 30sn", e)
            await asyncio.sleep(30)
