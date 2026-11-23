"""Giris fiyati indeksi (look-ahead YOK).

Kaynak onceligi (README §4c, 2026-08-23 duzeltmesi):

1. ``snapshot``      - `market_snapshots.yes_price` (gamma/snapshot). Canli
   botun `weather_markets.yes_price` degeri de bu kaynaktan gelir.
2. ``orderbook``     - `orderbook_snapshots.best_ask`. Ikincil: ask, gamma'dan
   once dusuk gorunebiliyor ve replay'i canlidan 30-90 dk ERKEN bet actiriyor.
3. ``price_history`` - `backtest_prices.db`. Son care.

Eski halde bir ``_seed`` adimi ilk gamma fiyatini zaman penceresi olmadan
kullaniyordu; bu look-ahead'di ve KALDIRILDI. Artik fiyat verisi gelene kadar
bet acilmaz.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict

#: Bos-defter artefaktlari (0.001-0.05) gercek dolu CLOB fiyati DEGILDIR.
MIN_REAL_PRICE = 0.05

#: Kaynak oncelik sirasi.
PRIORITY = ("snapshot", "orderbook", "price_history")


class PriceIndex:
    """market_id -> kaynak -> [(epoch, price)] (zamana gore sirali)."""

    def __init__(self):
        self._series: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
        #: (market_id, kaynak) -> sirali epoch listesi (bisect icin, tembel)
        self._ts_keys: dict[tuple[str, str], list[float]] = {}

    def add(self, source: str, market_id: str, ts: float, price: float) -> None:
        self._series[str(market_id)][str(source)].append((float(ts), float(price)))

    def load_from_archive(self, archive) -> "PriceIndex":
        for mid, source, ts, price in archive.price_rows:
            self.add(source, mid, ts, price)
        self.finalize()
        return self

    def finalize(self) -> None:
        for by_source in self._series.values():
            for rows in by_source.values():
                rows.sort(key=lambda r: r[0])

    # -- sorgular ------------------------------------------------------
    def before(self, market_id: str, t: float, min_price: float = 0.0):
        """`t` aninda (veya oncesinde) gorulen en yakin fiyat.

        Donen: ``(fiyat|None, kaynak|None)``. Oncelik sirasi ``PRIORITY``.
        """
        by_source = self._series.get(str(market_id))
        if not by_source:
            return None, None
        for source in PRIORITY:
            best = None
            for ts, price in by_source.get(source, []):
                if ts > t:
                    break
                if price >= min_price:
                    best = price
            if best is not None:
                return best, source
        return None, None

    def at_or_after(self, market_id: str, t: float, min_price: float = 0.0):
        """`t` anindan SONRAKI ilk fiyat (yalnizca bilincli fallback icin)."""
        by_source = self._series.get(str(market_id))
        if not by_source:
            return None, None
        for source in PRIORITY:
            for ts, price in by_source.get(source, []):
                if ts >= t and price >= min_price:
                    return price, source
        return None, None

    def latest_with_ts(
        self, market_id: str, t: float, source: str, max_age: float | None = None
    ) -> tuple[float, float] | None:
        """TEK bir kaynagin `t` anina kadarki SON (epoch, fiyat) kaydi (bisect)."""
        by_source = self._series.get(str(market_id))
        if not by_source:
            return None
        rows = by_source.get(source)
        if not rows:
            return None
        key = (str(market_id), source)
        keys = self._ts_keys.get(key)
        if keys is None or len(keys) != len(rows):
            keys = [r[0] for r in rows]
            self._ts_keys[key] = keys
        i = bisect_right(keys, t) - 1
        if i < 0:
            return None
        ts, price = rows[i]
        if max_age is not None and t - ts > max_age:
            return None
        return ts, price

    def latest(self, market_id: str, t: float, source: str, max_age: float | None = None) -> float | None:
        """TEK bir kaynagin `t` anina kadarki SON fiyati (look-ahead yok, bisect).

        Hibrit METAR replay'i (2026-11-23) icin: ``snapshot`` = gamma
        snapshot'i (30 dk), ``orderbook`` = CLOB ask, ``orderbook_bid`` = CLOB
        bid, ``orderbook_mid`` = (ask+bid)/2. `max_age` (sn) verilirse daha
        eski fiyat "yok" sayilir.
        """
        found = self.latest_with_ts(market_id, t, source, max_age=max_age)
        return None if found is None else found[1]

    def freshest(
        self, market_id: str, t: float, sources: tuple[str, ...], not_before: float | None = None
    ) -> float | None:
        """Verilen kaynaklar arasinda `t`'ye EN YAKIN (en yeni) fiyat.

        `not_before` verilirse bu andan ONCE kaydedilmis fiyat kabul edilmez
        (karar bilgisinden onceki fiyatla dolum = look-ahead).
        """
        best: tuple[float, float] | None = None
        for source in sources:
            found = self.latest_with_ts(market_id, t, source)
            if found is not None and (best is None or found[0] > best[0]):
                best = found
        if best is None or (not_before is not None and best[0] < not_before):
            return None
        return best[1]

    def entry(self, market_id: str, t: float, min_price: float = MIN_REAL_PRICE, allow_after: bool = False):
        """Giris fiyati: once `before`, istenirse `at_or_after` fallback."""
        price, source = self.before(market_id, t, min_price=min_price)
        if price is not None:
            return price, source
        if allow_after:
            return self.at_or_after(market_id, t, min_price=min_price)
        return None, None

    def __len__(self) -> int:
        return len(self._series)
