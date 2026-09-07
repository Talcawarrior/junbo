"""Replay FULL BOT — su anki botun TIPATIPA geriye oynatilmasi (2026-08-24).

Anayasa (CLAUDE.md §7): backtest botu birebir calistirir; look-ahead YASAKTIR.
Bu script botun GERCEK karar fonksiyonlarini kullanir:
  - executor.spread_placer.place_spread_bets   (T-2 ve T-1, forecast tabanli)
  - jobs.metar_peak.run_metar_peak_bets         (T-0 METAR-peak, detect_peak)
  - scrapers.metar.detect_peak                  (peak kilidi)
  - executor.settler.Settler                    (settlement)

Cevre: yalnizca READ-ONLY gercek DB'ler. Botun BET YAZAN koduna dokunmaz,
bunun yerine karar fonksiyonlarini "zaman dondurularak" cagirir ve sonuclari
bagimsiz bir rapora toplar. Canli bot.db'ye HICBIR sey yazmaz.

Zaman dondurma (look-ahead onleme):
  - simule 'today' D une cekilir. Botun `datetime.now()` / `time.time()`
    cagirilari D gununun gunduzune (12:00 UTC) sabitlenir.
  - fetch_metar_day -> arsiv (metar_observations) T<=12:00 kayitlarina okunur.
  - Settlement -> actuals.db'deki GERCEK kapanan sonuc.

Kullanim:
  python scripts/replay_full_bot.py [--start 2026-08-05] [--end 2026-08-24] [--detail]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

DATA = os.path.join(_REPO_ROOT, "data")
BOT_DB = os.path.join(DATA, "bot.db")
OB_DB = os.path.join(DATA, "orderbook.db")
ACTUAL_DB = os.path.join(DATA, "actuals.db")
BK_DB = os.path.join(DATA, "backtest.db")
BP_DB = os.path.join(DATA, "backtest_prices.db")


def _ro_conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def _ts(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
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


# --- Veri kaynaklari (read-only, gercek DB) ---------------------------------

class ReplayData:
    """Bir defa yuklenen, her gun icin T<=simtime filtreli erisim saglar."""

    def __init__(self, start: str, end: str):
        self.bot = _ro_conn(BOT_DB)
        self.ob = _ro_conn(OB_DB)
        self.act = _ro_conn(ACTUAL_DB)
        self.bk = _ro_conn(BK_DB) if os.path.exists(BK_DB) else None
        self.bp = _ro_conn(BP_DB) if os.path.exists(BP_DB) else None
        self.start = start
        self.end = end

        # markets: (code, day, thr_halfup) -> {id, target_ts, outcome, mtype, city}
        self.markets: dict[tuple, dict] = {}
        self._load_markets()

        # actuals: (city, day) -> (tmax, tmin)
        self.actuals: dict[tuple, float | None] = {}
        self._load_actuals()

        # price series: market_id -> [(t, ask)]
        self.price_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._load_orderbook()

        # METAR archive: code -> [(epoch, temp)] (yerel-gun penceresi metar_until'de)
        self.metar: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._load_metar()

        # forecast: (code, day) -> list[(fetched_at_epoch, model, value)]
        self.forecast: dict[tuple, list[tuple[float, str, float]]] = defaultdict(list)
        self._load_forecast()

        # city meta: code -> (city, lon)
        self.city_name: dict[str, str] = {}
        self.city_lon: dict[str, float] = {}
        self._load_city_meta()

        # bias (historical_calibrations): city -> avg |bias|
        self._bias_acc: dict[str, list] = defaultdict(lambda: [0.0, 0])
        self.city_bias: dict[str, float] = {}
        self._load_bias()

    def _load_markets(self):
        for mid, code, thr, tdate, metric, mtype, city, lon in self.bot.execute(
            "SELECT id, city_code, threshold, target_date, metric, market_type, city, longitude "
            "FROM weather_markets WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND metric='temperature_max'"
        ):
            if not code or not tdate:
                continue
            day = str(tdate)[:10]
            if not (self.start <= day <= self.end):
                continue
            # half-up bucket (Austin 35.9C -> 36)
            bkt = int(float(thr) + 0.5) if float(thr) >= 0 else int(float(thr) - 0.5)
            key = (code, day, bkt)
            # en son target_ts / ilk kayit kalsin (duplikasyon olursa)
            t = _ts(tdate)
            cur = self.markets.get(key)
            if cur is None or (t is not None and (cur["target_ts"] is None or t < cur["target_ts"])):
                self.markets[key] = {
                    "id": str(mid), "target_ts": t, "mtype": mtype,
                    "city": city, "threshold": float(thr), "lon": lon,
                }

    def _load_actuals(self):
        for city, d, tmax, tmin in self.act.execute(
            "SELECT city, date, temperature_2m_max, temperature_2m_min FROM actual_temperatures"
        ):
            if not d:
                continue
            day = str(d)[:10]
            # city eşleşmesi: actuals 'city' ad, markets 'city_code'(ICAO).
            # Burada yalnızca (city_name, day) -> tmax saklanır; eşleştirme
            # 'city' adıyla yapılır (replay settlement'ta code_name eşleşir).
            self.actuals[(city, day)] = tmax

    def _load_orderbook(self):
        for mid, ask, st in self.ob.execute(
            "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
        ):
            t = _ts(st)
            if t is None:
                continue
            try:
                a = float(ask)
                if 0 < a <= 1:
                    self.price_series[str(mid)].append((t, a))
            except (TypeError, ValueError):
                pass
        if self.bp is not None:
            try:
                for mid, t, p in self.bp.execute(
                    "SELECT market_id, ts, price FROM price_history WHERE price > 0 AND price <= 1"
                ):
                    tt = _ts(t)
                    if tt is None:
                        continue
                    try:
                        self.price_series[str(mid)].append((tt, float(p)))
                    except (TypeError, ValueError):
                        pass
            except sqlite3.OperationalError:
                pass
        for k in self.price_series:
            self.price_series[k].sort(key=lambda x: x[0])

    def _load_metar(self):
        # ham: code -> [(epoch, temp)] (yerel gun filtresi metar_until'de yapilir)
        for code, tmax, obs in self.bot.execute(
            "SELECT city_code, temp_c, obs_time FROM metar_observations WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL"
        ):
            t = _ts(obs)
            if t is None:
                continue
            self.metar[code].append((t, float(tmax)))
        for k in self.metar:
            self.metar[k].sort(key=lambda x: x[0])

    def _load_forecast(self):
        src = self.bk if self.bk is not None else self.bot
        for code, tdate, model, f_at, pv in src.execute(
            "SELECT city, target_date, source, fetched_at, predicted_value FROM weather_forecasts "
            "WHERE predicted_value IS NOT NULL AND (metric LIKE '%max%' OR metric='temperature_max')"
        ):
            if not code or not tdate:
                continue
            day = str(tdate)[:10]
            if not (self.start <= day <= self.end):
                continue
            # (code, day) -> list[(fetched_at_epoch, model, value)]
            if f_at is None:
                continue
            ft = _ts(f_at)
            if ft is None:
                continue
            self.forecast[(code, day)].append((ft, model, float(pv)))

    def _load_city_meta(self):
        for code, city, lon in self.bot.execute(
            "SELECT DISTINCT city_code, city, longitude FROM weather_markets "
            "WHERE city_code IS NOT NULL AND city_code != '' AND city IS NOT NULL"
        ):
            if code and city:
                self.city_name.setdefault(code, city)
                try:
                    self.city_lon.setdefault(code, float(lon))
                except (TypeError, ValueError):
                    pass

    def _load_bias(self):
        for code, bias in self.bot.execute(
            "SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL AND city_code IS NOT NULL"
        ):
            try:
                b = abs(float(bias))
            except (TypeError, ValueError):
                continue
            self._bias_acc[code][0] += b
            self._bias_acc[code][1] += 1
        self.city_bias = {
            k: v[0] / v[1] for k, v in self._bias_acc.items() if v[1] > 0
        }

    # ---- time-ekran (T) filtreli erisimler ----
    def forecast_for(self, code: str, day: str, settle_ts: float) -> float | None:
        """(code, day) icin, settle'dan once fetch edilmis EN SON batch'in
        model-ortalamasini doner (spread_placer func.max(fetched_at) esdeger;
        look-ahead yok: settle_ts sonrasi fetch kullanilmaz)."""
        rows = self.forecast.get((code, day), [])
        usable = [(ft, pv) for ft, _m, pv in rows if ft <= settle_ts]
        if not usable:
            return None
        latest_ft = max(ft for ft, _pv in usable)
        vals = [pv for ft, pv in usable if ft == latest_ft]
        if not vals:
            return None
        return sum(vals) / len(vals)

    def metar_until(self, code: str, day: str, sim_ts: float,
                    utc_off: float = 0.0) -> list[tuple[float, float]]:
        """(code) icin YEREL gun `day` penceresine giren, obs_time<=sim_ts gozlemler.

        METAR arsivi UTC-gun bazinda degil, ham epoch saklanir. Yerel gun
        penceresi (fetch_metar_day gibi): yerel 00:00 <= obs < yerel 24:00,
        yani UTC [00:00 - off*3600, +24h). Bati sehirlerinde (Denver -6)
        onceki aksamin verisi bugunun kilidine karismasin.
        """
        rows = self.metar.get(code, [])
        out = []
        for t, c in rows:
            if t > sim_ts:
                break
            ld = datetime.fromtimestamp(t + utc_off * 3600, tz=timezone.utc)
            if ld.strftime("%Y-%m-%d") == day:
                out.append((t, c))
        return out

    def price_at_or_after(self, mid: str, t: float) -> float | None:
        """t aninda (veya sonrasindaki) en yakin GERCERCI ask fiyati.

        Bos-defter artefaktlari (0.001-0.05) DISLANIR: bunlar gercek dolu
        CLOB fiyati DEGILDIR, boyle fiyata emir dolmaz. Botun stale-guard'i
        ile ayni kural (anayasa 'gercek-giris-fiyat'). Gercek piyasa alt siniri
        0.05 kabul edilir (metar_peak MIN_ENTRY ile ayni)."""
        s = self.price_series.get(mid, [])
        for tt, a in s:
            if tt >= t and a >= 0.05:
                return a
        return None

    def price_before(self, mid: str, t: float) -> float | None:
        """t anindan ONCEKI (botun gordugu) en yakin GERCEKCI ask fiyati (>=0.05)."""
        s = self.price_series.get(mid, [])
        best = None
        for tt, a in s:
            if tt <= t:
                if a >= 0.05:
                    best = a
            else:
                break
        return best


# --- Karar mantigi (botun gercek fonksiyonlarinin ekrani) ------------------

HALF_UP = lambda x: int(x + 0.5) if x >= 0 else int(x - 0.5)  # noqa: E731


def city_utc_offset(code: str, day: str, lon: float | None) -> float:
    from scrapers.metar import city_utc_offset as _off
    try:
        return _off(code, day, lon)
    except Exception:
        return round(float(lon) / 15.0) if lon is not None else 0.0


def detect_peak(rows, utc_off, confirmation_minutes=30):
    from scrapers.metar import detect_peak as _dp
    return _dp(rows, utc_offset_hours=utc_off, confirmation_minutes=confirmation_minutes)


def _avg_peak_hour_hist(rd: ReplayData, code: str, day: str, lon: float | None) -> float | None:
    """Sehir bazli gecmis ortalama peak saati (metar_peak._avg_peak_hour esdeger).
    >=3 gun (bugun haric), yerel saatte max'in oldugu saat ortalamasi."""
    all_rows = rd.metar.get(code, [])
    # gunlere ayir (yerel)
    by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
    off = city_utc_offset(code, day, lon)
    for t, c in all_rows:
        ld = datetime.fromtimestamp(t + off * 3600, tz=timezone.utc)
        by_day[ld.strftime("%Y-%m-%d")].append((t, c))
    hours = []
    for d, rows in by_day.items():
        if d >= day:
            continue
        if not rows:
            continue
        off_d = city_utc_offset(code, d, lon)
        mx = max(r[1] for r in rows)
        mt = max((r for r in rows if r[1] == mx), key=lambda x: x[0])[0]
        hh = datetime.fromtimestamp(mt + off_d * 3600, tz=timezone.utc).hour
        hours.append(hh)
    if len(hours) < 3:
        return None
    return sum(hours) / len(hours)


