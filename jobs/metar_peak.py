"""METAR zirve-tespiti tek esik bet acma.

Strateji (kullanici 2026-08-14):
1. Polymarket'ta acik marketi olan sehir listesi cekilir (2-a: tum acik sehirler).
2. Gun icinde her sehrin METAR sicakligi 30dk'da bir izlenir.
3. Sicaklik max'a cikip 2 kez arka arkaya duserse -> zirve KILITLENDI.
4. O sehrin kazanan bucket'ina (round(peak)) TEK ESIK YES bet acilir.
   (1-a: mevcut spread bet'leri acik kalir, bu sadece EK bet)
5. Kapanisa < 4 saat kalan sehirler atlanir (3-a: LA gibi bati ABD).

Kullanim: bot_loop.metar_loop her 30dk'da bir run_metar_peak_bets cagirir.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from database.db import get_session
from database.models import Bet, MetarObservation, Portfolio, WeatherMarket
from config.settings import bot_config

logger = logging.getLogger("SCHEDULER_METAR_PEAK")

# 2026-08-20 HIBRIT (kullanici onayi): sehir bazli gecmis ortalama peak saati
# ile ERKEN giris. Fiyat <= EARLY_MAX_PRICE ise dusus kilidi BEKLENMEDEN
# gireriz (ucuz firsat); pahaliysa piyasa bucket'i zaten biliyor demektir,
# 1-dusus kilidi beklenir. Backtest (05-19 Agu): hibrit 0.50 en iyi konfig —
# flat +$363 (kilit +$327, erken saf +$316), compound $200 -> $4,070
# (kilit $3,629, erken saf $2,973).
EARLY_MAX_PRICE = 0.50
# En az bu kadar gunluk gecmis veri olmadan tahmini peak saati yok (eski
# kilit kurali calisir).
MIN_PEAK_HISTORY_DAYS = 3
# 2026-08-21 kullanici karari: TUM kara liste KALDIRILDI (VHHH/ZGSZ/KBKF/
# KATL/KSEA/KSFO/NZWN). Denver (KBKF) sorgusu sonrasi kullanici riski kabul
# etti — bu sehirlerde METAR-peak bet yeniden acilir. Gecmis tutma orani
# (13-19 Agu): VHHH %20, ZGSZ %29, KBKF/KATL %43, KSEA/KSFO/NZWN %57, diger
# 35 sehir %100. Ileride tekrar kisitlamak istenirse bu set doldurulur.
METAR_PEAK_BLACKLIST: set[str] = set()

# Son tam arsiv toplama zamani (time.monotonic) — metar_loop 10 dk'ya inince
# aviationweather.gov'u yormamak icin arsiv 25 dk'da bir toplanir.
_LAST_ARCHIVE_RUN = 0.0

# Kapanisa kadar bet ACILABILIR (2026-08-18 E config, kullanici karari).
# Eski 2h kurali kapanisa yakin kilitlenen 13 sehir/gunun betini kaciriyordu.
# DIKKAT: yanlis bucket'taki acik betler (METAR'da farkli peak bulunan sehirler)
# kapanisa kadar KAPATILMAYA devam eder — bu sinir sadece YENI bet acma icindir
# (aktar mekanizmasi: zirve degisirse kapat + yeni zirveye ac).
MIN_HOURS_BEFORE_CLOSE = 0
# MIN_ENTRY 0.10 -> 0.05 (2026-08-18 E config, kullanici karari + backtest:
# 240 bet +$820.96 vs 202 bet +$593.62). 0.01-0.03 longshot'lar hala disinda
# (0.05 alti piyasa suphesi = METAR tespiti yanlis demektir).
MIN_ENTRY = 0.05
# METAR stake (kullanici karari 2026-08-16: 1 -> 2 -> 3 USD optimum.
# Backtest: bias-top 40 + tek esik, $3 stake = %91.7, +$120, maxDD $3.2.
# ROI stake'ten bagimsiz ama mutlak kazanc ve risk dengede $3 en iyi.)
METAR_STAKE = 3.0
# Kapanis = target_date + 12h (24:00 UTC)
CLOSE_HOURS = 12
# 2026-08-18 kullanici karari: "Metar betleri acilirken bias a gerek yok,
# nasil olsa peak tespit edilmis oluyor" -> BIAS_TOP_CITIES KALDIRILDI,
# TUM sehirlerin acik RANGE+max marketlerine bakilir.


def _city_lon(m) -> Optional[float]:
    """Sehir boylami (peak_watch dogu->bati siralama, 2026-08-21).

    Oncelik market kaydindaki longitude; bos/sifir ise config'deki
    _ICAO_COORDS (ICAO->koordinat) tablosundan. Bilinmiyorsa None doner
    (siralamada en sona duser).
    """
    lon = getattr(m, "longitude", None)
    if lon:
        return float(lon)
    code = getattr(m, "city_code", None)
    if code:
        try:
            from config.settings import _ICAO_COORDS

            c = _ICAO_COORDS.get(str(code))
            if c:
                return float(c[1])
        except Exception:  # noqa: BLE001 — gorunum yardimcisi, botu durdurmaz
            pass
    return None


def _avg_peak_hour(session, city_code: str, longitude: Optional[float]) -> Optional[float]:
    """Sehir bazli gecmis ortalama peak saati (yerel saat 0-24).

    2026-08-20 HIBRIT: ONCEKI gunlerin gercek peak saatlerinin ortalamasi
    (bugun DAHIL DEGIL — look-ahead yok). En az MIN_PEAK_HISTORY_DAYS gun
    veri gerekir; azsa None doner ve eski 1-dusus kilit kurali calisir.
    """
    from collections import defaultdict
    from datetime import datetime as _dt, timezone as _tz
    from scrapers.metar import city_utc_offset

    rows = (
        session.query(MetarObservation.obs_time, MetarObservation.temp_c)
        .filter(MetarObservation.city_code == city_code)
        .all()
    )
    if not rows:
        return None
    now_ts = time.time()
    today_loc = _dt.fromtimestamp(now_ts, tz=_tz.utc).strftime("%Y-%m-%d")
    by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for obs, temp in rows:
        if obs is None or temp is None:
            continue
        # obs_time naive UTC DateTime'dir — .timestamp() LOKAL tz yorumlar
        # (Istanbul +3 tuzağı, 2026-08-20), replace(tzinfo=utc) sart.
        t = obs.replace(tzinfo=_tz.utc).timestamp()
        off = city_utc_offset(city_code, str(obs)[:10], longitude)
        ld = _dt.fromtimestamp(t + off * 3600, tz=_tz.utc).strftime("%Y-%m-%d")
        if ld >= today_loc:
            continue  # bugunun ve gelecegin verisi tahmine girmez
        by_day[ld].append((t, float(temp)))
    hrs: list[float] = []
    for ld, arr in by_day.items():
        if len(arr) < 3:
            continue  # yarim gun veri — peak saati guvenilmez
        ts_max = max(arr, key=lambda x: x[1])[0]
        off = city_utc_offset(city_code, ld, longitude)
        loc = _dt.fromtimestamp(ts_max + off * 3600, tz=_tz.utc)
        hrs.append(loc.hour + loc.minute / 60.0)
    if len(hrs) < MIN_PEAK_HISTORY_DAYS:
        return None
    return sum(hrs) / len(hrs)


def _stale_threshold_min(session, city_code: str) -> float:
    """Istasyon yayin kadansina gore bayat esigi (2026-08-20 kullanici onayi).

    30dk istasyonlar: 45dk (bir yayin kacirmak = gercek bayat — Toronto).
    60dk (saatlik) istasyonlar: 90dk — 45dk esigi saatlik istasyonlarda HER
    SAAT tetikleniyordu (Wuhan/Chongqing/Qingdao/Busan 60dk kadans; 75dk eski
    gozlem normal, 11:00 yayini henuz yok). Kadans metar_observations'taki
    gozlem araliklarinin medyanindan cikarilir.
    """
    rows = [
        r[0].replace(tzinfo=timezone.utc).timestamp()
        for r in session.query(MetarObservation.obs_time)
        .filter(MetarObservation.city_code == city_code)
        .order_by(MetarObservation.obs_time.desc())
        .limit(40)
        .all()
        if r[0] is not None
    ]
    if len(rows) < 4:
        return 45.0
    rows.sort()
    gaps = sorted(b - a for a, b in zip(rows, rows[1:]) if b - a >= 300.0)
    if not gaps:
        return 45.0
    median_min = gaps[len(gaps) // 2] / 60.0
    return 90.0 if median_min >= 55.0 else 45.0


def _hours_until_close(market) -> float:
    """Kapanis (target+12h) ile simdi arasindaki saat."""
    if not market or not market.target_date:
        return 0.0
    td = market.target_date
    if getattr(td, "tzinfo", None) is None:
        td = td.replace(tzinfo=timezone.utc)
    close = td + timedelta(hours=CLOSE_HOURS)
    now = datetime.now(timezone.utc)
    return max(0.0, (close - now).total_seconds() / 3600.0)


def _existing_metar_bet(session, market_id: str) -> Optional[Bet]:
    """Bu markete daha once METAR-peak bet'i acildi mi?"""
    return (
        session.query(Bet)
        .filter(
            Bet.market_id == market_id,
            Bet.order_id.like("metar_%"),
            Bet.status.in_(("placed", "active")),
        )
        .first()
    )


