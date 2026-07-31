"""Saatlik piyasa fiyat snapshot'i — giris zamani analizi icin.

Tum acik WeatherMarket'lerin YES/NO fiyatlarini saatlik olarak
market_snapshots tablosuna kaydeder. Sadece yes_price > 0.01
olan marketler kaydedilir. Boylece hangi saat/gunde hangi
sicaklik araligina girmek daha karli oldugu analiz edilebilir.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func

from database.db import get_session
from database.models import WeatherMarket, MarketSnapshot

logger = logging.getLogger("SNAPSHOT_JOB")

YES_PRICE_MIN = 0.01


def take_market_snapshots() -> int:
    """Tum acik market'ler icin saatlik piyasa snapshot'i al.

    Sadece yes_price > 0.01 olan marketler kaydedilir.
    Ayni market'e ait snapshot'lar saatlik guncellenir
    (aynı saat icin tekrar kayit yapilmaz).

    Returns: Kaydedilen yeni snapshot sayisi.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    saved = 0

    with get_session() as session:
        # 1) Tum acik marketleri cek, yes_price > 0.01 olanlar
        # Sadece HIGH ve LOW sicaklik marketleri (range marketleri haric)
        open_markets = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.yes_price.isnot(None),
                WeatherMarket.yes_price > YES_PRICE_MIN,
                WeatherMarket.market_type.in_(["HIGH", "LOW"]),
            )
            .all()
        )

        if not open_markets:
            logger.info("take_market_snapshots: no qualifying markets found")
            return 0

        # 2) Her market icin snapshot olustur
        for market in open_markets:
            target_date = market.target_date
            if target_date and hasattr(target_date, "tzinfo") and target_date.tzinfo:
                target_date = target_date.replace(tzinfo=None)

            hours_to_settlement = 0.0
            if target_date:
                hours_to_settlement = (target_date - now).total_seconds() / 3600.0

            # Ayni market + saat icin zaten varsa guncelle
            existing = (
                session.query(MarketSnapshot)
                .filter(
                    MarketSnapshot.market_id == market.id,
                    func.date(MarketSnapshot.snapshot_time) == now.date(),
                    func.strftime("%H", MarketSnapshot.snapshot_time) == f"{now.hour:02d}",
                )
                .first()
            )

            if existing:
                existing.yes_price = float(market.yes_price or 0)
                existing.no_price = float(market.no_price or 0)
                existing.volume = float(market.volume or 0)
                existing.hours_to_settlement = round(hours_to_settlement, 2)
                existing.snapshot_time = now
            else:
                snapshot = MarketSnapshot(
                    market_id=market.id,
                    city=market.city,
                    metric=market.metric,
                    target_date=target_date,
                    threshold=market.threshold,
                    threshold_unit=market.threshold_unit,
                    market_type=market.market_type,
                    yes_price=float(market.yes_price or 0),
                    no_price=float(market.no_price or 0),
                    volume=float(market.volume or 0),
                    snapshot_time=now,
                    hours_to_settlement=round(hours_to_settlement, 2),
                )
                session.add(snapshot)
                saved += 1

        logger.info("take_market_snapshots: %d snapshots saved", saved)

    return saved


def cleanup_old_snapshots(days: int = 30) -> int:
    """Eski snapshot'lari temizle (varsayilan 30 gun)."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    with get_session() as session:
        deleted = session.query(MarketSnapshot).filter(MarketSnapshot.snapshot_time < cutoff).delete()
        if deleted:
            logger.info(
                "cleanup_old_snapshots: deleted %d snapshots older than %d days",
                deleted,
                days,
            )
        return deleted


def get_price_history(
    city: Optional[str] = None,
    metric: Optional[str] = None,
    target_date: Optional[datetime] = None,
    hours_back: int = 24,
) -> list[dict]:
    """Belirli bir market icin saatlik YES fiyat gecmisini getir.

    Returns list of dicts with snapshot data.
    """

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours_back)

    with get_session() as session:
        query = session.query(MarketSnapshot).filter(
            MarketSnapshot.snapshot_time >= cutoff,
        )

        if city:
            query = query.filter(MarketSnapshot.city == city)
        if metric:
            query = query.filter(MarketSnapshot.metric == metric)
        if target_date:
            if hasattr(target_date, "tzinfo") and target_date.tzinfo:
                target_date = target_date.replace(tzinfo=None)
            query = query.filter(MarketSnapshot.target_date == target_date)

        rows = query.order_by(MarketSnapshot.snapshot_time.asc()).all()

        return [
            {
                "market_id": r.market_id,
                "city": r.city,
                "metric": r.metric,
                "threshold": r.threshold,
                "target_date": r.target_date.isoformat() if r.target_date else None,
                "yes_price": r.yes_price,
                "no_price": r.no_price,
                "volume": r.volume,
                "hours_to_settlement": r.hours_to_settlement,
                "snapshot_time": r.snapshot_time.isoformat() if r.snapshot_time else None,
            }
            for r in rows
        ]


def get_city_price_comparison(
    city: str,
    metric: Optional[str] = None,
    target_date: Optional[datetime] = None,
    hours_back: int = 24,
) -> dict:
    """Belirli bir sehir icin saatler arasinda YES fiyat karsilastirmasi.

    Returns dict mapping threshold -> list of (snapshot_time, yes_price).
    """

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours_back)

    with get_session() as session:
        query = session.query(MarketSnapshot).filter(
            MarketSnapshot.city == city,
            MarketSnapshot.snapshot_time >= cutoff,
        )

        if metric:
            query = query.filter(MarketSnapshot.metric == metric)
        if target_date:
            if hasattr(target_date, "tzinfo") and target_date.tzinfo:
                target_date = target_date.replace(tzinfo=None)
            query = query.filter(MarketSnapshot.target_date == target_date)

        rows = query.order_by(MarketSnapshot.snapshot_time.asc()).all()

        result: dict[str, list] = {}
        for r in rows:
            key = r.threshold if r.threshold is not None else "unknown"
            if key not in result:
                result[key] = []
            result[key].append(
                {
                    "time": r.snapshot_time.isoformat() if r.snapshot_time else None,
                    "yes_price": r.yes_price,
                    "no_price": r.no_price,
                    "hours_to_settlement": r.hours_to_settlement,
                }
            )

        return result
