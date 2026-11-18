"""Replay zamanlayicisi: botun karar KURALLARINI arsiv uzerinde yurutur.

Iki kol calisir (canli botun 2026-08-20 hibrit stratejisi):

* **SPREAD** - T-2 / T-1 / T-0 bacaklari. Sehir secimi ANA KRITER
  "tahmini EN AZ SAPAN" sehirdir (|bias| kucuk once); sicaklik degil.
  Esik = half_up(forecast ortalamasi), market `RANGE` olmali, giris fiyati
  `spread_max_entry` altinda olmali.
* **METAR-peak** - T-0. `scrapers.metar.detect_peak` zirve kilidini bekler;
  kilitlenince kazanan bucket = half_up(cum_max). Giris
  ``MIN_ENTRY <= fiyat < max_entry`` araliginda olmali.

Butun karar parametreleri CANLI config'den (`bot_config.strategy`) okunur;
`--spread-max-cities` gibi override'lar config drift'ini telafi eder.

Settlement: `actuals.db` gercek tmax -> half_up -> bucket esitligi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from replay.archive import Archive, half_up
from replay.clock import ReplayClock
from replay.price_index import MIN_REAL_PRICE, PriceIndex

#: Canli botun `metar_peak.MIN_ENTRY` degeri ile ayni.
METAR_MIN_ENTRY = 0.05

#: METAR bacaginin gunluk varsayilan bet limiti.
METAR_MAX_BETS_PER_DAY = 12


@dataclass
class BetRecord:
    """Tek bir replay beti (ledger satiri)."""

    day: str
    opened_at: datetime
    strategy: str
    market_id: str
    city: str
    city_code: str
    threshold_c: float
    bucket: int
    entry_price: float
    stake_usd: float
    shares: float
    settle_bucket: int | None
    won: bool
    realized_pnl: float
    entry_fee: float
    price_source: str
    open_trigger: str
    close_kind: str = "settlement"
    closed_at: datetime | None = None


@dataclass
class RunResult:
    bets: list[BetRecord] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


def _cfg(strategy, name: str, default):
    value = getattr(strategy, name, default)
    return default if value is None else value


class ReplayScheduler:
    """Arsiv + PriceIndex + ReplayClock uzerinde gun-gun kosar."""

    def __init__(
        self,
        archive: Archive,
        prices: PriceIndex,
        clock: ReplayClock,
        strategy: str = "both",
        max_cities: int | None = None,
        max_bets_per_day: int | None = None,
        metar_max_per_day: int | None = None,
        allow_price_after: bool = False,
    ):
        self.archive = archive
        self.prices = prices
        self.clock = clock
        self.strategy = strategy
        self.allow_price_after = allow_price_after

        from config.settings import bot_config

        s = bot_config.strategy
        self.spread_stake = float(_cfg(s, "spread_stake_usd", 2.0))
        self.max_entry = float(_cfg(s, "spread_max_entry", 0.95))
        self.max_cities = int(max_cities if max_cities is not None else _cfg(s, "spread_max_cities", 15))
        self.max_bets_per_day = int(
            max_bets_per_day if max_bets_per_day is not None else _cfg(s, "spread_max_bets_per_day", 120)
        )
        metar_cap = (
            metar_max_per_day
            if metar_max_per_day is not None
            else _cfg(s, "metar_peak_max_bets_per_day", METAR_MAX_BETS_PER_DAY)
        )
        self.metar_max_per_day = int(metar_cap)
        self.fee_rate = float(_cfg(s, "current_fee_rate", 0.05))

    # -- yardimcilar ---------------------------------------------------
    @staticmethod
    def _metar_stake() -> float:
        import jobs.metar_peak as mp

        return float(mp.METAR_STAKE)

    def _settle(self, record_city_code: str, target_day: str) -> int | None:
        return self.archive.settlement_bucket(record_city_code, target_day)

    def _record(self, result: RunResult, **kw) -> None:
        stake = float(kw["stake_usd"])
        entry = float(kw["entry_price"])
        fee = round(stake * self.fee_rate, 6)
        shares = stake / entry if entry > 0 else 0.0
        won = bool(kw["won"])
        pnl = round(shares * 1.0 - stake - fee, 6) if won else round(-stake - fee, 6)
        result.bets.append(
            BetRecord(
                day=kw["day"],
                opened_at=kw["opened_at"],
                strategy=kw["strategy"],
                market_id=kw["market_id"],
                city=kw["city"],
                city_code=kw["city_code"],
                threshold_c=float(kw["threshold_c"]),
                bucket=int(kw["bucket"]),
                entry_price=entry,
                stake_usd=stake,
                shares=round(shares, 6),
                settle_bucket=kw.get("settle_bucket"),
                won=won,
                realized_pnl=pnl,
                entry_fee=fee,
                price_source=kw.get("price_source", ""),
                open_trigger=kw.get("open_trigger", ""),
                close_kind="settlement",
                closed_at=kw.get("closed_at"),
            )
        )

    # -- ana dongu -----------------------------------------------------
    def run(self) -> RunResult:
        result = RunResult()
        days = sorted({day for (_c, day, _b) in self.archive.markets})
        for day in days:
            if self.strategy in ("both", "spread"):
                self._run_spread_day(day, result)
            if self.strategy in ("both", "metar", "metar_peak"):
                self._run_metar_day(day, result)
        return result

    # -- SPREAD --------------------------------------------------------
    def _run_spread_day(self, day: str, result: RunResult) -> None:
        open_days = sorted({d for (_c, d, _b) in self.archive.markets if d >= day})
        if not open_days:
            return
        day_dt = datetime.fromisoformat(day + "T00:00:00")
        next_day = (day_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        far_day = open_days[-1]

        targets: list[tuple[str, str, datetime]] = []
        if day in open_days:
            # T-0 girisi sabah 10:00 UTC (gun ici forecast yayinlandiktan sonra)
            targets.append((day, "t0_open", day_dt + timedelta(hours=10)))
        if next_day in open_days:
            targets.append((next_day, "t1_open", day_dt + timedelta(hours=12)))
        if far_day != day and far_day != next_day:
            targets.append((far_day, "t2_open", day_dt + timedelta(hours=12)))

        ranked = self.archive.cities_by_bias()
        allowed = set(ranked[: self.max_cities]) if ranked else set(self.archive.city_name)

        for target_day, trigger, entry_dt in targets:
            entry_ts = entry_dt.timestamp()
            placed = 0
            for code in ranked:
                if placed >= self.max_bets_per_day:
                    break
                if code not in allowed:
                    continue
                mean = self.archive.forecast_before(code, target_day, entry_ts)
                if mean is None:
                    continue
                bucket = half_up(mean)
                market = self.archive.markets.get((code, target_day, bucket))
                if market is None or market.get("market_type") != "RANGE":
                    continue
                # market ilk gorulme ani giris anindan sonraysa look-ahead olur
                first_seen = market.get("first_seen_ts")
                if first_seen is not None and first_seen > entry_ts:
                    continue
                price, source = self.prices.entry(
                    market["id"],
                    entry_ts,
                    min_price=MIN_REAL_PRICE,
                    allow_after=self.allow_price_after,
                )
                if price is None or not (price < self.max_entry):
                    result.trace.append(
                        {
                            "iso": entry_dt.isoformat(),
                            "day": day,
                            "kind": "spread_skip",
                            "city_code": code,
                            "trigger": trigger,
                            "reason": "fiyat_yok" if price is None else "fiyat_siniri",
                        }
                    )
                    continue
                settle = self._settle(code, target_day)
                if settle is None:
                    continue
                won = settle == bucket
                self._record(
                    result,
                    day=day,
                    opened_at=entry_dt,
                    strategy="spread",
                    market_id=market["id"],
                    city=self.archive.city_name.get(code, code),
                    city_code=code,
                    threshold_c=market["threshold"],
                    bucket=bucket,
                    entry_price=price,
                    stake_usd=self.spread_stake,
                    settle_bucket=settle,
                    won=won,
                    price_source=source or "",
                    open_trigger=trigger,
                    closed_at=entry_dt + timedelta(hours=36),
                )
                result.trace.append(
                    {
                        "iso": entry_dt.isoformat(),
                        "day": day,
                        "kind": "spread_placed",
                        "city_code": code,
                        "trigger": trigger,
                        "reason": f"bias={self.archive.city_bias.get(code, float('nan')):.2f}",
                    }
                )
                result.events.append(
                    {
                        "ts": entry_ts,
                        "iso": entry_dt.isoformat(),
                        "type": "bet_opened",
                        "city": self.archive.city_name.get(code, code),
                        "detail": f"spread {target_day} {bucket}C giris=${price:.3f}",
                    }
                )
                placed += 1

    # -- METAR-peak ----------------------------------------------------
    def _run_metar_day(self, day: str, result: RunResult) -> None:
        """Gun icinde TICK TICK zirve kilidi bekler (look-ahead yok).

        Her tick'te `detect_peak` yalnizca O ANA KADAR gelen METAR
        gozlemlerini gorur; zirve kilitlenince (max'tan sonra >= 2 dusus)
        giris o tick'te yapilir. Gun sonunu bekleyip butun gunu tek seferde
        degerlendirmek giris zamanini yapay olarak geciktiriyordu.

        KAPSAM: canli hibritin "aktar" zinciri (zirve asilirsa eski beti
        kapat, yenisine gir) bu surumde MODELLENMEZ; tek giris noktasi alinir.
        """
        from scrapers.metar import city_utc_offset, detect_peak

        stake = self._metar_stake()
        placed = 0
        done: set[str] = set()

        offsets: dict[str, float] = {}
        for code in self.archive.city_name:
            try:
                offsets[code] = city_utc_offset(code, day, self.archive.city_lon.get(code))
            except Exception:
                offsets[code] = 0.0

        for tick_dt in self.clock.day_ticks(day):
            if placed >= self.metar_max_per_day:
                break
            tick_ts = tick_dt.timestamp()
            for code in list(self.archive.city_name):
                if placed >= self.metar_max_per_day:
                    break
                if code in done:
                    continue
                rows = self.archive.metar_day(code, day, tick_ts, offsets[code])
                if len(rows) < 3:
                    continue
                locked, confirmed = detect_peak(
                    sorted(rows, key=lambda r: r[0]),
                    utc_offset_hours=offsets[code],
                    confirmation_minutes=0,
                )
                if not confirmed or locked is None:
                    continue
                done.add(code)
                winner = half_up(max(max(t for _ts, t in rows), locked))
                market = self.archive.markets.get((code, day, winner))
                if market is None or market.get("market_type") != "RANGE":
                    continue
                price, source = self.prices.entry(
                    market["id"],
                    tick_ts,
                    min_price=0.0,
                    allow_after=self.allow_price_after,
                )
                if price is None or not (METAR_MIN_ENTRY <= price < self.max_entry):
                    result.trace.append(
                        {
                            "iso": tick_dt.isoformat(),
                            "day": day,
                            "kind": "metar_skip",
                            "city_code": code,
                            "trigger": "peak_locked",
                            "reason": "fiyat_yok" if price is None else "fiyat_araligi",
                        }
                    )
                    continue
                settle = self._settle(code, day)
                if settle is None:
                    continue
                self._record(
                    result,
                    day=day,
                    opened_at=tick_dt,
                    strategy="metar_peak",
                    market_id=market["id"],
                    city=self.archive.city_name.get(code, code),
                    city_code=code,
                    threshold_c=market["threshold"],
                    bucket=winner,
                    entry_price=price,
                    stake_usd=stake,
                    settle_bucket=settle,
                    won=settle == winner,
                    price_source=source or "",
                    open_trigger="peak_locked",
                    closed_at=tick_dt + timedelta(hours=12),
                )
                result.trace.append(
                    {
                        "iso": tick_dt.isoformat(),
                        "day": day,
                        "kind": "metar_placed",
                        "city_code": code,
                        "trigger": "peak_locked",
                        "reason": f"peak={locked:.1f}C bucket={winner}",
                    }
                )
                result.events.append(
                    {
                        "ts": tick_ts,
                        "iso": tick_dt.isoformat(),
                        "type": "bet_opened",
                        "city": self.archive.city_name.get(code, code),
                        "detail": f"metar {winner}C giris=${price:.3f}",
                    }
                )
                placed += 1