def _metar_bets_opened_today(session, now: datetime) -> int:
    """Bugunku (UTC) acilan METAR-peak bet sayisi.

    2026-08-21 cap semantigi: status bagimsiz — gun icinde kapanan/settled
    olan da cap'e sayilir ("gunluk acilan bet sayisi", backtest ile ayni).
    """
    return (
        session.query(Bet)
        .filter(
            Bet.order_id.like("metar_%"),
            Bet.placed_at >= datetime(now.year, now.month, now.day, 0, 0, 0),
        )
        .count()
    )


def _close_wrong_bucket_bets(session, city_code: str, target_date, winning_bucket: float) -> int:
    """Kazanan bucket belli oldugunda, o sehrin o gunu icin kazanan bucket
    DISINDAKI tum acik betleri canli fiyattan kapatir.

    Kullanici karari 2026-08-16 (3. adim): "T-2 oncesi actigimiz bet kazanan
    bucket'ta degilse onu kapatiyoruz". 16 Agu ornegi: 75 acik spread bet,
    sadece 6'si kazanan bucket'ta — 69 yanlis bet settlement'a kadar acik
    kaldi ve tam stake kaybedilecekti. Bu fonksiyon peak kilitlendiginde
    yanlis bucket'lari canli fiyattan satip kazanci/zarari erkenden gercekler.

    Sadece TUM acik betler (spread + metar) taranir; kazanan bucket'ta olanlar
    TUTULUR. Kapatilan her bet icin close_bet_for_rotation (canli fiyattan
    satis, portfolio kredisi) kullanilir.

    Returns: kapatilan bet sayisi.
    """
    from executor.bet_placer import BetPlacer

    if not target_date:
        return 0
    day = target_date.date().isoformat() if hasattr(target_date, "date") else str(target_date)[:10]

    candidates = (
        session.query(Bet, WeatherMarket)
        .join(WeatherMarket, WeatherMarket.id == Bet.market_id)
        .filter(
            WeatherMarket.city_code == city_code,
            WeatherMarket.target_date.isnot(None),
            # 2026-08-18 audit fix (M12): bucket mantigi yalnizca temperature_max
            # RANGE marketleri icindir (kullanici "tam bucket a aciyoruz").
            # temperature_min / HIGH / LOW marketlerinin kazanan bucket'i yoktur;
            # METAR peak bucket'i ile karsilastirilarak satilmazlar.
            WeatherMarket.metric == "temperature_max",
            WeatherMarket.market_type == "RANGE",
            Bet.status.in_(("placed", "open", "active", "pending")),
        )
        .all()
    )
    closed = 0
    placer = BetPlacer()
    for bet, wm in candidates:
        if wm.target_date.date().isoformat() != day:
            continue
        if wm.threshold is None:
            continue
        if int(float(wm.threshold) + 0.5) == winning_bucket:
            continue  # kazanan bucket TUTULUR
        try:
            live = float(wm.yes_price) if wm.yes_price else float(bet.entry_price or 0)
        except (TypeError, ValueError):
            continue
        ok = placer.close_bet_for_rotation(bet, max(0.01, min(0.99, live)), session)
        if ok:
            closed += 1
            logger.info(
                "metar_peak: KAPATILDI bet#%s %s %sC (kazanan %sC)",
                bet.id,
                bet.city,
                wm.threshold,
                winning_bucket,
            )
    if closed:
        logger.info("metar_peak: %s sehirde yanlis bucket betleri kapatildi (%s)", city_code, closed)
    return closed


