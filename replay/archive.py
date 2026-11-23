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
# 2026-11-20: yollar CALL TIME'da cozulur. Onceden REPO_ROOT/data sabitti;
# canli DB 2026-11-18'de C:\JUNBO data'ya tasindi ve testler DB_PATH'i
# degistirdigi icin import-anindaki sabit yol bos DB'ye isaret ediyordu.


def _path(name: str) -> str:
    """Live data dizinindeki DB yolu (JUNBO_DATA_DIR/DB_PATH aware)."""
    from utils.db_paths import get_data_dir

    return os.path.join(get_data_dir(), name)


def _forecast_dbs() -> tuple[str, str]:
    #: bot.db yalnizca SON gunleri tutar (2026-10-27: 2026-10-17..10-29);
    #: gecmis pencere backtest.db'de (2026-08-01..09-30). Cakismaz, birlikte okunur.
    return (_path("bot.db"), _path("backtest.db"))


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
    """Polymarket bucket yuvarlamasi (Austin 35.9C -> 36, -8.5C -> -8).

    2026-11-22: canli kodla ayni tek kaynak (scrapers.metar.polymarket_round).
    """
    from scrapers.metar import polymarket_round

    return polymarket_round(value)


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
        #: 2026-11-23 hibrit replay: (kod, gun, bucket) -> RANGE marketleri
        #: (canli `run_metar_peak_bets` yalnizca RANGE + temperature_max'a girer).
        self.range_markets: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
        self.market_by_id: dict[str, dict] = {}
        self.actuals: dict[tuple[str, str], float] = {}
        self.metar: dict[str, list[tuple[float, float]]] = defaultdict(list)
        #: (obs_epoch, temp, gorulme_epoch) — gorulme = `recorded_at` (botun
        #: gozlemi ILK gordugu an). Hibrit replay look-ahead'i bununla engeller.
        self.metar_seen: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
        self._peak_hours: dict[str, list[tuple[str, float]]] | None = None
        self._live_outcomes: dict[str, bool] | None = None
        self._day_obs_cache: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
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
        con = self._open(_path("bot.db"))
        if con is None:
            return
        from utils.market_outcome import parse_resolved_outcome

        rows = con.execute(
            "SELECT id, city_code, city, threshold, target_date, market_type, "
            "       longitude, first_seen, raw_data "
            "FROM weather_markets "
            "WHERE metric='temperature_max' AND threshold IS NOT NULL "
            "  AND target_date IS NOT NULL AND city_code IS NOT NULL"
        )
        for mid, code, city, thr, tdate, mtype, lon, first_seen, raw in rows:
            day = str(tdate)[:10]
            if not (self.start_day <= day <= self.end_day):
                continue
            try:
                thr_f = float(thr)
            except (TypeError, ValueError):
                continue
            key = (str(code), day, half_up(thr_f))
            info = {
                "id": str(mid),
                "city": city,
                "city_code": str(code),
                "threshold": thr_f,
                "day": day,
                "market_type": mtype,
                "first_seen_ts": _utc_epoch(first_seen),
                # Polymarket RESMI sonucu (True=YES, False=NO, None=cozulmemis)
                "poly_yes": parse_resolved_outcome(raw),
            }
            self.markets.setdefault(key, info)
            self.market_by_id[info["id"]] = info
            if mtype == "RANGE":
                self.range_markets[key].append(info)
            if code and city:
                self.city_name.setdefault(str(code), str(city))
                if lon is not None:
                    try:
                        self.city_lon.setdefault(str(code), float(lon))
                    except (TypeError, ValueError):
                        pass

    def _load_actuals(self) -> None:
        con = self._open(_path("actuals.db"))
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

        con = self._open(_path("bot.db"))
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

        con = self._open(_path("orderbook.db"))
        if con is not None:
            try:
                for mid, ask, bid, st in con.execute(
                    "SELECT market_id, best_ask, best_bid, snapshot_time FROM orderbook_snapshots "
                    "WHERE snapshot_time IS NOT NULL "
                    "  AND target_date >= ? AND target_date < ?",
                    (self._lo_date, self._hi_date),
                ):
                    if ask is not None:
                        self._add_price("orderbook", mid, st, ask)
                    # 2026-11-23: BID ayri kaynak ("orderbook_bid") — PRIORITY'de
                    # YOK, giris fiyatina karismaz; yalnizca aktar kapanisinda okunur.
                    if bid is not None:
                        self._add_price("orderbook_bid", mid, st, bid)
                    # orta fiyat: canli DB yes_price (gamma ~5 dk) icin snapshot'tan
                    # (30 dk) daha taze vekil; hibrit erken-giris esigi bunu okur.
                    if ask is not None and bid is not None:
                        self._add_price("orderbook_mid", mid, st, (float(ask) + float(bid)) / 2.0)
            except sqlite3.OperationalError:
                pass

        con = self._open(_path("backtest_prices.db"))
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
        con = self._open(_path("bot.db"))
        if con is None:
            return
        for code, temp, obs, seen in con.execute(
            "SELECT city_code, temp_c, obs_time, recorded_at FROM metar_observations "
            "WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL "
            "  AND obs_time >= ? AND obs_time <= ?",
            (self._meta_lo, self._meta_hi),
        ):
            t = _utc_epoch(obs)
            if t is None or not code:
                continue
            self.metar[str(code)].append((t, float(temp)))
            s = _utc_epoch(seen)
            # gozlem, yapilmadan once gorulemez -> gorulme >= obs zamani
            self.metar_seen[str(code)].append((t, float(temp), t if s is None else max(s, t)))
        for rows in self.metar.values():
            rows.sort(key=lambda r: r[0])
        for seen_rows in self.metar_seen.values():
            seen_rows.sort(key=lambda r: r[0])

    def _load_forecasts(self) -> None:
        seen: set[tuple[str, str, float, str]] = set()
        for path in _forecast_dbs():
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
        con = self._open(_path("bot.db"))
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

    # -- hibrit METAR-peak replay (2026-11-23) ------------------------------
    def _day_obs(self, code: str, day: str, utc_offset_hours: float) -> list[tuple[float, float, float]]:
        """Yerel `day` penceresindeki TUM arsiv gozlemleri (obs, temp, gorulme)."""
        key = (code, day)
        cached = self._day_obs_cache.get(key)
        if cached is not None:
            return cached
        out = []
        for ts, temp, seen in self.metar_seen.get(code, []):
            local = datetime.fromtimestamp(ts + utc_offset_hours * 3600, tz=timezone.utc)
            if local.strftime("%Y-%m-%d") == day:
                out.append((ts, temp, seen))
        self._day_obs_cache[key] = out
        return out

    def metar_seen_day(self, code: str, day: str, t: float, utc_offset_hours: float = 0.0):
        """`t` aninda BOTUN GORMUS OLDUGU yerel-gun gozlemleri [(obs, temp)].

        Look-ahead yok: gozlem `recorded_at` (ilk kayit = botun gordugu an)
        `t`'den sonraysa gorunmez. Canli `_merged_day_rows` ile ayni pencere.
        """
        return [(ts, temp) for ts, temp, seen in self._day_obs(code, day, utc_offset_hours) if seen <= t]

    def metar_info_ts(self, code: str, day: str, t: float, utc_offset_hours: float = 0.0) -> float | None:
        """`t` aninda gorulen EN YENI gozlemin gorulme ani (bilgi zamani).

        Hibrit replay fiyatlari bu andan ONCE kaydedilmis olamaz: piyasa METAR'a
        saniyeler icinde tepki verir; bilgiden onceki fiyatla dolum look-ahead'dir.
        """
        seen = [s for _ts, _temp, s in self._day_obs(code, day, utc_offset_hours) if s <= t]
        return max(seen) if seen else None

    def metar_final_bucket(self, code: str, day: str, utc_offset_hours: float = 0.0) -> int | None:
        """Gunun TUM arsiv gozlemlerinden kazanan bucket (Polymarket sonucu yoksa yedek)."""
        rows = self._day_obs(code, day, utc_offset_hours)
        if not rows:
            return None
        return half_up(max(temp for _ts, temp, _seen in rows))

    def _load_peak_hours(self) -> None:
        """Sehir -> [(yerel_gun, peak_saati)] — canli `_avg_peak_hour` girdisi.

        Canli bot TUM `metar_observations` gecmisini kullanir (pencereyle
        sinirli degil); bu yuzden tablo pencereden bagimsiz bir kez okunur.
        """
        from scrapers.metar import city_utc_offset

        self._peak_hours = {}
        con = self._open(_path("bot.db"))
        if con is None:
            return
        offsets: dict[tuple[str, str], float] = {}

        def _off(code: str, day: str) -> float:
            k = (code, day)
            if k not in offsets:
                try:
                    offsets[k] = float(city_utc_offset(code, day, self.city_lon.get(code)))
                except Exception:  # noqa: BLE001 - zaman dilimi cozulemezse UTC
                    offsets[k] = 0.0
            return offsets[k]

        by_day: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        for code, temp, obs in con.execute(
            "SELECT city_code, temp_c, obs_time FROM metar_observations "
            "WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL AND obs_time < ?",
            (self._meta_hi,),
        ):
            t = _utc_epoch(obs)
            if t is None or not code:
                continue
            code = str(code)
            off = _off(code, str(obs)[:10])
            local_day = datetime.fromtimestamp(t + off * 3600, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[(code, local_day)].append((t, float(temp)))
        for (code, local_day), arr in by_day.items():
            if len(arr) < 3:
                continue  # yarim gun veri — peak saati guvenilmez (canli ile ayni)
            arr.sort(key=lambda r: r[0])
            ts_max = max(arr, key=lambda r: r[1])[0]  # ilk (en erken) max
            loc = datetime.fromtimestamp(ts_max + _off(code, local_day) * 3600, tz=timezone.utc)
            self._peak_hours.setdefault(code, []).append((local_day, loc.hour + loc.minute / 60.0))

    def avg_peak_hour(self, code: str, day: str, min_days: int = 3) -> float | None:
        """`day`'DEN ONCEKI gunlerin ortalama yerel peak saati (look-ahead yok)."""
        if self._peak_hours is None:
            self._load_peak_hours()
        hours = [h for local_day, h in (self._peak_hours or {}).get(code, []) if local_day < day]
        if len(hours) < min_days:
            return None
        return sum(hours) / len(hours)

    def stale_limit_min(self, code: str, t: float) -> float:
        """Canli `_stale_threshold_min`: son 40 gozlem kadansi >= 55 dk -> 90, yoksa 45."""
        rows = [ts for ts, _temp, seen in self.metar_seen.get(code, []) if seen <= t]
        rows = rows[-40:]
        if len(rows) < 4:
            return 45.0
        gaps = sorted(b - a for a, b in zip(rows, rows[1:]) if b - a >= 300.0)
        if not gaps:
            return 45.0
        return 90.0 if gaps[len(gaps) // 2] / 60.0 >= 55.0 else 45.0

    def _load_live_outcomes(self) -> None:
        """Canli YES betlerinin won/lost durumu -> market sonucu (ikinci kaynak)."""
        self._live_outcomes = {}
        con = self._open(_path("bot.db"))
        if con is None:
            return
        try:
            for mid, status in con.execute(
                "SELECT market_id, status FROM bets WHERE status IN ('won','lost') AND (side='YES' OR side IS NULL)"
            ):
                if mid is not None:
                    self._live_outcomes[str(mid)] = status == "won"
        except sqlite3.OperationalError:
            return

    def market_outcome(self, market_id: str) -> tuple[bool | None, str]:
        """Marketin GERCEK sonucu: (YES kazandi mi, kaynak).

        Oncelik: Polymarket resmi cozum (`raw_data`) > canli betin won/lost
        durumu > (None, "yok"). Cagiran gerekirse METAR yedegine duser.
        """
        info = self.market_by_id.get(str(market_id))
        if info is not None and info.get("poly_yes") is not None:
            return bool(info["poly_yes"]), "polymarket"
        if self._live_outcomes is None:
            self._load_live_outcomes()
        live = (self._live_outcomes or {}).get(str(market_id))
        if live is not None:
            return live, "canli_bet"
        return None, "yok"

    def close(self) -> None:
        for con in self._conns:
            try:
                con.close()
            except sqlite3.Error:
                pass
        self._conns.clear()
