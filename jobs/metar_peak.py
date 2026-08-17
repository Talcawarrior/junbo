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
from database.models import Bet, HistoricalCalibration, Portfolio, WeatherMarket
from config.settings import bot_config
from sqlalchemy import func

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
# METAR-peak bet'i icin sehir secimi: bias-top N (en az sapan). Kullanici
# karari 2026-08-16: "bias'ta ilk 40 sehir icin bet acalim".
BIAS_TOP_CITIES = 40


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
        if round(float(wm.threshold)) == winning_bucket:
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
    logger.info(
        "metar_peak: BET acildi %s %sC peak=%.1f giris=%.3f stake=%.2f",
        market.city,
        market.threshold,
        peak_temp,
        fill_price,
        use_stake,
    )
    return bet


def run_metar_peak_bets() -> int:
    """Simdiki gunun acik marketlerine, METAR zirvesi kilitlenenlerde tek esik bet acar."""
    from scrapers.metar import fetch_metar_day, detect_peak

    opened = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date().isoformat()

    with get_session() as session:
        # Bias-top N sehir secimi (en az sapan) — kullanici karari 2026-08-16
        bias_scores: dict[str, float] = {}
        for code, b in (
            session.query(
                HistoricalCalibration.city_code,
                func.abs(HistoricalCalibration.bias),
            )
            .filter(HistoricalCalibration.bias.isnot(None))
            .all()
        ):
            if code:
                bias_scores[code] = bias_scores.get(code, 0.0) + float(b)
        bias_cnt: dict[str, int] = {}
        for (code,) in (
            session.query(HistoricalCalibration.city_code).filter(HistoricalCalibration.bias.isnot(None)).all()
        ):
            if code:
                bias_cnt[code] = bias_cnt.get(code, 0) + 1
        avg_bias: dict[str, float] = {}
        for code in bias_scores:
            if bias_cnt.get(code, 0) > 0:
                avg_bias[code] = bias_scores[code] / bias_cnt[code]
        top_codes = {c for c, _ in sorted(avg_bias.items(), key=lambda kv: kv[1])[:BIAS_TOP_CITIES]}

        # Acik marketler (status=open), bugun ve gelecek gun, bias-top sehirler
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
        markets = [m for m in markets if m.city_code in top_codes]
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
            utc_offset = 0.0
            try:
                utc_offset = round(float(m.longitude) / 15.0)
            except (TypeError, ValueError):
                utc_offset = 0.0
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
        for m, day, utc_offset in candidates:
            day_rows = metar_rows.get((m.city_code, day)) or []
            if not day_rows:
                continue
            peak, confirmed = detect_peak(day_rows, utc_offset_hours=utc_offset)
            if not confirmed or peak is None:
                continue  # zirve henuz kilitlenmedi
            bucket = round(peak)
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
            _close_wrong_bucket_bets(session, m.city_code, m.target_date, bucket)

        session.commit()
    if opened:
        logger.info("metar_peak: %d METAR-peak bet acildi", opened)
    return opened
