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
from datetime import datetime, timezone, timedelta
from typing import Optional

from database.db import get_session
from database.models import Bet, Portfolio, WeatherMarket
from config.settings import bot_config

# bot_loop._FETCH_TIMEOUT ile ayni deger — circular import onlemek icin burada
_FETCH_TIMEOUT = 60

logger = logging.getLogger("SCHEDULER_METAR_PEAK")

# Kapanisa bu kadar saat kala hala zirve kilitlenmediyse bet acilmaz.
# 2026-08-16: kullanici "peak YEREL saatte olunca gir" dedi -> erken giris.
# 4 saat -> 2 saat (kapanisa cok yakin olanlar riski; peak kilitlenince girilir)
MIN_HOURS_BEFORE_CLOSE = 2
# 2026-08-17 MIN_ENTRY: canli METAR-peak betleri analizi (30 bet, NET -$32.84):
#   entry < 0.10  -> 24 bet, NET -$39.90 (0.01-0.03 longshot'lar TAMAMEN kayip)
#   entry >= 0.10 ->  6 bet, NET +$7.06 (+$5.62 Toronto 0.150, +$1.30 BA 0.685, +$0.14 Taipei 0.945)
# Piyasa bir bucket'i 0.01'e fiyatliyorsa kazanma sansi ~%1 demektir; METAR
# tespiti yanlis. Sadece piyasanin da gercek sans verdigi bucket'a bet acilir.
MIN_ENTRY = 0.10
# METAR stake (kullanici karari 2026-08-16: 1 -> 2 -> 3 USD optimum.
# Backtest: bias-top 40 + tek esik, $3 stake = %91.7, +$120, maxDD $3.2.
# ROI stake'ten bagimsiz ama mutlak kazanc ve risk dengede $3 en iyi.)
METAR_STAKE = 3.0
# Kapanis = target_date + 12h (24:00 UTC)
CLOSE_HOURS = 12
# 2026-08-18 kullanici karari: "Metar betleri acilirken bias a gerek yok,
# nasil olsa peak tespit edilmis oluyor" -> BIAS_TOP_CITIES KALDIRILDI,
# TUM sehirlerin acik RANGE+max marketlerine bakilir.


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
        logger.info(
            "metar_peak: %s %sC giris=%.3f [MIN_ENTRY=%.2f, max_entry=%.2f], atlandi",
            market.city,
            market.threshold,
            entry,
            MIN_ENTRY,
            max_entry,
        )
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
            return None
    except Exception as exc:  # never block betting on CLOB failure
        logger.debug("metar_peak: live price guard skipped for %s: %s", market.id, exc)

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    cash = float(pf.cash_balance) if pf else 0.0
    use_stake = min(METAR_STAKE, max(0.0, cash))
    if use_stake <= 0:
        logger.warning("metar_peak: %s %sC nakit yetersiz (cash=%.2f)", market.city, market.threshold, cash)
        return None

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


def collect_metar_archive(session) -> int:
    """Tum sehirlerin bugunku METAR gozlemlerini arsivler (bet mantigindan BAGIMSIZ).

    2026-08-18 kullanici karari: "24 saat veri topla bundan sonra". Eski akis
    yalnizca ACIK marketi olan (ve kapanisa >2h kalan) sehirleri cekiyordu ->
    aksam 22:00 sonrasi toplama duruyordu; 16-17 Agu arsivi ~21:00'de kesildi,
    13 sehir/gun peak kilitlense bile MIN_HOURS_BEFORE_CLOSE yuzunden bet
    kacirdi. Bu fonksiyon her 30dk'da (metar_loop) TUM sehirlerin bugunku
    gozlemlerini ceker ve idempotent arsive yazar; bet acmaz, market durumuna
    bakmaz, kapanis saati filtrelemez.
    """
    from database.models import MetarObservation

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
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_one, c) for c in all_codes]
        for fut in as_completed(futs, timeout=_FETCH_TIMEOUT or 60):
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

        # 2026-08-18 kullanici karari: "Metar betleri acilirken bias a gerek
        # yok, nasil olsa peak tespit edilmis oluyor" -> bias-top sehir
        # filtresi KALDIRILDI, TUM sehirlerin acik marketlerine bakilir.
        # Acik marketler (status=open), bugun ve gelecek gun, TUM sehirler
        markets = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.target_date.isnot(None),
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
            m, day, _ = item
            try:
                rows = fetch_metar_day(m.city_code, day)
                archive_metar_observations(m.city_code, m.city or "", rows)
                return m.city_code, day, rows
            except Exception as exc:  # noqa: BLE001
                logger.warning("metar_peak: METAR fetch fail %s: %s", m.city_code, exc)
                return m.city_code, day, None

        metar_rows = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_one, item): item for item in unique.values()}
            for fut in as_completed(futs, timeout=_FETCH_TIMEOUT or 60):
                code, day, rows = fut.result()
                metar_rows[(code, day)] = rows

        # Paralele cekilen verilerle peak kontrolu + bet ac
        closed_cities: set[tuple[str, str]] = set()
        for m, day, utc_offset in candidates:
            day_rows = metar_rows.get((m.city_code, day)) or []
            if not day_rows:
                continue
            peak, confirmed = detect_peak(day_rows, utc_offset_hours=utc_offset)
            if not confirmed or peak is None:
                continue  # zirve henuz kilitlenmedi
            # 2026-08-18 kullanici: "ya koy" — kilitli zirve ASILDI ise eski
            # bucket betleri YANLIS demektir. Milan 18 Agu canli ornegi: kilit
            # 31C'de verildi, sonra 32C geldi; eski kod yeni zirvenin 2 dusus
            # ile kilitlenmesini beklerken 31C fiyati 0.0005'e coktu (bet 1452
            # -$3). Yeni kural: cur_max > kilitli peak ise 2 dusus BEKLEMEDEN
            # eski bucket betleri DERHAL kapatilir (yeni cummax kazanan sayilir).
            cur_max = max(t for _, t in day_rows)
            if cur_max > peak:
                if (m.city_code, day) not in closed_cities:
                    _close_wrong_bucket_bets(session, m.city_code, m.target_date, float(cur_max))
                    closed_cities.add((m.city_code, day))
                continue  # kilit bozuldu: eski bucket'a yeni bet acilmaz
            # 2026-08-18 audit fix (C2): round() banker's yerine half-up.
            bucket = int(peak + 0.5) if peak >= 0 else int(peak - 0.5)
            if float(m.threshold) != bucket:
                continue  # bu market kazanan bucket degil
            bet = _open_metar_bet(session, m, peak)
            if bet:
                opened += 1
            # Kullanici karari 2026-08-16 (3. adim): peak kilitlendi, kazanan
            # bucket belli -> o sehirdeki kazanan bucket DISINDAKI tum acik
            # betleri KAPAT (T-2'de yanlis bucket'a acilan spread betleri dahil).
            # Bot daha once kapatmiyordu: 16 Agu'da 75 acik bet, sadece 6'si
            # kazanan bucket'ta, 69 yanlis bet settlement'a kadar acik kaldi.
            # 2026-08-18: kapatma artik kazanan-bucket marketi OLMASA da
            # cagrilir (sehir-gun basina bir kez, closed_cities).
            if (m.city_code, day) not in closed_cities:
                _close_wrong_bucket_bets(session, m.city_code, m.target_date, bucket)
                closed_cities.add((m.city_code, day))

        session.commit()
    if opened:
        logger.info("metar_peak: %d METAR-peak bet acildi", opened)
    return opened