def _open_metar_bet(session, market: WeatherMarket, peak_temp: float) -> Optional[Bet]:
    """Bir markete METAR-peak tek esik YES bet acar."""
    from utils.formulas import bet_shares, polymarket_fee_from_stake

    entry = float(market.yes_price or 0)
    # METAR-peak: kazanan bucket'i biliyoruz, fiyat 0.95'e kadar girilebilir.
    # Backtest: 12 bet %91.7, entry 0.05-0.89 (8 bet 0.30+). 0.50 siniri
    # kazananlari kaciriyordu (0.52, 0.89). Optimum: 0.95 (2026-08-16).
    max_entry = 0.95
    # 2026-08-17 MIN_ENTRY: 0.01-0.03 longshot'lari elemek icin. Canli veride
    # entry<0.10 24 bet -$39.90 kaybetti (piyasa o bucket'e ~%1 sans veriyor
    # = METAR tespiti yanlis). entry>=0.10 6 bet +$7.06 kazandi.
    if not (MIN_ENTRY <= entry < max_entry):
        neden = f"giris={entry:.3f} disinda [MIN_ENTRY={MIN_ENTRY}, max={max_entry}]"
        if entry >= max_entry:
            neden = f"fiyat cok pahali ({entry:.3f} >= {max_entry})"
        elif entry < MIN_ENTRY:
            neden = f"piyasa supheli ({entry:.3f} < MIN_ENTRY={MIN_ENTRY})"
        logger.info(
            "metar_peak: %s %sC giris=%.3f [MIN_ENTRY=%.2f, max_entry=%.2f], atlandi (%s)",
            market.city,
            market.threshold,
            entry,
            MIN_ENTRY,
            max_entry,
            neden,
        )
        from utils.activity_log import log_event

        log_event("bet_blocked", str(market.city), f"peak bucket {float(market.threshold):.1f}C: {neden}")
        return None

    # 2026-08-18 audit fix (C3): fantom/stale fiyat guardi. Canli METAR-peak
    # betleri (30 bet, -$32.84) icin entry=0.01-0.03 longshot'lar tamamen
    # kayipti. MIN_ENTRY uzerindekiler de CLOB canli kottan %15'ten fazla
    # sapiyorsa reddedilir. CLOB hataliysa bet asla engellenmez.
    try:
        from utils.clob_live import live_quote_for_market, price_is_stale

        # getattr: WeatherMarket.raw_data mypy'de Column[str]; runtime'da str|None.
        # live_quote_for_market(str|None) bekler — Column tipini asla gecirmeyiz.
        _tok, live_ask, live_bid = live_quote_for_market(getattr(market, "raw_data", None))
        if _tok is not None and live_ask is not None and price_is_stale(entry, live_ask, live_bid):
            logger.warning(
                "metar_peak: STALE PRICE GUARD %s DB yes=%.4f vs CLOB ask=%.4f (bid=%.4f) - bet refused",
                market.id,
                entry,
                live_ask,
                live_bid,
            )
            from utils.activity_log import log_event

            log_event(
                "bet_blocked",
                str(market.city),
                f"STALE PRICE GUARD: DB={entry:.3f} vs CLOB ask={live_ask:.3f} (fark > %15)",
            )
            return None
    except Exception as exc:  # never block betting on CLOB failure
        logger.debug("metar_peak: live price guard skipped for %s: %s", market.id, exc)

    # 2026-08-20 STALE GUARD YEDEGI (orderbook.db — CLOB'dan BAGIMSIZ):
    # Toronto 19 Agu'da CLOB istegi dusunce guard atlandi, bayat 25C'ye
    # girildi (-$6). Orderbook.db botun kendi 5dk'lık GERCEK ask okumalaridir;
    # son okuma DB fiyatindan %15+ sapiyorsa fiyat bayattir, bet reddedilir.
    try:
        import os as _os
        import sqlite3 as _sq

        _ob = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "orderbook.db")
        if _os.path.exists(_ob):
            _oc = _sq.connect(_ob, timeout=5)
            _last = _oc.execute(
                "SELECT best_ask FROM orderbook_snapshots WHERE market_id=? AND best_ask IS NOT NULL "
                "ORDER BY snapshot_time DESC LIMIT 1",
                (str(market.id),),
            ).fetchone()
            _oc.close()
            if _last and _last[0] is not None:
                _ob_ask = float(_last[0])
                if 0 < _ob_ask < 1 and abs(entry - _ob_ask) / _ob_ask > 0.15:
                    from utils.activity_log import log_event

                    log_event(
                        "bet_blocked",
                        str(market.city),
                        f"STALE GUARD(ob): DB={entry:.3f} vs orderbook ask={_ob_ask:.3f} (fark > %15)",
                    )
                    return None
    except Exception:  # noqa: BLE001 — yedek guard asla beti durdurmaz
        pass

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    cash = float(pf.cash_balance) if pf else 0.0
    use_stake = min(METAR_STAKE, max(0.0, cash))
    if use_stake <= 0:
        logger.warning("metar_peak: %s %sC nakit yetersiz (cash=%.2f)", market.city, market.threshold, cash)
        return None

    # 2026-08-19 kullanici karari: DERINLIK SINIRI KALDIRILDI. CLOB ask
    # derinligine gore stake kucultme 0.00/0.40 USD'lik cop betler uretiyordu
    # (bos defter -> limit 0). Betler her zaman SABIT stake (3.0 USD) ile acilir.

    fill_price = max(0.01, min(0.99, round(entry, 4)))
    shares = bet_shares(use_stake, fill_price)
    fee_rate = bot_config.strategy.current_fee_rate
    entry_fee = polymarket_fee_from_stake(use_stake, fill_price, fee_rate)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = int(now.timestamp())

    bet = Bet(
        market_id=str(market.id),
        city=market.city,
        city_code=market.city_code or "",
        side="YES",
        amount=use_stake,
        stake_amount=use_stake,
        price=fill_price,
        entry_price=fill_price,
        shares=shares,
        current_price=fill_price,
        pnl=0.0,
        unrealized_pnl=0.0,
        fair_value=fill_price,
        expected_value=0.0,
        strike_temp=market.threshold,
        status="placed",
        realized_pnl=0.0,
        order_id=f"metar_{market.id}_{ts}",
        entry_fee=entry_fee,
        placed_at=now,
        covered_fraction=0.0,
    )
    session.add(bet)
    # 2026-08-18 audit fix (C1): stake daha once HIC dusulmuyordu -> kagit nakit
    # ve exposure yanlis. bet_placer/spread_placer gibi burada da debit edilir.
    try:
        from utils.accounting import debit_stake

        debit_stake(session, use_stake, f"metar_peak {market.city} {market.threshold}C")
    except ValueError as exc:
        logger.warning("metar_peak: %s %sC nakit dusulemedi (%s) - bet iptal", market.city, market.threshold, exc)
        session.rollback()
        return None
    logger.info(
        "metar_peak: BET acildi %s %sC peak=%.1f giris=%.3f stake=%.2f",
        market.city,
        market.threshold,
        peak_temp,
        fill_price,
        use_stake,
    )
    return bet


