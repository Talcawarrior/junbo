"""PEAK TESPIT HIZI ANALIZI (2026-08-19).

Soru (kullanici): "millet nasil bizden erken giriyor, biz neden bet
acamiyoruz?" — peak'leri YAKALIYORUZ ama GEC yakaliyoruz. Bu script 3 tespit
senaryosunun giris fiyatlarini olcer (look-ahead YOK — her senaryoda yalnizca
o anda elde olan veri kullanilir):

  V0 = MEVCUT sistem: 1-dusus kilidi (detect_peak) + 30 dk poll
       (poll gecikmesi dahil: kilitten sonraki ilk poll zamani)
  V1 = HIZLI METAR: dusus beklemeden, peak gozlemi BIZE ulastigi anda
       (metar_observations.recorded_at) kumulatif max'a bet
  V2 = WU-PROXY: dakikalik canli istasyon verisi gibi — gozlem SAATINDE
       (obs_time + 10 dk yayin gecikmesi) kumulatif max'a bet

Her senaryoda: kac bet acilabilirdi ([0.05, 0.95) fiyat), ortalama giris,
kazanma orani (final max ayni bucket'ta mi), $3 stake PnL.

Kullanim: python -m scripts.analyze_peak_speed [--days N]
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from database.db import get_session
from database.models import MetarObservation, WeatherMarket
from scrapers.metar import city_utc_offset, detect_peak
import sys as _sys
from pathlib import Path as _Path
import os

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir  # noqa: E402


def _price_at(market_id: int, t: float, prices: dict[int, list[tuple[float, float]]]) -> float | None:
    """t aninda (veya sonrasinda ilk) bilinen fiyat. 15 dk icinde yoksa None."""
    rows = prices.get(market_id)
    if not rows:
        return None
    # ilk fiyat >= t
    for ts, p in rows:
        if ts >= t:
            return p
    return None


def main() -> int:
    days_n = 2
    for a in sys.argv[1:]:
        if a.startswith("--days"):
            days_n = int(a.split("=", 1)[1] or "2")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_list = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_n)]
    day_list.reverse()

    with get_session() as session:
        # market DB id -> (threshold, city_code, target_date str)
        # hem price_history.market_id hem orderbook_snapshots.market_id
        # weather_markets.id kullanir (backfill_price_history mid'i yazar).
        mk: dict[int, tuple[float, str, str]] = {}
        code2name: dict[str, str] = {}
        for r in session.query(WeatherMarket).all():
            if r.target_date is None:
                continue
            if r.city_code:
                code2name[str(r.city_code)] = str(r.city)
            mk[int(r.id)] = (float(r.threshold or 0), str(r.city_code or ""), str(r.target_date.date().isoformat()))
        # session kapanmadan tuple'lara cevir (DetachedInstanceError onlemi)
        obs = [
            (str(o.city_code), str(o.day or ""), o.obs_time, float(o.temp_c or 0), o.recorded_at)
            for o in session.query(MetarObservation).all()
        ]

    # fiyat gecmisi: iki kaynak birlestirilir
    #  1) backtest_prices.price_history: 5dk CLOB YES fiyati (eski marketler)
    #  2) orderbook_snapshots.best_ask: 30dk CLOB ask (yeni marketler dahil)
    import sqlite3

    prices: dict[int, list[tuple[float, float]]] = {}
    conn = sqlite3.connect(os.path.join(get_data_dir(), "backtest_prices.db"))
    conn.execute("PRAGMA busy_timeout=10000")
    for m_id, ts, p in conn.execute("SELECT market_id, ts, price FROM price_history"):
        try:
            prices.setdefault(int(m_id), []).append((float(ts), float(p)))
        except ValueError:
            continue
    conn.close()
    conn = sqlite3.connect(os.path.join(get_data_dir(), "orderbook.db"))
    conn.execute("PRAGMA busy_timeout=10000")
    for m_id, t, ask in conn.execute(
        "SELECT market_id, snapshot_time, best_ask FROM orderbook_snapshots WHERE best_ask > 0"
    ):
        try:
            ts = float(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
            prices.setdefault(int(m_id), []).append((ts, float(ask)))
        except (ValueError, TypeError):
            continue
    conn.close()
    for k in prices:
        prices[k].sort()

    stats = {
        "V0": {"n": 0, "entries": 0, "wins": 0, "avg_entry": 0.0, "pnl": 0.0},
        "V1": {"n": 0, "entries": 0, "wins": 0, "avg_entry": 0.0, "pnl": 0.0},
        "V2": {"n": 0, "entries": 0, "wins": 0, "avg_entry": 0.0, "pnl": 0.0},
    }
    stake = 3.0
    rows_by_day: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    for code, d, obs_time, temp, rec in obs:
        key = (code, d)
        # naive datetime UTC'dir (archive .replace(tzinfo=None) ile yazar);
        # .timestamp() yerel yorumlar -> 3 saat kayma. UTC ile cevir.
        e = obs_time.replace(tzinfo=timezone.utc).timestamp()
        r = rec.replace(tzinfo=timezone.utc).timestamp()
        rows_by_day.setdefault(key, []).append((e, temp, r))

    detail_rows: list[tuple] = []

    def _try_entry(vname: str, m_id: int, t: float, bucket: int, fbucket: int, city: str = "") -> None:
        s = stats[vname]
        s["n"] += 1
        p = _price_at(m_id, t, prices)
        if p is None:
            return
        if not (0.05 <= p < 0.95):
            return
        win = bucket == fbucket
        s["entries"] += 1
        s["avg_entry"] += p
        s["wins"] += 1 if win else 0
        s["pnl"] += stake * (1.0 - p) if win else -stake
        if vname == "V0":
            detail_rows.append((city, t, p, bucket, fbucket, win))

    for day in day_list:
        for (code, d), rows in rows_by_day.items():
            if d != day:
                continue
            rows.sort()
            final_max = max(t for _, t, _ in rows)
            fbucket = int(final_max + 0.5)
            off = city_utc_offset(code, day, None)

            # V0: 1-dusus kilidi (mevcut sistem) + 10dk poll (2026-08-19'dan beri)
            day_rows = [(int(e), t) for e, t, _ in rows]
            peak, confirmed = detect_peak(day_rows, utc_offset_hours=off)
            if confirmed and peak is not None:
                lock_obs_time = None
                cmax = day_rows[0][1]
                for i in range(1, len(day_rows)):
                    ep, cur = day_rows[i]
                    if datetime.fromtimestamp(ep + off * 3600, tz=timezone.utc).hour < 13:
                        cmax = max(cmax, cur)
                        continue
                    if cur < cmax:
                        lock_obs_time = float(ep)
                        break
                    cmax = max(cmax, cur)
                if lock_obs_time is not None:
                    b = int(peak + 0.5)
                    cand = [m for m, (thr, cc, td) in mk.items() if cc == code and td == day and int(thr + 0.5) == b]
                    if cand:
                        _try_entry("V0", cand[0], lock_obs_time + 10 * 60, b, fbucket, city=code2name.get(code, code))

            # V1/V2: dusus beklenmeden, her YENI running-max gozleminde bet ac
            # (o an bilinen max'in bucket'ina; sonra yukselirse o bet KAYBEDER
            # — false-lock maliyeti olculur). Yerel 13:00 kapisi aynen gecerli.
            run_max = -999.0
            last_bucket: int | None = None
            for e, t, r in rows:
                local_h = datetime.fromtimestamp(e + off * 3600, tz=timezone.utc).hour
                if local_h < 13:
                    run_max = max(run_max, t)
                    continue
                if t <= run_max:
                    continue  # yeni max yok
                run_max = t
                b = int(run_max + 0.5)
                if b == last_bucket:
                    continue
                last_bucket = b
                cand = [m for m, (thr, cc, td) in mk.items() if cc == code and td == day and int(thr + 0.5) == b]
                if not cand:
                    continue
                _try_entry("V1", cand[0], r, b, fbucket)  # bize ulastigi an
                _try_entry("V2", cand[0], e + 10 * 60, b, fbucket)  # WU: gozlem+10dk

    print(f"günler: {day_list}")
    print(f"{'senaryo':4} {'sehir-gun':>9} {'giris':>6} {'ort.fiyat':>8} {'win%':>6} {'PnL($3)':>8}")
    for k, s in stats.items():
        if s["n"] == 0:
            print(f"{k:4} {s['n']:9d} {'-':>6} {'-':>8} {'-':>6} {'-':>8}")
            continue
        avg = s["avg_entry"] / s["entries"] if s["entries"] else 0.0
        wr = s["wins"] / s["entries"] * 100 if s["entries"] else 0.0
        print(f"{k:4} {s['n']:9d} {s['entries']:6d} {avg:8.3f} {wr:5.1f}% {s['pnl']:8.2f}")
    print()
    print("V0 fiyat bantlari (piyasa fiyati kilit sinyalini nasil dogruluyor?):")
    bands = [(0.05, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.95)]
    print(f"{'bant':>12s} {'giris':>5s} {'win%':>6s} {'PnL($3)':>8s}")
    for lo, hi in bands:
        rows = [r for r in detail_rows if lo <= r[2] < hi]
        if not rows:
            continue
        wins = sum(1 for r in rows if r[5])
        pnl = sum(stake * (1.0 - r[2]) if r[5] else -stake for r in rows)
        print(f"{lo:.2f}-{hi:.2f}  {len(rows):5d} {wins / len(rows) * 100:5.1f}% {pnl:8.2f}")
    print()
    print("V0 giris detayi (ucuz girislerde kazanan/kaybeden ozellikleri):")
    print(f"{'sehir':14s} {'kilitle(UTC)':13s} {'fiyat':>6s} {'bucket':>6s} {'sonuc':>6s}")
    for city, t, p, b, fb, win in sorted(detail_rows, key=lambda r: r[2]):
        res = "WIN" if win else f"LOSE(final={fb}C)"
        print(
            f"{city:14s} {datetime.fromtimestamp(t, tz=timezone.utc).strftime('%m-%d %H:%M'):13s} "
            f"{p:6.3f} {b:6d}C {res:>6s}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
