"""Spread betting placer — ana mod (BETTING_STRATEGY=spread).

Market acilir acilmaz, en son meteo tahmini etrafinda +/- ``spread_radius``
dereceye YES bet acar. Tahmin guncellendiginde (kayan pencere) yeni merkezin
+/-(radius) disinda kalan eski esikler kapatilir.

Akis:
  1. Hedef gun icin (city, metric) basina EN SON meteo tahminini oku
     (weather_forecasts, fetched_at desc).
  2. "Tahmini en yuksek" ilk ``spread_max_cities`` sehri sec.
  3. Her sehir icin center = round(tahmin), hedef esikler [center-radius ..
     center+radius].
  4. Her esik icin: market acik + CANLI yes_price < spread_max_entry +
     gunluk limit asilmadiysa -> YES bet ac (stake spread_stake_usd).
     (2026-08-11: snapshot fiyati degil, run_fetch_markets'in 5 dk'da bir
     guncelledigi canli weather_markets.yes_price kullanilir.)
  5. KAYAN PENCERE: tahmin degisti -> eski pencerede acik olup YENI pencere
     disinda kalan bet'ler kapatilir (eski spreadden cikanlar).

Eski edge-tabanli mod (BETTING_STRATEGY=edge) degistirilmez — bet_placer.py
korunur; bu modul yalnizca spread modunda cagrilir.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.db import get_session
from database.models import (
    OPEN_BET_STATUSES,
    Bet,
    Portfolio,
    WeatherForecast,
    WeatherMarket,
)

logger = logging.getLogger("SPREAD_PLACER")


def _day_range(day):
    lo = datetime(day.year, day.month, day.day, 0, 0, 0)
    hi = datetime(day.year, day.month, day.day, 23, 59, 59, 999999)
    return lo, hi


def _last_forecast_per_city_metric(session, target_day):
    """(city_code, metric) -> en son ensemble tahmini (tum modeller ortalamasi)."""
    from sqlalchemy import func

    lo, hi = _day_range(target_day)
    result = {}
    # En son fetched_at per (city, metric)
    latest = (
        session.query(
            WeatherForecast.city,
            WeatherForecast.metric,
            func.max(WeatherForecast.fetched_at),
        )
        .filter(WeatherForecast.target_date >= lo, WeatherForecast.target_date <= hi)
        .group_by(WeatherForecast.city, WeatherForecast.metric)
        .all()
    )
    for code, metric, fetched_at in latest:
        models = (
            session.query(WeatherForecast)
            .filter(
                WeatherForecast.city == code,
                WeatherForecast.metric == metric,
                WeatherForecast.target_date >= lo,
                WeatherForecast.target_date <= hi,
                WeatherForecast.fetched_at == fetched_at,
            )
            .all()
        )
        vals = [m.predicted_value for m in models if m.predicted_value is not None]
        if vals:
            result[(code, metric)] = sum(vals) / len(vals)
    return result


def _find_market(session, city_name, metric, target_day, thr):
    lo, hi = _day_range(target_day)
    return (
        session.query(WeatherMarket)
        .filter(
            WeatherMarket.city == city_name,
            WeatherMarket.metric == metric,
            WeatherMarket.threshold == thr,
            WeatherMarket.target_date >= lo,
            WeatherMarket.target_date <= hi,
            WeatherMarket.status == "open",
        )
        .first()
    )


def place_spread_bets(target_day, session=None) -> dict:
    """Bir hedef gun icin spread betlerini acar/kapatir.

    Session verilmezse kendi session'ini acar ve COMMIT eder (kritik: aksi
    halde betler DB'ye yazilmaz ve sonraki cagrilar dup olusturur).
    Test icin disaridan session verilebilir (commit caller'da kalir).

    Returns: {"placed": int, "closed": int, "skipped": int, "cities": [...]}
    """
    if session is None:
        with get_session() as s:
            return _place_spread_bets_inner(s, target_day)
    return _place_spread_bets_inner(session, target_day)


def _place_spread_bets_inner(session, target_day) -> dict:
    """Spread betlerini verilen session uzerinde acar/kapatir (commit etmez)."""
    from config.settings import bot_config

    s = bot_config.strategy
    radius = int(getattr(s, "spread_radius", 3) or 3)
    max_cities = int(getattr(s, "spread_max_cities", 15) or 15)
    max_entry = float(getattr(s, "spread_max_entry", 0.30) or 0.30)
    stake = float(getattr(s, "spread_stake_usd", 2.0) or 2.0)
    max_bets = int(getattr(s, "spread_max_bets_per_day", 30) or 30)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date()

    code_name: dict[str, str] = {}
    for c, code in (
        session.query(WeatherMarket.city, WeatherMarket.city_code)
        .filter(WeatherMarket.city_code.isnot(None))
        .distinct()
        .all()
    ):
        if code and c:
            code_name.setdefault(code, c)

    forecasts = _last_forecast_per_city_metric(session, target_day)
    if not forecasts:
        logger.info("spread: no forecasts for %s", target_day)
        return {"placed": 0, "closed": 0, "skipped": 0, "cities": []}

    # En yuksek tahminli sehirler (en sicak) ilk N
    selected = sorted(forecasts.items(), key=lambda kv: -kv[1])[:max_cities]

    # Bugunku acik spread betleri -> gunluk limit
    open_spread = (
        session.query(Bet)
        .filter(
            Bet.status.in_(OPEN_BET_STATUSES),
            Bet.placed_at >= datetime(today.year, today.month, today.day, 0, 0, 0),
        )
        .count()
    )
    remaining = max(0, max_bets - open_spread)

    placed = closed = skipped = 0
    cities_used = set()

    for (code, metric), fval in selected:
        city_name = code_name.get(code)
        if not city_name:
            continue
        if remaining <= 0:
            skipped += 1
            continue
        center = round(fval)
        targets = set(range(center - radius, center + radius + 1))

        # Kayan pencere: bu (city, day) icin acik betlerden yeni pencere
        # disinda kalanlari kapat. Bet'in marketinin target_date'i bu gun
        # olmalidir (placed_at degil).
        lo, hi = _day_range(target_day)
        day_market_ids = {
            str(m.id)
            for m in (
                session.query(WeatherMarket)
                .filter(
                    WeatherMarket.city == city_name,
                    WeatherMarket.target_date >= lo,
                    WeatherMarket.target_date <= hi,
                )
                .all()
            )
        }
        active_bets = (
            session.query(Bet)
            .filter(
                Bet.status.in_(OPEN_BET_STATUSES),
                Bet.city == city_name,
                Bet.market_id.in_(day_market_ids) if day_market_ids else Bet.market_id.isnot(None),
            )
            .all()
        )
        for bet in active_bets:
            mkt = session.query(WeatherMarket).filter_by(id=bet.market_id).first()
            if mkt is None:
                continue
            thr = float(mkt.threshold or 0)
            if thr not in targets:
                from executor.bet_placer import BetPlacer

                cur = float(bet.current_price or bet.entry_price or 0)
                logger.info(
                    "spread close (window moved): %s %s thr=%s new_window=%s",
                    city_name,
                    str(target_day),
                    thr,
                    sorted(targets),
                )
                BetPlacer().close_bet_for_rotation(bet, cur, session)
                closed += 1

        # Yeni esikler
        for thr in sorted(targets):
            if remaining <= 0:
                skipped += 1
                continue
            mkt = _find_market(session, city_name, metric, target_day, thr)
            if mkt is None:
                skipped += 1
                continue
            dup = (
                session.query(Bet)
                .filter(
                    Bet.market_id == str(mkt.id),
                    Bet.status.in_(OPEN_BET_STATUSES),
                )
                .first()
            )
            if dup:
                continue
            # CANLI fiyat kullan: weather_markets.yes_price, run_fetch_markets
            # her 5 dk'da bir (price poller + scan loop) Polymarket'ten gunceller.
            # (2026-08-11 kullanici karari: bayat snapshot fiyati yerine canli
            # fiyata gore ac — bet 594 entry=0.50 iken canli 0.0085'ti.)
            entry = float(mkt.yes_price) if mkt.yes_price is not None else None
            if entry is None or not (0 < entry < max_entry):
                skipped += 1
                continue
            pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            if pf is None:
                # Portfolio satiri yoksa olustur (bot lifespan disindan calisirken
                # -- orn. catch-up scripti -- garanti yok). 0 cash ile sessizce
                # bet atlamak yerine portfolio'yu INITIAL_PORTFOLIO ile yarat.
                from database.db import ensure_initial_portfolio

                ensure_initial_portfolio()
                pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
            cash = float(pf.cash_balance) if pf else 0.0
            use_stake = min(stake, max(0.0, cash))
            if use_stake <= 0:
                logger.warning(
                    "spread skip: %s %s %sC - insufficient cash (cash=%.2f)",
                    city_name,
                    str(target_day),
                    thr,
                    cash,
                )
                skipped += 1
                continue

            from executor.bet_placer import BetPlacer
            from utils.formulas import bet_shares, polymarket_fee_from_stake

            fill_price = max(0.01, min(0.99, round(entry, 4)))
            shares = bet_shares(use_stake, fill_price)
            fee_rate = bot_config.strategy.current_fee_rate
            entry_fee = polymarket_fee_from_stake(use_stake, fill_price, fee_rate)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            ts = int(now.timestamp())

            bet = Bet(
                market_id=str(mkt.id),
                city=mkt.city,
                city_code=mkt.city_code or "",
                side="YES",
                amount=use_stake,
                stake_amount=use_stake,
                price=fill_price,
                entry_price=fill_price,
                shares=shares,
                current_price=fill_price,
                status="placed",
                order_id=f"spread_{mkt.id}_{ts}",
                placed_at=now,
                entry_fee=round(entry_fee, 4),
                strike_temp=float(mkt.threshold or 0.0),
                potential_payout=use_stake / fill_price if fill_price > 0 else 0,
                fair_value=fill_price,
            )
            from utils.accounting import debit_stake

            try:
                debit_stake(session, use_stake, f"spread_open:{mkt.id}")
                if entry_fee > 0:
                    debit_stake(session, entry_fee, f"spread_fee:{mkt.id}")
                session.add(bet)
                session.flush()
                placed += 1
                remaining -= 1
                cities_used.add(city_name)
                logger.info("spread open: %s %s %sC entry=%.4f", city_name, str(target_day), thr, entry)
            except ValueError as exc:
                logger.warning("spread debit failed for %s: %s", mkt.id, exc)
                skipped += 1

    result = {"placed": placed, "closed": closed, "skipped": skipped, "cities": sorted(cities_used)}
    logger.info("spread %s: %s", target_day, result)
    # Commit, cagiran (wrapper'in with get_session() blok sonu) tarafindan yapilir.
    return result