def _merged_day_rows(
    city_code: str, day: str, utc_offset_hours: float, fresh: list[tuple[int, float]]
) -> list[tuple[int, float]]:
    """Taze METAR fetch + kalici arsiv birlestirilir (kumulatif, monoton).

    2026-08-19 fix: aviationweather.gov yanitlari donguden donguye degisebiliyor
    (eksik gozlem) -> ayni gunun tespit edilen max'i orn. 24C/25C arasi
    saliniyordu; kilitli peak geri dusup aktar mekanizmasini bos yere
    tetikliyordu. Arsiv (metar_observations) bir kez gorulen gozlemi asla
    kaybetmez; birlestirilmis satirlarda kumulatif max yalnizca ARTAR.
    Pencere: sehirin YEREL gunu (yerel 00:00 = UTC 00:00 - offset) —
    dunun aksam gozlemleri (eski UTC-gun filtresinin artiklari dahil) disarida kalir.
    """
    from datetime import timedelta

    from database.models import MetarObservation

    y, mo, d = int(day[:4]), int(day[5:7]), int(day[8:10])
    start_naive = datetime(y, mo, d) - timedelta(seconds=utc_offset_hours * 3600)
    end_naive = start_naive + timedelta(days=1)
    merged: dict[int, float] = {e: t for e, t in fresh}
    try:
        with get_session() as session:
            rows = (
                session.query(MetarObservation.obs_time, MetarObservation.temp_c)
                .filter(
                    MetarObservation.city_code == city_code,
                    MetarObservation.obs_time >= start_naive,
                    MetarObservation.obs_time < end_naive,
                )
                .all()
            )
        for obs_dt, temp in rows:
            if obs_dt is None or temp is None:
                continue
            ep = int(obs_dt.replace(tzinfo=timezone.utc).timestamp())
            merged[ep] = max(merged.get(ep, -999.0), float(temp))
    except Exception as exc:  # noqa: BLE001 — arsiv okunamazsa taze veri yeterli
        logger.debug("metar arsiv merge fail %s: %s", city_code, exc)
    return sorted(merged.items())


