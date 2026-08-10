"""Yarim saatlik piyasa fiyat snapshot'i — giris zamani analizi icin.

Tum acik WeatherMarket'lerin YES/NO fiyatlarini 30 dakikada bir
market_snapshots tablosuna kaydeder. "highest/lowest temperature"
merdivenindeki her threshold (HIGH or-above, LOW or-below, RANGE exact
bucket) ayri bir markettir ve YES fiyati >= 0.0005 (Polymarket'in gercek
minimum tick'i) olan hepsi kaydedilir. Boylece en dusuk fiyatli longshot
bucket'lari da fiyat gecmisine girer — spread stratejisi ve backtest icin
kritiktir (0.005 ustu filtre, 0.005 alti marketleri atladigi icin en
yuksek edge'li longshot'larin gecmisi eksik kalıyordu).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from database.db import get_session
from database.models import WeatherMarket, MarketSnapshot

logger = logging.getLogger("SNAPSHOT_JOB")

# Polymarket minimum price tick. 0.005 ustu filtre 0.005 alti marketleri
# (en dusuk fiyatli longshot'lar) atliyordu -> spread/backtest fiyat
# gecmisi eksik kaliyordu. 0.0005'e dusturuldu (2026-08-10).
YES_PRICE_MIN = 0.0005


def _bucket_start(dt: datetime) -> datetime:
    """dt'nin icinde bulundugu 30dk penceresinin baslangic zamanini verir."""
    minute = (dt.minute // 30) * 30
    return dt.replace(minute=minute, second=0, microsecond=0)


def _same_bucket(a: datetime, b: datetime) -> bool:
    """a ve b ayni 30dk penceresinde mi?"""
    return _bucket_start(a) == _bucket_start(b)


def take_market_snapshots() -> int:
    """Tum acik sicaklik bucket marketleri icin 30 dakika bir piyasa snapshot'i al.

    Highest/lowest temperature merdivenindeki her threshold kaydedilir
    (HIGH, LOW ve RANGE exact bucket'lari dahil). yes_price >= 0.0005
    (Polymarket min tick) olanlar kaydedilir.

    Ayni market'e ait snapshot'lar 30dk bucket icinde guncellenir
    (ayni 30dk icin tekrar kayit yapilmaz; yeni 30dk penceresi yeni satirdir).

    Returns: Kaydedilen yeni snapshot sayisi.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    saved = 0

    with get_session() as session:
        # 1) Tum acik sicaklik bucket marketlerini cek, yes_price > 0.01 olanlar.
        #    HIGH (or-above), LOW (or-below) VE RANGE (exact bucket) hepsi kaydedilir —
        #    "highest/lowest temperature" merdivenindeki her threshold ayni urunun
        #    bucket'laridir ve tamami saatlik olarak izlenmelidir.
        open_markets = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.status == "open",
                WeatherMarket.yes_price.isnot(None),
                WeatherMarket.yes_price > YES_PRICE_MIN,
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

            # Ayni market icin en guncel snapshot ayni 30dk bucket'indaysa onu
            # guncelle; degilse (yeni 30dk penceresi) yeni satir olustur.
            existing = (
                session.query(MarketSnapshot)
                .filter(MarketSnapshot.market_id == market.id)
                .order_by(MarketSnapshot.snapshot_time.desc())
                .first()
            )
            if existing and existing.snapshot_time and _same_bucket(existing.snapshot_time, now):
                # Ayni 30dk bucket'i -> mevcut satiri guncelle (yeni kayit yok)
                existing.yes_price = float(market.yes_price or 0)
                existing.no_price = float(market.no_price or 0)
                existing.volume = float(market.volume or 0)
                existing.hours_to_settlement = round(hours_to_settlement, 2)
                existing.snapshot_time = now
            else:
                # Eski/parklı bucket ya da hic kayit yok -> HER ZAMAN yeni satir ekle.
                # (Bugfix 2026-08-08: onceden "existing = None" set ediliyor ama yeni
                #  satir olusturulmuyordu; boylece 30dk gecislerinde snapshot duruyordu.)
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


def cleanup_old_snapshots(days: int = 365) -> int:
    """Eski snapshot'lari temizle (varsayilan 365 gun - backtest verisi korunur).

    Dikkat: Snapshot verisi backtest icin kritik girdidir; 30 gun gibi kisa
    bir retention veri kaybina yol acar. Varsayilan 365 gun ile 1 yillik
    backtest penceresi garanti altina alinir.
    """
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
