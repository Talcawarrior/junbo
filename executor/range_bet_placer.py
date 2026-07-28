"""Range betting: YES-only, fixed $10/bet, 3 threshold (T-1, T, T+1).

Akisi:
  place_range_bets()  → 3 bet ac (her sehir icin)
  check_range_pt()    → her 5dk: PT, trail stop, settlement satis
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from config.settings import bot_config
from database.db import get_session
from database.models import OPEN_BET_STATUSES, Bet, Portfolio, WeatherMarket, WeatherForecast

logger = logging.getLogger("RANGE_BET")

_TARGET_DAY_OFFSET = 2  # 2 gun sonrasi (30/7 icin)


# ── HELPERS ──────────────────────────────────────────────────────────────

def _resolve_icao(city: str) -> str | None:
    """City name → ICAO code via WeatherMarket."""
    with get_session() as s:
        row = s.query(WeatherMarket.city_code).filter(
            WeatherMarket.city.ilike(city), WeatherMarket.city_code.isnot(None)
        ).first()
        return row[0] if row else None


def _get_forecast_temp(city: str, icao: str, metric: str, target_date: datetime) -> float | None:
    """Get latest temperature forecast (cache-first)."""
    with get_session() as s:
        forecasts = (
            s.query(WeatherForecast)
            .filter(
                WeatherForecast.city.ilike(icao),
                WeatherForecast.metric == metric,
                WeatherForecast.target_date == target_date,
                WeatherForecast.source.isnot(None),
            )
            .order_by(WeatherForecast.fetched_at.desc())
            .all()
        )
        if not forecasts:
            return None
        latest_by_source = {}
        for f in forecasts:
            if f.source not in latest_by_source:
                latest_by_source[f.source] = f.predicted_value
        values = list(latest_by_source.values())
        if not values:
            return None
        return sum(values) / len(values)


def _polymarket_favorite(city: str, target_date: datetime) -> int | None:
    """Return the threshold (int) of the Polymarket strike with highest yes_price."""
    with get_session() as s:
        market = (
            s.query(WeatherMarket)
            .filter(
                WeatherMarket.city.ilike(city),
                WeatherMarket.metric == "temperature_max",
                WeatherMarket.target_date == target_date,
                WeatherMarket.status == "open",
            )
            .order_by(WeatherMarket.yes_price.desc())
            .first()
        )
        if market and market.threshold:
            return int(round(market.threshold))
        return None


def _find_market(city: str, threshold: int, target_date: datetime) -> WeatherMarket | None:
    with get_session() as s:
        s.expire_on_commit = False
        return s.query(WeatherMarket).filter(
            WeatherMarket.city.ilike(city),
            WeatherMarket.metric == "temperature_max",
            WeatherMarket.threshold == float(threshold),
            WeatherMarket.target_date == target_date,
            WeatherMarket.status == "open",
        ).first()


def _existing_bet(market_id: str) -> bool:
    with get_session() as s:
        return s.query(Bet).filter(Bet.market_id == market_id, Bet.status != "rejected").first() is not None


def _settlement_dt(market: WeatherMarket) -> datetime | None:
    if not market.target_date:
        return None
    dt = market.target_date
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hours_left(target: datetime) -> float:
    return (target - datetime.now(timezone.utc)).total_seconds() / 3600.0


# ── BET ACILISI ──────────────────────────────────────────────────────────

def place_range_bets() -> list[str]:
    """3 threshold bet açar (T-1, T, T+1). Tum 3'u ≤0.10 olmalı."""
    s = bot_config.strategy
    if not s.range_bet_enabled or not s.range_bet_cities:
        return []

    spread = s.range_bet_spread  # =1
    bet_amount = s.range_bet_amount  # =10
    target_date = (datetime.now(timezone.utc) + timedelta(days=_TARGET_DAY_OFFSET)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    # Also try midnight version
    alt_date = target_date.replace(hour=0, minute=0, second=0, microsecond=0)

    logger.info("Range bet placement: target=%s, spread=%d", target_date.date(), spread)
    results = []

    for city in s.range_bet_cities:
        icao = _resolve_icao(city)
        if not icao:
            logger.info("Range: %s — ICAO bulunamadi", city)
            continue

        # Weather fetch et (cache-first) — for forecast data freshness
        _ensure_weather(city, icao, target_date)

        # Use Polymarket highest-priced strike as center
        base = _polymarket_favorite(city, target_date)
        if base is None:
            # Fallback: model forecast
            temp = _get_forecast_temp(city, icao, "temperature_max", target_date)
            if temp is None:
                logger.info("Range: %s — polymarket da forecast da yok", city)
                continue
            base = round(temp)

        spread = s.range_bet_spread  # =1 (3 bet: T-1, T, T+1)
        thresholds = list(range(base - spread, base + spread + 1))

        candidates = []
        skip = False
        for t in thresholds:
            market = _find_market(city, t, target_date)
            if not market:
                market = _find_market(city, t, alt_date)
            if not market:
                logger.info("Range: %s %dC — market yok, atlaniyor", city, t)
                skip = True
                break
            if _existing_bet(str(market.id)):
                logger.info("Range: %s %dC — zaten bet var, atlaniyor", city, t)
                skip = True
                break
            hl = _settlement_dt(market)
            if hl and _hours_left(hl) <= 8:
                logger.info("Range: %s %dC — settlementa %.0fh kala, atlaniyor", city, t, _hours_left(hl))
                skip = True
                break
            yp = float(market.yes_price or 0.5)
            candidates.append((market, yp, t))

        if skip or len(candidates) < (spread * 2 + 1):
            continue

        # Tum betleri ac
        for market, yp, threshold in candidates:
            _place_one_bet(market, yp, bet_amount)
            msg = f"{city} {threshold}C YES ${bet_amount:.0f} @ {yp:.3f}"
            logger.info("Range bet: %s", msg)
            results.append(msg)

    if results:
        logger.info("Range betting: %d bets placed", len(results))
    else:
        logger.info("Range betting: none placed")
    return results


def _ensure_weather(city: str, icao: str, target_date: datetime) -> None:
    """Cache-first weather. Forecast varsa bekle, yoksa fetch et."""
    with get_session() as s:
        has = s.query(WeatherForecast).filter(
            WeatherForecast.city == icao,
            WeatherForecast.target_date == target_date,
            WeatherForecast.source.isnot(None),
        ).first()
        if has:
            return
    # Forecast yok → fetch tetikle
    try:
        from jobs.scheduler import run_fetch_weather
        run_fetch_weather()
    except Exception as e:
        logger.warning("Weather fetch failed for %s: %s", city, e)


def _place_one_bet(market: WeatherMarket, yes_price: float, bet_amount: float) -> None:
    shares = round(bet_amount / yes_price, 4) if yes_price > 0 else 0
    entry_fee = round(bet_amount * bot_config.strategy.current_fee_rate * (1 - yes_price), 4)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = int(now.timestamp())

    bet = Bet(
        market_id=str(market.id),
        city=market.city,
        city_code=market.city_code or "",
        side="YES",
        amount=bet_amount,
        stake_amount=bet_amount,
        price=yes_price,
        entry_price=yes_price,
        shares=shares,
        current_price=yes_price,
        status="placed",
        order_id=f"range_{market.id}_{ts}",
        placed_at=now,
        entry_fee=entry_fee,
        ladder_data=json.dumps({"pt_taken": False, "peak_price": yes_price, "type": "range"}),
        potential_payout=bet_amount / yes_price if yes_price > 0 else 0,
    )

    with get_session() as s:
        from utils.accounting import debit_stake
        from utils.formulas import portfolio_total_value

        s.add(bet)
        m = s.query(WeatherMarket).filter(WeatherMarket.id == market.id).first()
        if m:
            m.status = "bet_placed"

        # Deduct stake + entry fee from portfolio cash (central accounting)
        try:
            debit_stake(s, bet_amount, f"range_bet_open:{market.id}")
            if entry_fee > 0:
                debit_stake(s, entry_fee, f"range_bet_fee:{market.id}")
        except ValueError as e:
            logger.warning("Range bet debit failed for %s: %s", market.id, e)

        # Update portfolio totals
        pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
        if pf:
            open_amt = s.query(Bet.amount).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
            open_exposure = sum(float(a[0] or 0) for a in open_amt)
            pf.total_value = portfolio_total_value(float(pf.cash_balance or 0), open_exposure)
            pf.last_updated = now
        s.commit()


# ── PT / TRAIL STOP / SETTLEMENT SATIS ──────────────────────────────────

def check_range_pt() -> int:
    """5dk'da 1 calisir. PT, trail stop, settlement satisini yonetir.

    Return: kapatilan bet sayisi
    """
    closed = 0
    s = bot_config.strategy
    trail_pct = s.range_bet_trail_stop_pct        # 0.30
    pre_settle = s.range_bet_pre_settlement_hours # 1.0

    with get_session() as session:
        bets = (
            session.query(Bet)
            .filter(
                Bet.order_id.like("range_%"),
                Bet.status.in_(OPEN_BET_STATUSES),
                Bet.ladder_data.isnot(None),
            )
            .all()
        )
        if not bets:
            return 0

        # Group by city
        cities: dict[str, list[Bet]] = {}
        for b in bets:
            c = (b.city or "").lower()
            cities.setdefault(c, []).append(b)

        expected_count = s.range_bet_spread * 2 + 1  # 5 bets for spread=2
        for city, city_bets in cities.items():
            if len(city_bets) != expected_count:
                continue  # incomplete set

            # PT kontrolu: total hisse degeri
            total_value = sum(float(b.shares or 0) * float(b.current_price or 0) for b in city_bets)
            total_stake = sum(float(b.amount or 0) for b in city_bets)

            # PT tetikle: deger 2x stake olduysa
            if not _any_pt_taken(city_bets) and total_value >= total_stake * 2:
                logger.info("PT: %s total_value=%.2f >= $%.0f, PT basliyor", city, total_value, total_stake * 2)
                closed += _execute_pt(city_bets, session)
                continue

            # Trail stop / settlement check for remaining
            for b in city_bets:
                if b.status not in OPEN_BET_STATUSES or b.ladder_data is None:
                    continue
                try:
                    meta = json.loads(b.ladder_data)
                except (json.JSONDecodeError, TypeError):
                    meta = {"peak_price": float(b.entry_price or 0), "pt_taken": True}

                if meta.get("pt_taken") and trail_pct > 0:
                    current = float(b.current_price or 0)
                    peak = meta.get("peak_price", current)
                    if current > peak:
                        meta["peak_price"] = current
                    elif current < peak * (1 - trail_pct):
                        _close_bet(b, session, "closed", f"trail_stop: peak={peak:.4f} cur={current:.4f}")
                        meta["closed_reason"] = "trail"
                        closed += 1
                    b.ladder_data = json.dumps(meta)
                elif meta.get("pt_taken"):
                    b.ladder_data = json.dumps(meta)

                # Settlement kontrolu
                m = session.query(WeatherMarket).filter(
                    WeatherMarket.id == b.market_id
                ).first()
                if m and m.target_date:
                    td = m.target_date
                    if getattr(td, 'tzinfo', None) is None:
                        td = td.replace(tzinfo=timezone.utc)
                    hl = _hours_left(td)
                    if hl <= pre_settle:
                        cur = float(b.current_price or b.entry_price or 0)
                        _close_bet(b, session, "closed", f"pre_settlement: {hl:.1f}h left @{cur:.4f}")
                        closed += 1

            session.commit()

    if closed:
        logger.info("Range PT: %d positions closed", closed)
    return closed


def _any_pt_taken(bets: list[Bet]) -> bool:
    for b in bets:
        if b.ladder_data:
            try:
                meta = json.loads(str(b.ladder_data))  # type: ignore[arg-type]
                if meta.get("pt_taken"):
                    return True
            except (json.JSONDecodeError, TypeError):
                pass
    return False


def _execute_pt(bets: list[Bet], session) -> int:
    """Partial take: degerin yarisini sat."""
    closed = 0
    for b in bets:
        if b.ladder_data is None:
            continue
        try:
            meta = json.loads(str(b.ladder_data))  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError):
            meta = {}

        cur = float(b.current_price or 0)
        shares = float(b.shares or 0)
        entry_price = float(b.entry_price or 0)
        stake = float(b.amount or 0)

        # PT: yari hisseyi sat
        sell_shares = shares * 0.5
        sell_value = sell_shares * cur
        buy_cost = (shares * 0.5) * entry_price
        pt_pnl = sell_value - buy_cost

        # Credit sold portion to portfolio (central accounting)
        try:
            from utils.accounting import credit_sale
            proceeds = round(sell_value, 2)
            if proceeds > 0:
                credit_sale(session, proceeds, f"range_pt:{b.market_id}")
        except Exception as e:
            logger.warning("PT credit failed for %s: %s", b.market_id, e)

        # Kalan yariyi trail stop ile takip et
        meta["pt_taken"] = True
        meta["peak_price"] = cur
        meta["pt_pnl"] = meta.get("pt_pnl", 0) + pt_pnl

        b.realized_pnl = (b.realized_pnl or 0) + pt_pnl  # type: ignore[assignment]
        b.shares = shares * 0.5  # kalan hisse  # type: ignore[assignment]
        b.amount = stake * 0.5  # type: ignore[assignment]
        b.ladder_data = json.dumps(meta)  # type: ignore[assignment]
        thr = _threshold_from_market(str(b.market_id), session)  # type: ignore[arg-type]
        logger.info(
            "PT: %s %sC yari satis @%.4f pnl=%.2f",
            b.city, thr, cur, pt_pnl,
        )
        closed += 1

    # Update portfolio totals after all PT sales
    try:
        from utils.formulas import portfolio_total_value
        pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
        if pf:
            open_amt = session.query(Bet.amount).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
            open_exposure = sum(float(a[0] or 0) for a in open_amt)
            pf.total_value = portfolio_total_value(float(pf.cash_balance or 0), open_exposure)
            pf.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception as e:
        logger.warning("PT portfolio update failed: %s", e)

    session.commit()
    return closed