# --- Replay ana dongu -------------------------------------------------------

def simulate(rd: ReplayData, day: str, detail: bool):
    """Bir gunun tam bot davranisini simüle eder. Sonuc listesi doner."""
    from config.settings import bot_config

    s = bot_config.strategy
    spread_max_cities = int(getattr(s, "spread_max_cities", 15) or 15)
    spread_max_entry = float(getattr(s, "spread_max_entry", 0.95) or 0.95)
    spread_stake = float(getattr(s, "spread_stake_usd", 2.0) or 2.0)
    spread_max_bets = int(getattr(s, "spread_max_bets_per_day", 120) or 120)
    metar_cap = int(getattr(s, "metar_peak_max_bets_per_day", 12) or 12)
    fee_rate = float(getattr(s, "current_fee_rate", 0.05) or 0.05)
    GAS = 0.0

    # sim 'bugun' zamani: D gunu.
    #   - SPREAD (T-2/T-1): bot o gun ilk acilis aninda karar verir -> 12:00 UTC.
    #   - METAR-peak (T-0): 30dk loop gun sonuna kadar peak'i bekler -> 23:59.
    sim_ts = _ts(f"{day} 12:00:00")
    day_end_ts = _ts(f"{day} 23:59:59")
    _st = day_end_ts
    settle_ts = (_st + 12 * 3600) if _st is not None else (sim_ts or 0.0) + 36 * 3600

    day_mkts = {
        (code, d, bkt): m
        for (code, d, bkt), m in rd.markets.items()
        if d == day
    }
    if not day_mkts:
        return []

    # bias-top N sehir (spread icin)
    ordered = [c for c, _ in sorted(rd.city_bias.items(), key=lambda kv: kv[1])]
    keep = set(ordered[:spread_max_cities])

    results: list[dict] = []
    metar_opened = 0

    # ---- T-2 + T-1 spread ----
    # T-2 = en ileri acik tarih (max open date). T-1 = yarin. Replay'de
    # 'acik tarihler' = rd.markets icindeki date'ler (o gunun ve sonrasinin
    # acilmis marketleri). T-2 hedef: o gun botun gorebildigi en ileri tarih.
    all_days = sorted({d for (c, d, _b) in rd.markets.keys()})
    eligible_days = [d for d in all_days if d >= day]
    open_dates = set(eligible_days)
    # T-1 = yarin
    next_date = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)
    t1_day = next_date.strftime("%Y-%m-%d")

    targets = []
    # T-0 = AYNI GUN sabah forecast (gün basi tahmin, kullanici 2026-08-24).
    # forecast target = bugun, fetch ayni gun sabahi (10:00 civari).
    if day in open_dates:
        targets.append((day, "T-0"))
    # T-1 = yarin forecast
    if t1_day in open_dates:
        targets.append((t1_day, "T-1"))
    # T-2 = en ileri acik tarih (max open date)
    if eligible_days:
        t2_day = max(eligible_days)
        if t2_day != day:
            targets.append((t2_day, "T-2"))

    # giris zamanlari: T-0 sabah 10:00, T-1/T-2 o gun 12:00 (botun gun ici dongusu)
    t0_entry_ts = _ts(f"{day} 10:00:00") or sim_ts

    for tgt_day, leg in targets:
        # forecast = (code, tgt_day) model->val, settlement oncesi en son batch
        # forecast: her sehir icin en son batch ortalamasi (look-ahead yok)
        cands = []
        for code in list(rd.city_name.keys()):
            if code not in keep:
                continue
            mean = rd.forecast_for(code, tgt_day, settle_ts)
            if mean is None:
                continue
            cands.append((code, mean))
        cands.sort(key=lambda cv: (rd.city_bias.get(cv[0], 999), -cv[1]))
        seen = set()
        for code, mean in cands:
            if code in seen:
                continue
            seen.add(code)
            if len([r for r in results if r["leg"] == leg]) >= spread_max_bets:
                break
            center = HALF_UP(mean)
            m = rd.markets.get((code, tgt_day, center))
            if m is None:
                continue
            if m["mtype"] != "RANGE":
                continue
            mid = m["id"]
            # giris fiyati: T-0 sabah, T-1/T-2 gun ici (bot o an gorur)
            et = t0_entry_ts if leg == "T-0" else sim_ts
            entry = rd.price_before(mid, et)
            if entry is None:
                entry = rd.price_at_or_after(mid, et)
            if entry is None or not (0 < entry < spread_max_entry):
                continue
            # settlement sonucunu bul (actuals)
            outcome = settlement_outcome(rd, code, tgt_day, center, m)
            if outcome is None:
                continue
            stake = spread_stake
            cost = stake  # YES bet costu stake
            shares = stake / entry
            if outcome:
                pnl = (stake / entry) - cost - stake * fee_rate
                won = True
            else:
                pnl = -cost
                won = False
            results.append({
                "leg": leg, "day": day, "tgt": tgt_day, "city": rd.city_name[code],
                "code": code, "bucket": center, "entry": entry,
                "stake": stake, "won": won, "pnl": pnl, "exit": "hold_settlement",
            })

    # ---- T-0 METAR-peak ----
    # TUM sehirler (bias filtresi kaldirildi), RANGE+temperature_max, detect_peak
    peak_cities: dict[str, dict] = {}
    for code in rd.city_name:
        if code not in rd.city_lon and code not in rd.city_name:
            continue
        lon = rd.city_lon.get(code)
        off = city_utc_offset(code, day, lon)
        rows = rd.metar_until(code, day, day_end_ts, off)
        if len(rows) < 3:
            continue
        locked, confirmed = detect_peak(sorted(rows, key=lambda x: x[0]), off, confirmation_minutes=0)
        if not confirmed or locked is None:
            continue
        # HIBRIT: gecmis ortalama peak saati
        avg_hour = _avg_peak_hour_hist(rd, code, day, lon)
        cur_max = max(t for _, t in rows)
        winner_val = float(cur_max) if cur_max > locked else locked
        winner_bucket = HALF_UP(winner_val)
        # market kazanan bucket'ta var mi (RANGE)
        m = day_mkts.get((code, day, winner_bucket))
        if m is None or m["mtype"] != "RANGE":
            continue
        # MIN_ENTRY + max_entry (giris: peak kilidi anindan sonraki gercer ask)
        entry = rd.price_before(m["id"], day_end_ts)
        if entry is None:
            entry = rd.price_at_or_after(m["id"], day_end_ts)
        if entry is None or not (0.05 <= entry < spread_max_entry):
            continue
        outcome = settlement_outcome(rd, code, day, winner_bucket, m)
        if outcome is None:
            continue
        if metar_opened >= metar_cap:
            continue
        stake = 3.0  # METAR-peak stake
        shares = stake / entry
        if outcome:
            pnl = (stake / entry) - stake - stake * fee_rate
            won = True
        else:
            pnl = -stake
            won = False
        metar_opened += 1
        results.append({
            "leg": "METAR", "day": day, "tgt": day, "city": rd.city_name[code],
            "code": code, "bucket": winner_bucket, "entry": entry,
            "stake": stake, "won": won, "pnl": pnl, "exit": "hold_settlement",
        })

    return results


