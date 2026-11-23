"""Replay zamanlayicisi: botun karar KURALLARINI arsiv uzerinde yurutur.

Iki kol calisir (canli botun 2026-08-20 hibrit stratejisi):

* **SPREAD** - T-2 / T-1 / T-0 bacaklari. Sehir secimi ANA KRITER
  "tahmini EN AZ SAPAN" sehirdir (|bias| kucuk once); sicaklik degil.
  Esik = half_up(forecast ortalamasi), market `RANGE` olmali, giris fiyati
  `spread_max_entry` altinda olmali.
* **METAR-peak** - T-0. Iki mod:

  - ``lock`` (varsayilan, eski): `scrapers.metar.detect_peak` zirve kilidini
    bekler; kilitlenince kazanan bucket = half_up(cum_max). Tek giris,
    aktar YOK. Giris ``MIN_ENTRY <= fiyat < max_entry`` araliginda olmali.
  - ``hybrid`` (2026-11-23): canli `jobs.metar_peak.run_metar_peak_bets`
    kurallari — sehir bazli gecmis ortalama peak saati + ucuz
    (<= EARLY_MAX_PRICE) ERKEN giris, pahaliysa 1-dusus kilidi (30 dk onay),
    zirve asilinca AKTAR zinciri (yanlis bucket canli bid'den kapatilir,
    yenisine girilir), bayat METAR korumasi, gunluk cap. ``metar_early=False``
    erken girisi kapatir (yalnizca 1-dusus kilidi, 2026-08-20 oncesi kural).

Butun karar parametreleri CANLI config'den (`bot_config.strategy`) okunur;
`--spread-max-cities` gibi override'lar config drift'ini telafi eder.

Settlement: ``actuals`` (varsayilan) `actuals.db` gercek tmax -> half_up ->
bucket esitligi. ``polymarket``: marketin GERCEK Polymarket sonucu
(`raw_data`), yoksa canli betin won/lost durumu, o da yoksa gunun arsiv
METAR max'i (half_up).

ZAMAN (2026-11-23 duzeltme): ReplayClock naive UTC uretir; naive
`datetime.timestamp()` MAKINE saat dilimiyle (UTC+3) yorumlaniyordu -> her
karar 3 saat ESKI veriye bakiyor, UTC gununun son 3 saati hic gorulmuyordu.
Tum tick epoch'lari artik `_utc_ts` ile UTC alinir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from replay.archive import Archive, half_up
from replay.clock import ReplayClock
from replay.price_index import MIN_REAL_PRICE, PriceIndex

#: Canli botun `metar_peak.MIN_ENTRY` degeri ile ayni.
METAR_MIN_ENTRY = 0.05

#: Canli `_open_metar_bet` ust siniri (kazanan bucket bilindigi icin 0.95).
METAR_MAX_ENTRY = 0.95

#: Canli `run_metar_peak_bets` icindeki `detect_peak` onay suresi (dk).
METAR_CONFIRM_MIN = 30

#: METAR bacaginin gunluk varsayilan bet limiti.
METAR_MAX_BETS_PER_DAY = 12

#: Hibrit replay: orderbook ask/bid bu kadar eskiyse canli CLOB sayilmaz (sn).
ORDERBOOK_FRESH_S = 30 * 60

METAR_MODES = ("lock", "hybrid")
SETTLE_SOURCES = ("actuals", "polymarket")


def _utc_ts(dt: datetime) -> float:
    """Replay saati (naive = UTC) -> UTC epok. MAKINE saat dilimi KULLANILMAZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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
    close_price: float | None = None


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
        metar_mode: str = "lock",
        metar_early: bool = True,
        early_max_price: float | None = None,
        settle: str = "actuals",
        include_non_metar: bool = False,
        info_prices: bool = True,
    ):
        if metar_mode not in METAR_MODES:
            raise ValueError(f"bilinmeyen metar_mode: {metar_mode!r} (gecerli: {METAR_MODES})")
        if settle not in SETTLE_SOURCES:
            raise ValueError(f"bilinmeyen settle: {settle!r} (gecerli: {SETTLE_SOURCES})")
        self.archive = archive
        self.prices = prices
        self.clock = clock
        self.strategy = strategy
        self.allow_price_after = allow_price_after
        self.metar_mode = metar_mode
        self.metar_early = bool(metar_early)
        self.early_max_price = early_max_price
        self.settle = settle
        self.include_non_metar = bool(include_non_metar)
        #: hybrid: True = fiyat, karar bilgisinin (son gozlemin gorulme ani)
        #: SONRASINDA kaydedilmis olmali. False yalnizca duyarlilik analizi
        #: icindir (bilgi oncesi fiyat = look-ahead, sonuc iyimser cikar).
        self.info_prices = bool(info_prices)

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

    def _bet_outcome(
        self, market: dict, code: str, day: str, bucket: int, utc_offset_hours: float = 0.0
    ) -> tuple[bool | None, int | None, str]:
        """(kazandi mi, settle_bucket, kaynak). ``actuals`` = eski davranis."""
        if self.settle == "polymarket":
            won, source = self.archive.market_outcome(market["id"])
            if won is not None:
                return won, None, source
            fallback = self.archive.metar_final_bucket(code, day, utc_offset_hours)
            if fallback is None:
                return None, None, "yok"
            return fallback == bucket, fallback, "metar_arsiv"
        settle = self._settle(code, day)
        if settle is None:
            return None, None, "yok"
        return settle == bucket, settle, "actuals"

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
                if self.metar_mode == "hybrid":
                    self._run_metar_hybrid_day(day, result)
                else:
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
            entry_ts = _utc_ts(entry_dt)
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
                won, settle, _src = self._bet_outcome(market, code, target_day, bucket)
                if won is None:
                    continue
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

    # -- METAR-peak (lock) ---------------------------------------------
    def _run_metar_day(self, day: str, result: RunResult) -> None:
        """Gun icinde TICK TICK zirve kilidi bekler (look-ahead yok).

        Her tick'te `detect_peak` yalnizca O ANA KADAR gelen METAR
        gozlemlerini gorur; zirve kilitlenince (max'tan sonra >= 2 dusus)
        giris o tick'te yapilir. Gun sonunu bekleyip butun gunu tek seferde
        degerlendirmek giris zamanini yapay olarak geciktiriyordu.

        KAPSAM: canli hibritin erken girisi ve "aktar" zinciri bu modda
        MODELLENMEZ; tek giris noktasi alinir (bkz. ``metar_mode="hybrid"``).
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
            tick_ts = _utc_ts(tick_dt)
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
                won, settle, _src = self._bet_outcome(market, code, day, winner, offsets[code])
                if won is None:
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
                    won=won,
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

    # -- METAR-peak (hybrid, canli kural) ------------------------------
    def _range_market(self, code: str, day: str, bucket: int) -> dict | None:
        found = self.archive.range_markets.get((code, day, bucket))
        return found[0] if found else None

    def _db_price(self, market_id: str, t: float, not_before: float | None = None) -> float | None:
        """Canli botun DB `yes_price` vekili: snapshot (30 dk) ile orderbook
        orta fiyatindan (~10 dk) hangisi `t`'ye daha yakinsa o; `not_before`
        (bilgi zamani) oncesi fiyat kabul edilmez."""
        return self.prices.freshest(market_id, t, ("snapshot", "orderbook_mid"), not_before=not_before)

    def _ask_price(self, market_id: str, t: float, not_before: float | None) -> float | None:
        """Dolum: taze CLOB ask (bilgi zamanindan sonra kaydedilmis)."""
        found = self.prices.latest_with_ts(market_id, t, "orderbook", max_age=ORDERBOOK_FRESH_S)
        if found is None or (not_before is not None and found[0] < not_before):
            return None
        return float(found[1])

    def _exit_price(self, market_id: str, t: float, not_before: float | None = None) -> float | None:
        """Aktar satis fiyati: taze orderbook BID, yoksa DB fiyat vekili.

        Ikisi de bilgi zamanindan (`not_before`) ONCE ise None — bet o tick'te
        kapatilmaz, sonraki tick'te (piyasa yeni max'i fiyatladiginda) denenir.
        """
        bid = self.prices.latest_with_ts(market_id, t, "orderbook_bid", max_age=ORDERBOOK_FRESH_S)
        if bid is not None and (not_before is None or bid[0] >= not_before):
            return float(bid[1])
        return self._db_price(market_id, t, not_before=not_before)

    def _run_metar_hybrid_day(self, day: str, result: RunResult) -> None:  # noqa: C901
        """Canli `run_metar_peak_bets` (2026-08-20 hibrit) + aktar zinciri.

        Her tick (canli dongu 10 dk) ve sehir icin YALNIZCA o ana kadar botun
        gordugu (`recorded_at`) METAR gozlemleri kullanilir:

        * erken acik: sehrin gecmis ortalama peak saati gelmeden beklenir;
          saat gelince cur_max bucket'inin fiyati <= early_max ise dusus
          beklenmeden girilir, pahaliysa 1-dusus kilidi beklenir.
        * erken kapali (``metar_early=False``): yalnizca 1-dusus kilidi.
        * kazanan bucket = half_up(cur_max); degisince yanlis bucket betleri
          bid'den (yoksa snapshot) kapatilir, yenisine girilir. Fiyat yoksa
          bet settlement'a kadar tutulur (canli ile ayni).
        * bayat METAR (45/90 dk): aktar calisir, YENI bet acilmaz.
        * giris: DB yes_price vekili (snapshot / orderbook orta, hangisi
          tazeyse) [MIN_ENTRY, 0.95) + taze orderbook ask (yoksa vekil) ile
          dolum; gunluk cap UTC gun basina.
        """
        import jobs.metar_peak as mp
        from engine.market_selection import trading_city_allowed
        from scrapers.metar import city_utc_offset, detect_peak
        from utils.formulas import bet_shares, polymarket_fee_from_stake

        stake = self._metar_stake()
        early_max = float(mp.EARLY_MAX_PRICE if self.early_max_price is None else self.early_max_price)
        skip_codes = set() if self.include_non_metar else {str(c).upper() for c in mp.NON_METAR_RESOLUTION}

        codes = sorted({c for (c, d, _b) in self.archive.range_markets if d == day})
        codes = [
            c for c in codes if c.upper() not in skip_codes and trading_city_allowed(self.archive.city_name.get(c))
        ]
        if not codes:
            return

        day_start_ts = _utc_ts(datetime.fromisoformat(day + "T00:00:00"))
        offsets: dict[str, float] = {}
        avg_hours: dict[str, float | None] = {}
        stale_lims: dict[str, float] = {}
        for code in codes:
            try:
                offsets[code] = float(city_utc_offset(code, day, self.archive.city_lon.get(code)))
            except Exception:  # noqa: BLE001 - zaman dilimi cozulemezse UTC
                offsets[code] = 0.0
            avg_hours[code] = self.archive.avg_peak_hour(code, day) if self.metar_early else None
            stale_lims[code] = self.archive.stale_limit_min(code, day_start_ts)

        open_pos: dict[str, list[dict]] = {c: [] for c in codes}
        had_pos: set[str] = set()
        all_pos: list[dict] = []
        opened_today = 0

        for tick_dt in self.clock.day_ticks(day):
            tick_ts = _utc_ts(tick_dt)
            for code in codes:
                off = offsets[code]
                rows = self.archive.metar_seen_day(code, day, tick_ts, off)
                if not rows:
                    continue
                # bilgi zamani: fiyatlar bu andan sonra kaydedilmis olmali (look-ahead yok)
                info_ts = self.archive.metar_info_ts(code, day, tick_ts, off) if self.info_prices else None
                cur_max = max(t for _ts, t in rows)
                pk_lock, cf_lock = detect_peak(rows, utc_offset_hours=off, confirmation_minutes=METAR_CONFIRM_MIN)
                avg_hour = avg_hours[code]
                early_attempt = avg_hour is not None
                trigger = "lock"
                if early_attempt and avg_hour is not None:
                    local_now_hr = ((tick_ts + off * 3600.0) % 86400.0) / 3600.0
                    if local_now_hr < avg_hour:
                        continue  # tahmini peak saati gelmedi (kilit kurali da calismaz)
                    peak: float | None = cur_max
                    confirmed = True
                    trigger = "lock" if (cf_lock and pk_lock is not None) else "early"
                else:
                    peak, confirmed = pk_lock, cf_lock
                if not confirmed or peak is None:
                    continue
                if early_attempt:
                    bkt = self._range_market(code, day, half_up(cur_max))
                    mkt_price = self._db_price(bkt["id"], tick_ts, not_before=info_ts) if bkt else None
                    if bkt is not None and mkt_price is None:
                        continue  # bilgi sonrasi fiyat henuz yok -> sonraki tick
                    if float(mkt_price or 0.0) > early_max:
                        if not cf_lock or pk_lock is None:
                            continue  # pahali + kilit yok -> bekle
                        peak = pk_lock
                        trigger = "lock"
                winner_val = float(cur_max) if cur_max > peak else float(peak)
                winner = half_up(winner_val)

                # AKTAR: kazanan bucket disindaki acik betleri kapat
                for pos in list(open_pos[code]):
                    if pos["bucket"] == winner:
                        continue
                    px = self._exit_price(pos["market_id"], tick_ts, not_before=info_ts)
                    if px is None:
                        continue  # bilgi sonrasi satis fiyati yok -> sonraki tick / settlement
                    pos["close_kind"] = "aktar"
                    pos["closed_at"] = tick_dt
                    pos["close_price"] = max(0.01, min(0.99, px))
                    open_pos[code].remove(pos)
                    result.trace.append(
                        {
                            "iso": tick_dt.isoformat(),
                            "day": day,
                            "kind": "metar_aktar_close",
                            "city_code": code,
                            "trigger": pos["trigger"],
                            "reason": f"{pos['bucket']}C -> {winner}C satis={pos['close_price']:.3f}",
                        }
                    )

                stale_age = (tick_ts - rows[-1][0]) / 60.0
                if stale_age > stale_lims[code]:
                    continue  # bayat veri: aktar calisti, YENI bet yok

                for market in self.archive.range_markets.get((code, day, winner), []):
                    first_seen = market.get("first_seen_ts")
                    if first_seen is not None and first_seen > tick_ts:
                        continue
                    if any(p["market_id"] == market["id"] for p in open_pos[code]):
                        continue  # bu markette acik bet var (canli _existing_metar_bet)
                    if opened_today >= self.metar_max_per_day:
                        continue  # gunluk cap (aktar cap'ten bagimsiz calisti)
                    db_price = self._db_price(market["id"], tick_ts, not_before=info_ts)
                    if db_price is None or not (METAR_MIN_ENTRY <= db_price < METAR_MAX_ENTRY):
                        continue
                    ask = self._ask_price(market["id"], tick_ts, not_before=info_ts)
                    entry = float(ask if ask is not None else db_price)
                    if not (METAR_MIN_ENTRY <= entry < METAR_MAX_ENTRY):
                        continue
                    fill = max(0.01, min(0.99, round(entry, 4)))
                    pos = {
                        "market": market,
                        "market_id": market["id"],
                        "code": code,
                        "bucket": winner,
                        "opened_at": tick_dt,
                        "entry": fill,
                        "shares": bet_shares(stake, fill),
                        "fee": polymarket_fee_from_stake(stake, fill, self.fee_rate),
                        "source": "orderbook" if ask is not None else "snapshot",
                        "trigger": trigger + ("+aktar" if code in had_pos else ""),
                        "peak": winner_val,
                        "close_kind": "settlement",
                        "closed_at": None,
                        "close_price": None,
                    }
                    open_pos[code].append(pos)
                    all_pos.append(pos)
                    had_pos.add(code)
                    opened_today += 1
                    result.trace.append(
                        {
                            "iso": tick_dt.isoformat(),
                            "day": day,
                            "kind": "metar_placed",
                            "city_code": code,
                            "trigger": pos["trigger"],
                            "reason": f"cur_max={cur_max:.1f}C bucket={winner} giris={fill:.3f}",
                        }
                    )
                    result.events.append(
                        {
                            "ts": tick_ts,
                            "iso": tick_dt.isoformat(),
                            "type": "bet_opened",
                            "city": self.archive.city_name.get(code, code),
                            "detail": f"metar {winner}C giris=${fill:.3f} ({pos['trigger']})",
                        }
                    )

        # -- settlement / kapanis PnL --------------------------------------
        day_end = datetime.fromisoformat(day + "T00:00:00") + timedelta(hours=24)
        for pos in all_pos:
            code = pos["code"]
            stake_f, shares, fee = float(stake), float(pos["shares"]), float(pos["fee"])
            settle_bucket: int | None = None
            if pos["close_kind"] == "aktar":
                won = False
                pnl = shares * float(pos["close_price"]) - stake_f - fee
                source = "aktar"
            else:
                outcome, settle_bucket, source = self._bet_outcome(
                    pos["market"], code, day, pos["bucket"], offsets.get(code, 0.0)
                )
                if outcome is None:
                    result.trace.append(
                        {
                            "iso": pos["opened_at"].isoformat(),
                            "day": day,
                            "kind": "metar_settle_yok",
                            "city_code": code,
                            "trigger": pos["trigger"],
                            "reason": f"market {pos['market_id']} sonucu bilinmiyor - PnL disi",
                        }
                    )
                    continue
                won = bool(outcome)
                pnl = (shares - stake_f - fee) if won else (-stake_f - fee)
            result.bets.append(
                BetRecord(
                    day=day,
                    opened_at=pos["opened_at"],
                    strategy="metar_peak",
                    market_id=pos["market_id"],
                    city=self.archive.city_name.get(code, code),
                    city_code=code,
                    threshold_c=float(pos["market"]["threshold"]),
                    bucket=int(pos["bucket"]),
                    entry_price=float(pos["entry"]),
                    stake_usd=stake_f,
                    shares=round(shares, 6),
                    settle_bucket=settle_bucket,
                    won=won,
                    realized_pnl=round(pnl, 6),
                    entry_fee=round(fee, 6),
                    price_source=f"{pos['source']}|settle={source}",
                    open_trigger=pos["trigger"],
                    close_kind=pos["close_kind"],
                    closed_at=pos["closed_at"] or day_end,
                    close_price=pos["close_price"],
                )
            )
