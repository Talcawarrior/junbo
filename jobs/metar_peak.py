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

logger = logging.getLogger("SCHEDULER_METAR_PEAK")

# Kapanisa bu kadar saat kala hala zirve kilitlenmediyse bet acilmaz
MIN_HOURS_BEFORE_CLOSE = 4
# METAR stake (kullanici: 1 USD bet ac)
METAR_STAKE = 1.0
# Kapanis = target_date + 12h (24:00 UTC)
CLOSE_HOURS = 12


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


def _open_metar_bet(session, market: WeatherMarket, peak_temp: float) -> Optional[Bet]:
    """Bir markete METAR-peak tek esik YES bet acar."""
    from utils.formulas import bet_shares, polymarket_fee_from_stake

    entry = float(market.yes_price or 0)
    max_entry = float(getattr(bot_config.strategy, "spread_max_entry", 0.50) or 0.50)
    if not (0 < entry < max_entry):
        logger.info("metar_peak: %s %sC giris=%.3f >= max_entry=%.2f, atlandi",
                    market.city, market.threshold, entry, max_entry)
        return None

    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    cash = float(pf.cash_balance) if pf else 0.0
    use_stake = min(METAR_STAKE, max(0.0, cash))
    if use_stake <= 0:
        logger.warning("metar_peak: %s %sC nakit yetersiz (cash=%.2f)",
                       market.city, market.threshold, cash)
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
    logger.info("metar_peak: BET acildi %s %sC peak=%.1f giris=%.3f stake=%.2f",
                market.city, market.threshold, peak_temp, fill_price, use_stake)
    return bet


def run_metar_peak_bets() -> int:
    """Simdiki gunun acik marketlerine, METAR zirvesi kilitlenenlerde tek esik bet acar."""
    from scrapers.metar import fetch_metar_day, detect_peak

    opened = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date().isoformat()

    with get_session() as session:
        # Acik marketler (status=open), bugun ve gelecek gun
        markets = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.target_date.isnot(None),
                WeatherMarket.city_code.isnot(None),
                WeatherMarket.city_code != "",
                WeatherMarket.latitude != 0,
            )
            .all()
        )
        if not markets:
            return 0

        # Sehir -> market gruplama (her sehir icin en iyi bucket adayini sec)
        # Bu dongude her ACIK market icin METAR zirvesi kontrol edilir.
        for m in markets:
            # kapanisa yeterli zaman var mi
            if _hours_until_close(m) < MIN_HOURS_BEFORE_CLOSE:
                continue
            # zaten metar bet'i var mi
            if _existing_metar_bet(session, str(m.id)):
                continue
            # METAR gun verisi
            day = m.target_date.date().isoformat() if m.target_date else today
            try:
                day_rows = fetch_metar_day(m.city_code, day)
            except Exception as exc:  # noqa: BLE001
                logger.warning("metar_peak: METAR fetch fail %s: %s", m.city_code, exc)
                continue
            peak, confirmed = detect_peak(day_rows)
            if not confirmed or peak is None:
                continue  # zirve henuz kilitlenmedi
            bucket = round(peak)
            if float(m.threshold) != bucket:
                continue  # bu market kazanan bucket degil
            bet = _open_metar_bet(session, m, peak)
            if bet:
                opened += 1

        session.commit()
    if opened:
        logger.info("metar_peak: %d METAR-peak bet acildi", opened)
    return opened