def _close_bet(bet: Bet, session, status: str, reason: str) -> None:
    cur = float(bet.current_price or bet.entry_price or 0)
    shares = float(bet.shares or 0)
    entry = float(bet.entry_price or 0)
    pnl = (cur - entry) * shares
    bet.realized_pnl = (bet.realized_pnl or 0) + pnl  # type: ignore[assignment]
    bet.status = status  # type: ignore[assignment]
    bet.close_reason = reason  # type: ignore[assignment]
    bet.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)  # type: ignore[assignment]

    # Credit sale proceeds to portfolio (central accounting)
    try:
        from utils.accounting import credit_sale
        from utils.formulas import portfolio_total_value
        proceeds = round(shares * cur, 2)
        if proceeds > 0:
            credit_sale(session, proceeds, f"range_close:{bet.market_id}:{reason}")
    except Exception as e:
        logger.warning("Range close credit failed for %s: %s", bet.market_id, e)

    # Update portfolio totals
    pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if pf:
        open_amt = session.query(Bet.amount).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
        open_exposure = sum(float(a[0] or 0) for a in open_amt)
        pf.total_value = portfolio_total_value(float(pf.cash_balance or 0), open_exposure)
        pf.total_realized_pnl = round((pf.total_realized_pnl or 0) + pnl, 2)
        if pnl > 0:
            pf.total_won = (pf.total_won or 0) + 1
        else:
            pf.total_lost = (pf.total_lost or 0) + 1
        pf.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)

    logger.info("Close: %s %s pnl=%.2f reason=%s", bet.city, bet.market_id, pnl, reason)


def _threshold_from_market(market_id: str, session) -> str:
    m = session.query(WeatherMarket).filter(WeatherMarket.id == market_id).first()
    if m and m.threshold is not None:
        return str(int(m.threshold))
    return "?"
