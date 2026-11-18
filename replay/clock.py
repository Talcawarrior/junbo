"""Replay saati: kontrol edilebilir, geri sarmayan zaman.

Canli bot `datetime.now()` / `time.time()` kullanir. Replay'de zaman
disaridan surulur; boylece "T aninda yalnizca T'den once var olan veri"
kurali uygulanabilir (CLAUDE.md §7, look-ahead yasak).

`ReplayClock` zaman URETMEZ, yalnizca ilerletir ve sinirlari dogrular:
pencere disina cikinca durur. Makine saatine HIC bakmaz.
"""

from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_TICK_MINUTES = 5


def _naive(value: datetime) -> datetime:
    """tz-aware degeri naive UTC'ye indir (arsivler naive UTC saklar)."""
    if value.tzinfo is not None:
        offset = value.utcoffset() or timedelta(0)
        return value.replace(tzinfo=None) - offset
    return value


class ReplayClock:
    """`[start, end)` araliginda `tick_minutes` adimla ilerleyen saat."""

    def __init__(self, start: datetime, end: datetime, tick_minutes: int = DEFAULT_TICK_MINUTES):
        self.start = _naive(start)
        self.end = _naive(end)
        if self.end <= self.start:
            raise ValueError("ReplayClock: end, start'tan sonra olmali")
        self.tick = timedelta(minutes=max(1, int(tick_minutes)))
        self._now = self.start

    # -- durum ---------------------------------------------------------
    def now(self) -> datetime:
        return self._now

    def reset(self) -> datetime:
        self._now = self.start
        return self._now

    def advance(self) -> datetime:
        """Bir tick ilerlet; pencere bittiyse end'e sabitle."""
        self._now = min(self._now + self.tick, self.end)
        return self._now

    def exhausted(self) -> bool:
        return self._now >= self.end

    # -- uretim --------------------------------------------------------
    def ticks(self):
        """`start`'tan `end`'e (haric) kadar tum tick'ler."""
        t = self.start
        while t < self.end:
            yield t
            t += self.tick

    def day_ticks(self, day: str):
        """Bir gunun (YYYY-MM-DD) pencereye giren tick'leri."""
        lo = max(self.start, datetime.fromisoformat(day + "T00:00:00"))
        hi = min(self.end, datetime.fromisoformat(day + "T00:00:00") + timedelta(days=1))
        t = lo
        while t < hi:
            yield t
            t += self.tick

    def __repr__(self) -> str:  # pragma: no cover - teshis
        return f"ReplayClock({self.start} -> {self.end}, tick={self.tick})"