def collect_metar_archive(session) -> int:
    """Tum sehirlerin bugunku METAR gozlemlerini arsivler (bet mantigindan BAGIMSIZ).

    2026-08-18 kullanici karari: "24 saat veri topla bundan sonra". Eski akis
    yalnizca ACIK marketi olan (ve kapanisa >2h kalan) sehirleri cekiyordu ->
    aksam 22:00 sonrasi toplama duruyordu; 16-17 Agu arsivi ~21:00'de kesildi,
    13 sehir/gun peak kilitlense bile MIN_HOURS_BEFORE_CLOSE yuzunden bet
    kacirdi. Bu fonksiyon her 30dk'da (metar_loop) TUM sehirlerin bugunku
    gozlemlerini ceker ve idempotent arsive yazar; bet acmaz, market durumuna
    bakmaz, kapanis saati filtrelemez.

    2026-08-19: metar_loop 10dk'ya indirildi ama METAR yayinlari ~30dk'da bir
    guncellenir — arsivi her 10dk'da toplamak aviationweather.gov'u bos yere
    yorar (19 Agu aksami 17 timeout goruldu). Zaman kapisi: son toplamadan
    25 dk gecmeden atlanir (arsiv idempotent, kayip yok).
    """
    from database.models import MetarObservation

    global _LAST_ARCHIVE_RUN
    if time.monotonic() - _LAST_ARCHIVE_RUN < 25 * 60:
        return 0  # 10 dk'lik dongude her 3. kosuda bir toplanir
    _LAST_ARCHIVE_RUN = time.monotonic()

    codes = [r[0] for r in session.query(MetarObservation.city_code).distinct().order_by(MetarObservation.city_code)]
    # Arsivde henuz gozlem olmayan sehirler de toplanmali -> weather_markets.
    mk = [
        r[0]
        for r in session.query(WeatherMarket.city_code)
        .filter(WeatherMarket.city_code.isnot(None), WeatherMarket.city_code != "")
        .distinct()
    ]
    all_codes = sorted(set(codes) | set(mk))
    if not all_codes:
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scrapers.metar import archive_metar_observations, fetch_metar_day

    def _one(icao: str) -> int:
        try:
            rows = fetch_metar_day(icao, today)
            if rows:
                return archive_metar_observations(icao, icao, rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("metar arsiv fetch fail %s: %s", icao, exc)
        return 0

    added = 0
    # 2026-08-19: 8 -> 4 worker — aviationweather.gov'u bogmamak icin
    # (19 Agu gece 17 'Read timed out' + bot kendi istek yukunu yigdi).
    # 2026-08-19: as_completed timeout KALDIRILDI — 60s aksam yavasliginda
    # asiliyor, TimeoutError run'u olduruyordu (loop 5 dk'da tekrar basliyor,
    # cakisan run'lar uretiyordu). Her future kendi retry'siyle (4x12s) sinirli.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_one, c) for c in all_codes]
        for fut in as_completed(futs):
            try:
                added += fut.result() or 0
            except Exception as exc:  # noqa: BLE001
                logger.warning("metar arsiv worker fail: %s", exc)
    return added


def run_metar_peak_bets() -> int:
    """Simdiki gunun acik marketlerine, METAR zirvesi kilitlenenlerde tek esik bet acar."""
    from scrapers.metar import fetch_metar_day, detect_peak

    opened = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date().isoformat()

    with get_session() as session:
        # 2026-08-18 kullanici karari: 24 saat kesintisiz METAR arsivi —
        # bet acma mantigindan bagimsiz (aksam kesilmesi duzeltildi).
        collect_metar_archive(session)

        # 2026-08-21 kullanici karari: gunluk METAR-peak bet cap'i.
        # Backtest cap-sweep (canli hibrit + cikis gecikmeli): cap7 +$539
        # (%78.5), cap12 +$834 (%82.6) — cap arttikca profit ve winrate
        # birlikte yukseldi, cap12 EN KARLI. Bugunku (UTC) acilan METAR
        # betleri sayilir — status bagimsiz: gun icinde kapanan/settled olan
        # da cap'e sayilir (backtest semantigi: "gunluk acilan bet sayisi").
        # Cap dolunca YENI bet acilmaz; yanlis-bucket kapatma (aktar/zincir)
        # cap'tan bagimsiz devam eder.
        metar_max_bets = int(getattr(bot_config.strategy, "metar_peak_max_bets_per_day", 12) or 12)
        metar_opened_today = _metar_bets_opened_today(session, now)
        cap_logged = False

        # 2026-08-18 kullanici karari: "Metar betleri acilirken bias a gerek
        # yok, nasil olsa peak tespit edilmis oluyor" -> bias-top sehir
        # filtresi KALDIRILDI, TUM sehirlerin acik marketlerine bakilir.
        # Acik marketler (status=open), SADECE BUGUN, TUM sehirler.
        # 2026-08-19 fix: "bugun ve gelecek gun" filtresi yarinin marketlerini
        # de isliyordu; yarinin YEREL gun penceresi gec saatlerde acildigi icin
        # (HK +8 -> 19:00 TSİ) ilk gece gozlemleriyle (24-25C) SAHTE peak
        # kilitleniyordu. Yarinin peak'i bugun bilinemez — sadece bugun.
        markets = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.target_date.isnot(None),
                WeatherMarket.target_date.like(f"{today}%"),
                WeatherMarket.city_code.isnot(None),
                WeatherMarket.city_code != "",
                WeatherMarket.latitude != 0,
                # BUGFIX 2026-08-18 (kullanici "tam bucket a aciyoruz"): peak
                # mantigi EN YUKSEK sicaklik TAM bucket (RANGE) marketi icindir.
                #   - temperature_min marketine round(peak) ile bet acilamaz
                #     (canli 6 bet 0.01 entry, 5 lost -$11.55 — London/Paris/
                #     Shanghai/Hong Kong/Seoul/Tokyo lowest marketleri).
                #   - HIGH/LOW (or-above/or-below) marketleri TAM BUCKET DEGIL:
                #     canli 5 bet (3 HIGH + 2 LOW) hepsi 0.01 entry, 4 lost.
                # Sadece RANGE + temperature_max marketlerine bet acilir.
                WeatherMarket.metric == "temperature_max",
                WeatherMarket.market_type == "RANGE",
            )
            .all()
        )
        if not markets:
            return 0

        # Sehir -> market gruplama (her sehir icin en iyi bucket adayini sec)
        # METAR fetch'leri PARALEL cekilir — 40 sehir tek tek cekilirse
        # 60s _FETCH_TIMEOUT'a dusup "METAR poll timed out" oluyor, peak'ler
        # kaciyor (2026-08-17 bugfix). ThreadPoolExecutor ile ~3-5s'de biter.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        candidates = []
        for m in markets:
            if _hours_until_close(m) < MIN_HOURS_BEFORE_CLOSE:
                continue
            # 2026-08-21: kara liste KALDIRILDI (METAR_PEAK_BLACKLIST bos) —
            # tum sehirler adaydir. (2026-08-20'de VHHH/ZGSZ/KBKF/KATL/KSEA/
            # KSFO/NZWN burada atlaniyordu; kullanici riski kabul etti.)
            if _existing_metar_bet(session, str(m.id)):
                continue
            day = m.target_date.date().isoformat() if m.target_date else today
            # 2026-08-18 audit fix (M3): gercek saat dilimi (zoneinfo + DST),
            # round(lon/15) nominal degil. China +8, Seoul +9, London BST +1.
            from scrapers.metar import city_utc_offset

            utc_offset = city_utc_offset(m.city_code, day, m.longitude)
            candidates.append((m, day, utc_offset))

        # Ilk olarak sadece benzersiz (city_code, day) icin METAR cek (cache'li)
        unique: dict[tuple[str, str], tuple[WeatherMarket, str, float]] = {}
        for m, day, off in candidates:
            unique.setdefault((m.city_code, day), (m, day, off))

        from scrapers.metar import fetch_metar_day, archive_metar_observations

        def _fetch_one(item):
            m, day, off = item
            try:
                # 2026-08-19: YEREL gun penceresi — batili sehirlerde dunun
                # aksami bugunun kilidi sanilmasin (kullanici: "NY nasil
                # kitledi, bu dunun mu").
                rows = fetch_metar_day(m.city_code, day, utc_offset_hours=off)
                archive_metar_observations(m.city_code, m.city or "", rows)
                # 2026-08-19: arsiv ile birlestir (API donguden donguye eksik
                # gozlem donebilir -> peak salinimi; arsiv kumulatiftir).
                return m.city_code, day, _merged_day_rows(m.city_code, day, off, rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("metar_peak: METAR fetch fail %s: %s", m.city_code, exc)
                return m.city_code, day, None

        metar_rows = {}
        # 2026-08-19: as_completed timeout KALDIRILDI — 60s asilinca TimeoutError
        # tum run'u oldurup (event'ler YAZILMADAN) loop'un 5 dk'da yeniden
        # baslamasina yol aciyordu; arka planda yasayan eski thread'lerle
        # cakisan run'lar olusuyordu. Her future kendi retry'siyle sinirli.
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_one, item): item for item in unique.values()}
            for fut in as_completed(futs):
                code, day, rows = fut.result()
                metar_rows[(code, day)] = rows

        # Paralele cekilen verilerle peak kontrolu + bet ac
        from utils.activity_log import log_event, update_peak_watch

        last_closed: dict[tuple[str, str], int] = {}
        # 2026-08-19: aktivite akisi gurultusu duzeltildi —
        # peak_found sehir basina 1 kez, "market DB'de yok" yalnizca GERCEKTEN
        # eslesen market yoksa, bet_closed yalnizca GERCEKTEN kapatma varsa.
        peaks_logged: dict[tuple[str, str], tuple[int, str]] = {}  # (code, day) -> (bucket, sehir)
        matched_markets: set[tuple[str, str]] = set()  # bucket marketi eslesen (code, day)
        watch_rows: dict[str, dict] = {}
        # 2026-08-20: bayat-METAR alarmi sehir-gun basina 1 kez (market basina
        # degil) — kullanici "islem yapmadi" sanmasin, log gurultusu olmasin.
        stale_logged: set[tuple[str, str]] = set()
        for m, day, utc_offset in candidates:
            day_rows = metar_rows.get((m.city_code, day)) or []
            if not day_rows:
                # 2026-08-21 (A): ilk PARALEL fetch bos/hata dondu — sessiz
                # atlamak yerine o sehrin METAR'ini DERHAL yeniden cek
                # (aviationweather gecikmeli yayinlar; Buenos Aires 19 Agu
                # 17:07->20:34 sessizligi buydu — hic deneme yoktu). Yeniden
                # cekim de bos/basarisizsa sehir-gun basina 1 kez logla + atla.
                _fresh = fetch_metar_day(m.city_code, day, utc_offset_hours=utc_offset)
                if _fresh:
                    archive_metar_observations(m.city_code, m.city or "", _fresh)
                    _merged = _merged_day_rows(m.city_code, day, utc_offset, _fresh)
                    if _merged:
                        day_rows = _merged
                    else:
                        if (m.city_code, day) not in stale_logged:
                            stale_logged.add((m.city_code, day))
                            log_event(
                                "bet_blocked", str(m.city), "METAR: ilk fetch bos, yeniden cekim de bos - yeni bet yok"
                            )
                        continue
                else:
                    if (m.city_code, day) not in stale_logged:
                        stale_logged.add((m.city_code, day))
                        log_event(
                            "bet_blocked", str(m.city), "METAR: ilk fetch bos, yeniden cekim basarisiz - yeni bet yok"
                        )
                    continue
            # 2026-08-20 BAYAT VERI KORUMASI (Toronto 19 Agu: bayat seriyle
            # 25C'ye girildi, piyasa 27'yi biliyordu, -$6 uctu): aviationweather
            # gozlemleri gecikmeli yayinlayabiliyor. Son gozlem 45dk'dan eskiyse
            # kilit/bet ATLANIR — gecikme gecicidir, 30dk sonraki dongude taze
            # veri gelir (kullanici: "90 cok uzun, o zamana kadar bet acar").
            import time as _time

            key_stale = (m.city_code, day)
            last_obs_age_min = (_time.time() - day_rows[-1][0]) / 60.0
            # 2026-08-20 kadans-bilgili esik: 60dk istasyonlarda 90dk,
            # 30dk istasyonlarda 45dk (saatlik istasyonlarda sahte alarm).
            stale_lim = _stale_threshold_min(session, m.city_code)
            # 2026-08-20 kullanici onayi: bayat veri YALNIZCA yeni bet acmayi
            # engeller; aktar (yanlis bucket kapatma) bayattan bagimsiz calisir
            # — elimizdeki en iyi peak ile kapatmak guvenlidir (asla gozlenen
            # max'in USTUNDE kapatmayiz).
            stale_skip = False
            if last_obs_age_min > stale_lim:
                # 2026-08-20 kullanici: "duzeltmeye calismadi" — pasif atlamak
                # yerine o sehrin METAR'ini DERHAL yeniden cekmeyi dene; taze
                # veri gelirse bet mantigi devam eder, gelmezse YENI BET yok
                # (aviationweather istasyon yayinini geciktirebiliyor).
                # 2026-08-21 (B): 1 deneme yerine 3 deneme, aralarda 3 sn
                # bekleme — aviationweather tek cagriyi bayat donebiliyor;
                # kisa retry taze gozlem gelmesine yetiyor.
                _fresh_ok = False
                for _attempt in range(3):
                    _fresh = fetch_metar_day(m.city_code, day, utc_offset_hours=utc_offset)
                    if _fresh:
                        archive_metar_observations(m.city_code, m.city or "", _fresh)
                        _merged = _merged_day_rows(m.city_code, day, utc_offset, _fresh)
                        if _merged and (_time.time() - _merged[-1][0]) / 60.0 <= stale_lim:
                            day_rows = _merged  # taze veri geldi — devam et
                            _fresh_ok = True
                            break
                    if _attempt < 2:
                        _time.sleep(3)  # yeniden denemeden once kisa bekleme
                if not _fresh_ok:
                    stale_skip = True
                    # 3 deneme de bayat/basarisiz: sehir-gun basina 1 kez logla
                    if key_stale not in stale_logged:
                        stale_logged.add(key_stale)
                        from utils.activity_log import log_event as _le

                        _le(
                            "bet_blocked",
                            str(m.city),
                            f"BAYAT METAR: 3 yeniden cekim de bayat "
                            f"({last_obs_age_min:.0f} dk >= {stale_lim:.0f}) - yeni bet yok",
                        )
            # 2026-08-20 HIBRIT (kullanici onayi): sehir bazli gecmis ortalama
            # peak saati biliniyorsa, o saatten ONCE bet acilmaz (bekleme);
            # saat gelince cur_max'a ERKEN giris adayi olur (dusus beklenmez).
            # Fiyat EARLY_MAX_PRICE ustundeyse (piyasa bucket'i biliyor)
            # 1-dusus kilidini bekleriz. Backtest: hibrit 0.50 en iyi.
            avg_hour = _avg_peak_hour(session, m.city_code, m.longitude)
            early_attempt = avg_hour is not None
            # 2026-08-19 PEAK TAKIBI (kullanici): su anki sicaklik + yon +
            # durum (kilitli/takip/bekleme) — dashboard'da gorunur.
            cur_t = day_rows[-1][1]
            prev_t = day_rows[-2][1] if len(day_rows) >= 2 else None
            if prev_t is not None:
                direction = "UP" if cur_t > prev_t else ("DOWN" if cur_t < prev_t else "FLAT")
            else:
                direction = "-"
            last_local_hour = datetime.fromtimestamp(day_rows[-1][0] + utc_offset * 3600, tz=timezone.utc).hour
            cur_max = max(t for _, t in day_rows)
            # 2026-08-20 kullanici: "erken girise [kilit] ekleme, ne zaman
            # peak tespit olursa KESIN o zaman kilit olsun" — detect_peak HER
            # modda calisir; gercek 1-dusus kilidi varsa durumda KILIT gorunur,
            # erken giris ancak kilit yokken gosterilir (aktar yine cur_max'tan).
            pk_lock, cf_lock = detect_peak(day_rows, utc_offset_hours=utc_offset)
            if early_attempt and avg_hour is not None:
                local_now_hr = (time.time() + utc_offset * 3600.0) % 86400.0 / 3600.0
                if local_now_hr < avg_hour:
                    # tahmini peak saati henuz gelmedi: bekleriz (kilit kurali
                    # da calismaz — erken giris saati onceliklidir).
                    watch_rows[str(m.city)] = {
                        "city": str(m.city),
                        "cur": cur_t,
                        "prev": prev_t,
                        "direction": direction,
                        "status": f"bekleme (peak ~{avg_hour:.0f}:00)",
                        "peak": None,
                        "day": day,
                        # 2026-08-21 kullanici: peak_watch dogu->bati (lon DESC)
                        "lon": _city_lon(m),
                    }
                    continue
                peak = cur_max
                confirmed = True
            else:
                peak, confirmed = pk_lock, cf_lock
            if cf_lock and pk_lock is not None:
                # 2026-08-19 kullanici: "sicaklik yukseliyor ama kilitli gorunuyor"
                # — kilitten SONRA zirve asildiysa bunu GOSTER (aktar devrede).
                if cur_max > pk_lock:
                    status = f"zirve asildi -> {cur_max:.1f}C (aktar)"
                else:
                    status = f"kilitli peak={pk_lock:.1f}C"
            elif confirmed and peak is not None:
                if early_attempt:
                    status = f"erken giris (max {cur_max:.1f}C, ~{avg_hour:.0f}:00)"
                else:
                    status = f"takip (peak={peak:.1f}C, kilit bekliyor)"
            elif last_local_hour >= 13:
                status = "takip (13+ sonrasi)"
            else:
                status = "bekleme (13 oncesi)"
            watch_rows[str(m.city)] = {
                "city": str(m.city),
                "cur": cur_t,
                "prev": prev_t,
                "direction": direction,
                "status": status,
                # gercek kilit varsa KILITLI deger yazilir (2026-08-20)
                "peak": float(pk_lock)
                if cf_lock and pk_lock is not None
                else (float(peak) if confirmed and peak is not None else None),
                # 2026-08-19: gun kapaninca ekrandaki kilitler silinir (kullanici)
                "day": day,
                # 2026-08-21 kullanici: peak_watch dogu->bati siralanir (lon DESC)
                "lon": _city_lon(m),
            }
            if not confirmed or peak is None:
                continue  # zirve henuz kilitlenmedi (1 dusus kurali, 2026-08-18)
            # 2026-08-20 HIBRIT ESIGI: erken giris adayi PAHALI ise (bu
            # bucket'in piyasa fiyati > EARLY_MAX_PRICE) piyasa bucket'i zaten
            # fiyatlamis demektir -> 1-dusus kilidini bekle. Backtest: ucuz
            # fiyatta erken giris, pahali fiyatta kilit kesinligi.
            if early_attempt:
                _bkt = next(
                    (
                        mm
                        for mm in markets
                        if mm.city_code == m.city_code
                        and mm.metric == "temperature_max"
                        and mm.market_type == "RANGE"
                        and mm.target_date is not None
                        and mm.target_date.date().isoformat() == day
                        and mm.threshold is not None
                        and int(float(mm.threshold) + 0.5) == int(cur_max + 0.5)
                    ),
                    None,
                )
                _mkt_price = float(_bkt.yes_price) if _bkt is not None and _bkt.yes_price is not None else 0.0
                if _mkt_price > EARLY_MAX_PRICE:
                    pk_l, cf_l = detect_peak(day_rows, utc_offset_hours=utc_offset)
                    if not cf_l or pk_l is None:
                        continue  # pahali + kilit yok -> bekle
                    peak = pk_l
            # 2026-08-18 kullanici: "20 21 22 22 21 diyorsa ikinci 21 ve altini
            # bekleme 22 ye bet ac, daha sonra 23 e cikarsa 22 yi kapa 23 e ac."
            # Zirve ASILDIYSA (cur_max > kilitli peak) eski bucket betleri
            # yanlis: DERHAL kapat VE yeni zirvenin bucket'ina TEKRAR bet ac.
            # Zincir (22->23->24) her dongude calisir: kazanan bucket
            # degistiginde kapatma yeniden cagrilir (last_closed guard).
            cur_max = max(t for _, t in day_rows)
            winner_val = float(cur_max) if cur_max > peak else peak
            winner_bucket = int(winner_val + 0.5) if winner_val >= 0 else int(winner_val - 0.5)
            key = (m.city_code, day)
            # 2026-08-19 AKTIVITE: bulunan peak sehir basina 1 kez kaydedilir
            # (market basina loglanirsa 9 marketli sehir 9 kere yazar).
            if key not in peaks_logged:
                peaks_logged[key] = (winner_bucket, m.city)
                log_event("peak_found", m.city, f"peak={winner_val:.1f}C bucket={winner_bucket}C")
            if last_closed.get(key) != winner_bucket:
                closed = _close_wrong_bucket_bets(session, m.city_code, m.target_date, winner_bucket)
                last_closed[key] = winner_bucket
                # 2026-08-19: yalnizca GERCEKTEN kapatilan bet varsa logla
                # (eski davranis: her dongude 0 kapatmayla da "kapatildi" yazardi).
                if closed:
                    log_event("bet_closed", m.city, f"yanlis bucketlar kapatildi (kazanan {winner_bucket}C)")
            # 2026-08-20 kullanici onayi: bayat veride aktar calisti ama
            # YENI BET ACILMAZ (Toronto dersi — bayat seriyle yanlis bucket'a
            # girmeyelim).
            if stale_skip:
                continue
            # half-up (2026-08-18 audit): US sehirlerinde esikler float C'dir
            # (F'den donusturulur) — int() truncate yanlis bucket uretir;
            # Austin 35.9C marketi bucket 36'ya karsilik gelir.
            if int(float(m.threshold) + 0.5) != winner_bucket:
                continue  # bu market kazanan bucket degil — diger marketler denenir
            matched_markets.add(key)
            # 2026-08-21 kullanici karari: gunluk METAR-peak cap. Cap dolunca
            # YENI bet acilmaz; aktar/zincir kapatma cap'tan bagimsiz devam
            # eder (yukarida zaten calisti). matched_markets add'den SONRA —
            # market DB'de var, "market yok" uyarisi tetiklenmemeli.
            if metar_opened_today >= metar_max_bets:
                if not cap_logged:
                    cap_logged = True
                    logger.info(
                        "metar_peak: gunluk cap doldu (%d/%d) - yeni bet acilmayacak",
                        metar_opened_today,
                        metar_max_bets,
                    )
                continue
            bet = _open_metar_bet(session, m, winner_val)
            if bet:
                opened += 1
                metar_opened_today += 1
                log_event(
                    "bet_opened",
                    str(m.city),
                    f"{winner_bucket}C giris=${bet.entry_price:.3f} stake=${bet.stake_amount:.2f}",
                )
        # 2026-08-19 OTOMATIK UYARI (kullanici: "bunu onleyecek/kontrol edecek
        # bir sey yap"): kilitli peak'in bucket marketi DB'de YOKSA uyar.
        # Yalnizca HICBIR market eslesmeyen (code, day) icin, sehir basina 1 kez.
        # Ama: bucket'ta zaten ACIK metar beti varsa ya da esik baska turde
        # (HIGH/LOW/temperature_min) mevcutsa uyari yazilmaz — bunlar tasarim
        # geregi atlanir, "market yok" degildir (2026-08-19 yanlis uyari fix).
        for key, (bucket, city_name) in peaks_logged.items():
            if key in matched_markets:
                continue
            code, day = key
            bet_thr = [
                r[0]
                for r in session.query(WeatherMarket.threshold)
                .join(Bet, Bet.market_id == WeatherMarket.id)
                .filter(
                    WeatherMarket.city_code == code,
                    WeatherMarket.target_date.like(f"{day}%"),
                    Bet.order_id.like("metar_%"),
                    Bet.status.in_(("placed", "active")),
                )
                .all()
            ]
            if any(t is not None and int(float(t) + 0.5) == bucket for t in bet_thr):
                continue  # bu bucket'ta bet zaten acik — eksik market yok
            any_thr = [
                r[0]
                for r in session.query(WeatherMarket.threshold)
                .filter(
                    WeatherMarket.city_code == code,
                    WeatherMarket.target_date.like(f"{day}%"),
                    WeatherMarket.status == "open",
                )
                .all()
            ]
            if any(t is not None and int(float(t) + 0.5) == bucket for t in any_thr):
                continue  # market var ama RANGE/temperature_max degil — tasarim geregi atlanir
            logger.warning(
                "metar_peak: KILITLI BUCKET MARKETI YOK %s %sC — Polyde yeni acildiysa bir sonraki dongude yakalanir",
                city_name,
                bucket,
            )
            log_event("bet_blocked", city_name, f"bucket={bucket}C: market DB'de yok")

        # 2026-08-19: peak takibi durumu tek seferde yazilir (dashboard).
        if watch_rows:
            update_peak_watch(list(watch_rows.values()))

        session.commit()
    if opened:
        logger.info("metar_peak: %d METAR-peak bet acildi", opened)
    return opened
