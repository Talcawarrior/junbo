"""Saatlik bet snapshot job'i — giris zamani analizi icin.

Tum aktif bet'lerin saatlik fiyat snapshot'ini cekerek bet_snapshots tablosuna
kaydeder. Boylece hangi saat/gunde giris yapilinca daha karli oldugu analiz edilebilir.
"""

import logging
from datetime import datetime, timezone


from database.db import get_session
from database.models import (
    OPEN_BET_STATUSES,
    Bet,
    BetSnapshot,
    WeatherMarket,
)

logger = logging.getLogger("SNAPSHOT_JOB")


def take_bet_snapshots() -> int:
    """Tum aktif bet'ler icin saatlik snapshot al.

    Returns: Kaydedilen snapshot sayisi.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    saved = 0

    with get_session() as session:
        # 1) Aktif bet'leri çek (market ile birlikte)
        active_bets = (
            session.query(Bet, WeatherMarket)
            .join(WeatherMarket, Bet.market_id == WeatherMarket.id, isouter=True)
            .filter(Bet.status.in_(OPEN_BET_STATUSES))
            .all()
        )

        if not active_bets:
            logger.info("take_bet_snapshots: no active bets found")
            return 0

        # 2) Her bet icin snapshot olustur
        for bet, market in active_bets:
            if not market:
                continue

            # Zaman hesaplama
            placed_at = bet.placed_at
            if placed_at and hasattr(placed_at, "tzinfo") and placed_at.tzinfo:
                placed_at = placed_at.replace(tzinfo=None)

            hours_held = 0.0
            if placed_at:
                hours_held = (now - placed_at).total_seconds() / 3600.0

            target_date = market.target_date
            if target_date and hasattr(target_date, "tzinfo") and target_date.tzinfo:
                target_date = target_date.replace(tzinfo=None)

            hours_to_settlement = 0.0
            if target_date:
                hours_to_settlement = (target_date - now).total_seconds() / 3600.0

            # Piyasa fiyati
            market_yes_price = float(market.yes_price or 0)
            entry_price = float(bet.entry_price or bet.price or 0)
            current_price = market_yes_price

            # Unrealized PnL hesapla
            unrealized_pnl = 0.0
            if entry_price > 0 and market_yes_price > 0 and bet.shares:
                shares = float(bet.shares or 0)
                current_value = shares * market_yes_price
                cost = float(bet.amount or 0) + float(bet.entry_fee or 0)
                unrealized_pnl = current_value - cost

            snapshot = BetSnapshot(
                bet_id=bet.id,
                market_id=bet.market_id,
                city=market.city,
                metric=market.metric,
                target_date=target_date,
                entry_price=entry_price,
                amount=float(bet.amount or 0),
                side=bet.side,
                current_price=current_price,
                unrealized_pnl=round(unrealized_pnl, 4),
                market_yes_price=market_yes_price,
                placed_at=placed_at,
                snapshot_time=now,
                hours_held=round(hours_held, 2),
                hours_to_settlement=round(hours_to_settlement, 2),
                bet_status=bet.status,
            )
            session.add(snapshot)
            saved += 1

        logger.info("take_bet_snapshots: %d snapshots saved", saved)

    return saved


def cleanup_old_snapshots(days: int = 30) -> int:
    """Eski snapshot'lari temizle ( varsayilan 30 gun )."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    with get_session() as session:
        deleted = session.query(BetSnapshot).filter(BetSnapshot.snapshot_time < cutoff).delete()
        if deleted:
            logger.info("cleanup_old_snapshots: deleted %d snapshots older than %d days", deleted, days)
        return deleted


def get_entry_time_analysis() -> dict:
    """Giris zamani analizi — saatlik ortalama PnL ve kazanma orani.

    Returns dict with hourly stats.
    """
    with get_session() as session:
        # Tum snapshot'lari çek
        snapshots = session.query(BetSnapshot).all()

        if not snapshots:
            return {"error": "no snapshots found"}

        # Saatlik grupla
        from collections import defaultdict

        hourly = defaultdict(
            lambda: {
                "count": 0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "win_count": 0,
                "win_rate": 0.0,
                "avg_entry_price": 0.0,
                "avg_hours_held": 0.0,
                "avg_hours_to_settlement": 0.0,
            }
        )

        for snap in snapshots:
            if not snap.placed_at:
                continue
            hour = snap.placed_at.hour
            h = hourly[hour]
            h["count"] += 1
            h["total_pnl"] += snap.unrealized_pnl or 0
            h["avg_entry_price"] += snap.entry_price or 0
            h["avg_hours_held"] += snap.hours_held or 0
            h["avg_hours_to_settlement"] += snap.hours_to_settlement or 0
            if (snap.unrealized_pnl or 0) > 0:
                h["win_count"] += 1

        # Ortalamalari hesapla
        for hour, h in hourly.items():
            if h["count"] > 0:
                h["avg_pnl"] = h["total_pnl"] / h["count"]
                h["win_rate"] = h["win_count"] / h["count"]
                h["avg_entry_price"] /= h["count"]
                h["avg_hours_held"] /= h["count"]
                h["avg_hours_to_settlement"] /= h["count"]

        return dict(hourly)
