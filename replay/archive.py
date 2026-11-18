"""Arsiv erisimi (SALT-OKUNUR, look-ahead yasak).

Tum girdiler gercek arsiv DB'lerinden gelir; SAHTE VERI YOK:

* ``data/bot.db``             - weather_markets, market_snapshots (gamma fiyati),
                                metar_observations, weather_forecasts,
                                historical_calibrations
* ``data/orderbook.db``       - orderbook_snapshots (best_ask)
* ``data/backtest_prices.db`` - price_history
* ``data/actuals.db``         - actual_temperatures (settlement sicakligi)

Butun baglantilar ``mode=ro`` ile acilir; production DB'lere HICBIR yazi
gitmez.

LOOK-AHEAD DUZELTMESI (2026-08-23): naive zaman damgalarinda
``datetime.timestamp()`` MAKINE saat dilimini (UTC+3) varsayiyordu, bu yuzden
arsiv kayitlari ~3 saat ERKEN gorunuyor ve replay canlidan once bet aciyordu.
``_utc_epoch`` naive degeri acikca UTC kabul eder.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

BOT_DB = os.path.join(DATA_DIR, "bot.db")
BACKTEST_DB = os.path.join(DATA_DIR, "backtest.db")
ORDERBOOK_DB = os.path.join(DATA_DIR, "orderbook.db")
PRICES_DB = os.path.join(DATA_DIR, "backtest_prices.db")
ACTUALS_DB = os.path.join(DATA_DIR, "actuals.db")

#: Forecast kaynagi onceligi. `bot.db` yalnizca SON gunleri tutar
#: (2026-10-27 olcumu: 2026-10-17..10-29); gecmis pencere `backtest.db`'de
#: (1.106.701 satir, 2026-08-01..09-30). Ikisi zaman olarak cakismaz, birlikte
#: okunur.
FORECAST_DBS = (BOT_DB, BACKTEST_DB)

#: Arsivde look-ahead'e karsi okunacak en fazla geri gun (METAR/forecast).
META_LOOKBACK_DAYS = 2


def _utc_epoch(value) -> float | None:
    """Arsiv zaman damgasi -> UTC epok (naive = UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def half_up(value: float) -> int:
    """Polymarket bucket yuvarlamasi (Austin 35.9C -> 36)."""
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def _ro(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


class Archive:
    """Pencereye ait arsiv dilimleri; T-filtreli erisim yardimcilari."""

    def __init__(self, start_day: str, end_day: str):
        self.start_day = start_day
        self.end_day = end_day
        lo = datetime.fromisoformat(start_day) - timedelta(days=META_LOOKBACK_DAYS)
        self._meta_lo = lo.strftime("%Y-%m-%d %H:%M:%S")
        hi = datetime.fromisoformat(end_day) + timedelta(days=2)
        self._meta_hi = hi.strftime("%Y-%m-%d %H:%M:%S")
        # target_date karsilastirmalari TEXT; kapsayici ust sinir icin +1 gun.
        self._lo_date = start_day
        self._hi_date = (datetime.fromisoformat(end_day) + timedelta(days=1)).strftime("%Y-%m-%d")

        self.markets: dict[tuple[str, str, int], dict] = {}
        self.actuals: dict[tuple[str, str], float] = {}
        self.metar: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.forecasts: dict[tuple[str, str], list[tuple[float, str, float]]] = defaultdict(list)
        self.city_name: dict[str, str] = {}
        self.city_lon: dict[str, float] = {}
        self.city_bias: dict[str, float] = {}
        self.price_rows: list[tuple[str, str, float, float]] = []

        self._conns: list[sqlite3.Connection] = []
        self._load_markets()
        self._load_actuals()
        self._load_prices()
        self._load_metar()
        self._load_forecasts()
        self._load_bias()

    # -- yukleme -------------------------------------------------------
    def _open(self, path: str) -> sqlite3.Connection | None:
        con = _ro(path)
        if con is not None:
            self._conns.append(con)
        return con

    def _load_markets(self) -> None:
        con = self._open(BOT_DB)
        if con is None:
            return
        rows = con.execute(
            "SELECT id, city_code, city, threshold, target_date, market_type, "
            "       longitude, first_seen "
            "FROM weather_markets "
            "WHERE metric='temperature_max' AND threshold IS NOT NULL "
            "  AND target_date IS NOT NULL AND city_code IS NOT NULL"
        )
        for mid, code, city, thr, tdate, mtype, lon, first_seen in rows:
            day = str(tdate)[:10]
            if not (self.start_day <= day <= self.end_day):
                continue
            try:
                thr_f = float(thr)
            except (TypeError, ValueError):
                continue
            key = (str(code), day, half_up(thr_f))
            self.markets.setdefault(
                key,
                {
                    "id": str(mid),
                    "city": city,
                    "city_code": str(code),
                    "threshold": thr_f,
                    "day": day,
                    "market_type": mtype,
                    "first_seen_ts": _utc_epoch(first_seen),
                },
            )
            if code and city:
                self.city_name.setdefault(str(code), str(city))
                if lon is not None:
                    try:
                        self.city_lon.setdefault(str(code), float(lon))
                    except (TypeError, ValueError):
                        pass

    def _load_actuals(self) -> None:
        con = self._open(ACTUALS_DB)
        if con is None:
            return
        for city, day, tmax in con.execute(
            "SELECT city, date, temperature_2m_max FROM actual_temperatures WHERE temperature_2m_max IS NOT NULL"
        ):
            if city and day:
                self.actuals[(str(city), str(day)[:10])] = float(tmax)

    def _load_prices(self) -> None:
        """Uc fiyat kaynagini TEK listeye topla (kaynak etiketiyle).

        2026-10-27: yalnizca PENCEREDEKI marketlerin fiyatlari yuklenir.
        Onceden tum arsiv taranıyordu (orderbook.db 4 GB) ve tek gunluk bir
        replay 4 dakika suruyordu. `market_snapshots` / `orderbook_snapshots`
        `target_date` tasidigi icin SQL'de filtre uygulanir; `price_history`
        tasimadigi icin market_id kumeleri halinde sorgulanir.
        """
        market_ids = {m["id"] for m in self.markets.values()}

        con = self._open(BOT_DB)
        if con is not None:
            try:
                for mid, py, st in con.execute(
                    "SELECT market_id, yes_price, snapshot_time FROM market_snapshots "
                    "WHERE yes_price IS NOT NULL AND snapshot_time IS NOT NULL "
                    "  AND target_date >= ? AND target_date < ?",
                    (self._lo_date, self._hi_date),
                ):
                    self._add_price("snapshot", mid, st, py)
            except sqlite3.OperationalError:
                pass

        con = self._open(ORDERBOOK_DB)
        if con is not None:
            try:
                for mid, ask, st in con.execute(
                    "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots "
                    "WHERE best_ask IS NOT NULL AND snapshot_time IS NOT NULL "
                    "  AND target_date >= ? AND target_date < ?",
                    (self._lo_date, self._hi_date),
                ):
                    self._add_price("orderbook", mid, st, ask)
            except sqlite3.OperationalError:
                pass

        con = self._open(PRICES_DB)
        if con is not None and market_ids:
            ids = list(market_ids)
            try:
                for start in range(0, len(ids), 500):
                    chunk = ids[start : start + 500]
                    marks = ",".join("?" * len(chunk))
                    for mid, ts, price in con.execute(
                        f"SELECT market_id, ts, price FROM price_history "
                        f"WHERE price > 0 AND price <= 1 AND market_id IN ({marks})",
                        chunk,
                    ):
                        self._add_price("price_history", mid, ts, price)
            except sqlite3.OperationalError:
                pass

    def _add_price(self, source: str, market_id, ts, price) -> None:
        t = _utc_epoch(ts)
        if t is None:
            return
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if not (0.0 < p <= 1.0):
            return
        self.price_rows.append((str(market_id), source, t, p))

    def _load_metar(self) -> None:
        con = self._open(BOT_DB)
        if con is None:
            return
        for code, temp, obs in con.execute(
            "SELECT city_code, temp_c, obs_time FROM metar_observations "
            "WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL "
            "  AND obs_time >= ? AND obs_time <= ?",
            (self._meta_lo, self._meta_hi),
        ):
            t = _utc_epoch(obs)
            if t is None or not code:
                continue
            self.metar[str(code)].append((t, float(temp)))
        for rows in self.metar.values():
            rows.sort(key=lambda r: r[0])

    def _load_forecasts(self) -> None:
        seen: set[tuple[str, str, float, str]] = set()
        for path in FORECAST_DBS:
            con = self._open(path)
            if con is None:
                continue
            try:
                cursor = con.execute(
                    "SELECT city, target_date, source, fetched_at, predicted_value "
                    "FROM weather_forecasts WHERE predicted_value IS NOT NULL "
                    "  AND fetched_at >= ? AND fetched_at <= ?",
                    (self._meta_lo, self._meta_hi),
                )
            except sqlite3.OperationalError:
                continue
            for code, tdate, model, fetched, value in cursor:
                if not code or not tdate or fetched is None:
                    continue
                day = str(tdate)[:10]
                if not (self.start_day <= day <= self.end_day):
                    continue
                t = _utc_epoch(fetched)
                if t is None:
                    continue
                key = (str(code), day, t, str(model or ""))
                if key in seen:
                    continue
                seen.add(key)
                self.forecasts[(str(code), day)].append((t, str(model or ""), float(value)))
        for rows in self.forecasts.values():
            rows.sort(key=lambda r: r[0])

    def _load_bias(self) -> None:
        con = self._open(BOT_DB)
        if con is None:
            return
        acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        try:
            rows = con.execute(
                "SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL AND city_code IS NOT NULL"
            )
            for code, bias in rows:
                acc[str(code)][0] += abs(float(bias))
                acc[str(code)][1] += 1.0
        except (sqlite3.OperationalError, TypeError, ValueError):
            return
        self.city_bias = {c: v[0] / v[1] for c, v in acc.items() if v[1] > 0}

    # -- T-filtreli erisim --------------------------------------------
    def forecast_before(self, code: str, day: str, t: float) -> float | None:
        """`t`'den ONCE fetch edilmis EN SON batch'in model ortalamasi."""
        rows = [(ft, val) for ft, _model, val in self.forecasts.get((code, day), []) if ft <= t]
        if not rows:
            return None
        latest = max(ft for ft, _val in rows)
        vals = [v for ft, v in rows if ft == latest]
        return sum(vals) / len(vals) if vals else None

    def metar_day(self, code: str, day: str, t: float, utc_offset_hours: float = 0.0):
        """Yerel `day` penceresine giren, `t`'den onceki METAR gozlemleri."""
        out: list[tuple[float, float]] = []
        for ts, temp in self.metar.get(code, []):
            if ts > t:
                break
            local = datetime.fromtimestamp(ts + utc_offset_hours * 3600, tz=timezone.utc)
            if local.strftime("%Y-%m-%d") == day:
                out.append((ts, temp))
        return out

    def settlement_bucket(self, code: str, day: str) -> int | None:
        """Gercek kapanis bucket'i (actuals tmax -> half-up)."""
        name = self.city_name.get(code)
        if not name:
            return None
        tmax = self.actuals.get((name, day))
        return None if tmax is None else half_up(tmax)

    def cities_by_bias(self) -> list[str]:
        """Tahmini EN AZ SAPAN sehriler once (|bias| kucuk -> iyi)."""
        return [c for c, _ in sorted(self.city_bias.items(), key=lambda kv: kv[1])]

    def close(self) -> None:
        for con in self._conns:
            try:
                con.close()
            except sqlite3.Error:
                pass
        self._conns.clear()