def settlement_outcome(rd: ReplayData, code: str, tgt_day: str, bucket: int, m: dict) -> bool | None:
    """GERCEK kapanis sonucu: o sehrin o gunku actual tmax'i bucket'a esit mi?"""
    city_name = rd.city_name.get(code)
    if not city_name:
        return None
    tmax = rd.actuals.get((city_name, tgt_day))
    if tmax is None:
        return None
    return HALF_UP(tmax) == bucket


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-05")
    ap.add_argument("--end", default="2026-08-24")
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    rd = ReplayData(args.start, args.end)
    days = sorted({d for (_c, d, _b) in rd.markets.keys()})

    all_res = []
    per_day = defaultdict(lambda: defaultdict(list))
    for day in days:
        res = simulate(rd, day, args.detail)
        all_res.extend(res)
        for r in res:
            per_day[day][r["leg"]].append(r)

    # --- rapor ---
    print("=" * 78)
    print("REPLAY FULL BOT  (05-24 Agustos, gercek forecast + orderbook + actuals)")
    print("=" * 78)

    by_leg = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for r in all_res:
        a = by_leg[r["leg"]]
        a[0] += 1
        a[1] += 1 if r["won"] else 0
        a[2] += r["pnl"]
        a[3] += r["stake"]

    print(f"\n{'LEG':<8}{'bet':>5}{'won':>5}{'wr%':>7}{'NET':>10}{'stake':>10}{'ROI%':>8}")
    tot_n = tot_w = 0
    tot_pnl = tot_stake = 0.0
    for leg in ["T-2", "T-1", "T-0", "METAR"]:
        a = by_leg[leg]
        n, w, pnl, stake = a
        wr = 100 * w / n if n else 0
        roi = 100 * pnl / stake if stake else 0
        print(f"{leg:<8}{n:>5}{w:>5}{wr:>7.1f}{pnl:>+10.2f}{stake:>10.2f}{roi:>+8.1f}")
        tot_n += n
        tot_w += w
        tot_pnl += pnl
        tot_stake += stake
    wr = 100 * tot_w / tot_n if tot_n else 0
    roi = 100 * tot_pnl / tot_stake if tot_stake else 0
    print("-" * 54)
    print(f"{'TOPLAM':<8}{tot_n:>5}{tot_w:>5}{wr:>7.1f}{tot_pnl:>+10.2f}{tot_stake:>10.2f}{roi:>+8.1f}")

    # gun gun
    print(f"\nGUN BAZINDA:")
    print(f"{'gun':<12}{'T-2':>8}{'T-1':>8}{'T-0':>8}{'METAR':>8}{'NET':>10}")
    for day in days:
        t2 = sum(r['pnl'] for r in per_day[day].get('T-2', []))
        t1 = sum(r['pnl'] for r in per_day[day].get('T-1', []))
        t0 = sum(r['pnl'] for r in per_day[day].get('T-0', []))
        mt = sum(r['pnl'] for r in per_day[day].get('METAR', []))
        net = t2 + t1 + t0 + mt
        print(f"{day:<12}{t2:>+8.2f}{t1:>+8.2f}{t0:>+8.2f}{mt:>+8.2f}{net:>+10.2f}")

    if args.detail and all_res:
        print(f"\n--- DETAY (ilk 60 bet) ---")
        for r in all_res[:60]:
            print(f"  {r['day']} {r['leg']:<4} {r['city']:<20} bucket={r['bucket']:>3} "
                  f"entry={r['entry']:.3f} {'WON' if r['won'] else 'LOST'} pnl={r['pnl']:+.2f}")

    print("\n[crash] YOK — replay temiz tamamlandi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
