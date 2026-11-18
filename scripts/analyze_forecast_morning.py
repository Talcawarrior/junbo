"""SABAH TAHMIN ANALIZI (2026-08-19, kullanici sorusu).

"Open-Meteo'dan sabah verilen tahminleri cek, sehir bazinda tutma oranlarini
incele; sabah spread acsak mi — 28 derse +-1'e mi acsak?"

1) Sabah tahmini = hedef gunun 00:00-08:00 UTC arasi fetch'lerindeki model
   tahminlerinin MEDYANI (open-meteo 8 model).
2) Gerceklesen = actuals.db temperature_2m_max (ayni saglayici — caveat:
   tahmin ve gercek ayni kaynaktan; bagimsiz kontrol icin 17-19 Agu METAR
   arsivi de kiyaslanir).
3) Tutma oranlari: bucket isabeti (round(tahmin)==round(gercek)), +-1 bant,
   MAE, sehir bazinda bias.
4) Sabah bet simulasyonu: sabah fiyatindan (gunun ilk orderbook snapshot'i)
   TEK bucket'a ve +-1 bant'a $2 YES bet — PnL.

Kullanim: python -m scripts.analyze_forecast_morning
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from database.db import get_session
from database.models import WeatherForecast, WeatherMarket
import sys as _sys
from pathlib import Path as _Path
import os

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402


def _bucket(x: float) -> int:
    return int(x + 0.5) if x >= 0 else int(x - 0.5)


def main() -> int:
    # 1) gerceklesenler
    act: dict[tuple[str, str], float] = {}
    conn = sqlite3.connect(os.path.join(get_data_dir(), "actuals.db"))
    for city, date, tmax in conn.execute(
        "SELECT city, date, temperature_2m_max FROM actual_temperatures WHERE temperature_2m_max IS NOT NULL"
    ):
        act[(str(city), str(date))] = float(tmax)
    conn.close()

    with get_session() as session:
        # ICAO -> sehir adi
        code2name: dict[str, str] = {}
        mk: list[tuple[str, str, float, int]] = []  # (city_name, date, threshold, db_id)
        for r in session.query(WeatherMarket).all():
            if r.target_date is None or r.city_code is None:
                continue
            code2name[str(r.city_code)] = str(r.city)
            mk.append(
                (
                    str(r.city),
                    str(r.target_date.date().isoformat()),
                    float(r.threshold or 0),
                    int(r.id),
                )
            )
        # 2) tahminler: (sehir, tarih) -> sabah mediani
        frows: list[tuple[str, str, float]] = []
        for r in session.query(WeatherForecast).filter(WeatherForecast.metric == "temperature_max").all():
            if r.target_date is None or r.fetched_at is None or r.predicted_value is None:
                continue
            h = r.fetched_at.hour if hasattr(r.fetched_at, "hour") else int(str(r.fetched_at)[11:13])
            if h > 8:
                continue  # sabah penceresi: 00-08 UTC
            frows.append((str(r.city), str(r.target_date.date().isoformat()), float(r.predicted_value)))

    # median sabah tahmini
    from collections import defaultdict

    fm: dict[tuple[str, str], list[float]] = defaultdict(list)
    for code, d, v in frows:
        fm[(code2name.get(code, code), d)].append(v)
    fc: dict[tuple[str, str], float] = {}
    for k, vs in fm.items():
        vs.sort()
        fc[k] = vs[len(vs) // 2]

    # 3) fiyatlar (orderbook + price_history)
    prices: dict[int, list[tuple[float, float]]] = {}
    # bot canli yazarken kilit cakismasin: read-only baglanti + retry
    import time as _t

    conn = None
    for _i in range(6):
        try:
            conn = sqlite3.connect(f"file:{os.path.join(get_data_dir(), 'orderbook.db')}?mode=ro", uri=True)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()
            break
        except sqlite3.OperationalError:
            if conn:
                conn.close()
            conn = None
            _t.sleep(5)
    if conn is None:
        print("UYARI: orderbook.db kilitli — fiyat gerektiren sim bolumu atlanir (istatistikler tamam)")
    else:
        for m_id, t, ask in conn.execute(
            "SELECT market_id, snapshot_time, best_ask FROM orderbook_snapshots WHERE best_ask > 0"
        ):
            try:
                ts = float(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
                prices.setdefault(int(m_id), []).append((ts, float(ask)))
            except (ValueError, TypeError):
                continue
        conn.close()
    conn = sqlite3.connect(f"file:{os.path.join(get_data_dir(), 'backtest_prices.db')}?mode=ro", uri=True)
    for m_id, ts, p in conn.execute("SELECT market_id, ts, price FROM price_history WHERE price > 0"):
        try:
            prices.setdefault(int(m_id), []).append((float(ts), float(p)))
        except ValueError:
            continue
    conn.close()
    for k in prices:
        prices[k].sort()

    def morning_price(m_id: int, date: str) -> float | None:
        day_start = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()
        rows = prices.get(m_id)
        if not rows:
            return None
        # gunun ilk fiyati (sabah) — 12:00 UTC'den once
        for ts, p in rows:
            if ts >= day_start and ts < day_start + 12 * 3600:
                return p
        return None

    # 4) degerlendirme
    hit = band = n = 0
    err_sum = 0.0
    err_signed = 0.0
    city_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [n, hit]
    tek = band3 = 0
    tek_pnl = band3_pnl = 0.0
    tek_wins = band3_wins = 0
    band3_n = 0
    stake = 2.0
    tek_city_pnl: dict[str, list[float]] = defaultdict(list)  # sehir -> bet pnl listesi
    for key, f in fc.items():
        city, d = key
        a = act.get(key)
        if a is None:
            continue
        n += 1
        bf, ba = _bucket(f), _bucket(a)
        if bf == ba:
            hit += 1
        if abs(bf - ba) <= 1:
            band += 1
        err_sum += abs(f - a)
        err_signed += f - a
        cs = city_stats[city]
        cs[0] += 1
        cs[1] += 1 if bf == ba else 0
        # TEK bucket sabah beti
        m_tek = [m for m in mk if m[0] == city and m[1] == d and _bucket(m[2]) == bf]
        if m_tek:
            p = morning_price(m_tek[0][3], d)
            if p is not None and 0.05 <= p < 0.95:
                tek += 1
                if bf == ba:
                    tek_wins += 1
                    tek_pnl += stake * (1.0 - p)
                    tek_city_pnl[city].append(stake * (1.0 - p))
                else:
                    tek_pnl -= stake
                    tek_city_pnl[city].append(-stake)
        # +-1 bant: 3 bucket'a bet
        band_mkts = [
            (m, b) for m in mk if m[0] == city and m[1] == d for b in (bf - 1, bf, bf + 1) if _bucket(m[2]) == b
        ]
        if len(band_mkts) >= 3:
            ps = [(b, morning_price(m[3], d)) for m, b in band_mkts]
            ps = [(b, p) for b, p in ps if p is not None and 0.05 <= p < 0.95]
            if len(ps) == 3:
                band3_n += 1
                for b, p in ps:
                    band3 += 1
                    if b == ba:
                        band3_wins += 1
                        band3_pnl += stake * (1.0 - p)
                    else:
                        band3_pnl -= stake

    print(f"sehir-gun: {n} (sabah tahmini + gerceklesen)")
    print(f"bucket isabeti (TEK esik): {hit}/{n} = {hit / n * 100:.1f}%")
    print(f"+-1 bant kapsama: {band}/{n} = {band / n * 100:.1f}%")
    print(f"MAE: {err_sum / n:.2f} C | ortalama sapma (tahmin - gercek): {err_signed / n:+.2f} C")
    print()
    # METAR bagimsiz kontrol (17-19 Agu): ayni-provider sisirmesi mi?
    mconn = sqlite3.connect(f"file:{get_db_path()}?mode=ro", uri=True)
    mconn.execute("PRAGMA busy_timeout=30000")
    metar_max: dict[tuple[str, str], float] = {}
    for code, d, t in mconn.execute("SELECT city_code, day, temp_c FROM metar_observations"):
        key = (code2name.get(str(code), str(code)), str(d))
        metar_max[key] = max(metar_max.get(key, -999.0), float(t))
    mconn.close()
    mh = mn = 0
    metar_pairs: dict[tuple[str, str], tuple[float, float]] = {}
    for key, f in fc.items():
        a = metar_max.get(key)
        if a is None or key not in act:
            continue
        mn += 1
        metar_pairs[key] = (f, a)
        if _bucket(f) == _bucket(a):
            mh += 1
    if mn:
        print(f"METAR bagimsiz kontrol ({mn} sehir-gun): bucket isabeti {mh}/{mn} = {mh / mn * 100:.1f}%")
        # sehir bazinda sistematik sapma (havalimani vs sehir grid'i) + duzeltme
        city_bias: dict[str, list[float]] = defaultdict(list)
        for (city, _d), (f, a) in metar_pairs.items():
            city_bias[city].append(f - a)
        print("sehir bazinda sapma (tahmin - METAR) — ilk 12 ve son 8:")
        sb = sorted(city_bias.items(), key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
        for c, vs in sb[:12] + sb[-8:]:
            print(f"  {c:16s} n={len(vs):3d} ort sapma {sum(vs) / len(vs):+.2f} C")
        # sapma duzeltmeli bucket isabeti (in-sample)
        ch = 0
        for (city, _d), (f, a) in metar_pairs.items():
            vs = city_bias[city]
            corr = sum(vs) / len(vs)
            if _bucket(f - corr) == _bucket(a):
                ch += 1
        print(f"sapma duzeltmeli bucket isabeti (in-sample): {ch}/{mn} = {ch / mn * 100:.1f}%")
    print()
    print("SABAH BET SIMULASYONU (gunun ilk fiyati, $2, [0.05, 0.95)):")
    if tek:
        print(f"  TEK esik: {tek} bet, win {tek_wins}/{tek} ({tek_wins / tek * 100:.1f}%), PnL ${tek_pnl:+.2f}")
    else:
        print("  TEK esik: fiyatli giris yok")
    if band3_n:
        print(
            f"  +-1 bant (3 bet): {band3_n} gun, {band3} bet, win {band3_wins}/{band3} "
            f"({band3_wins / band3 * 100:.1f}%), PnL ${band3_pnl:+.2f}"
        )
    else:
        print("  +-1 bant: fiyatli giris yok")
    print()
    print("sehir bazinda TEK-esik isabeti (en iyi 12):")
    best = sorted(city_stats.items(), key=lambda kv: kv[1][1] / kv[1][0] if kv[1][0] else 0, reverse=True)[:12]
    for c, (nn, hh) in best:
        print(f"  {c:16s} {hh}/{nn} ({hh / nn * 100:.1f}%)")
    print("sehir bazinda TEK-esik isabeti (en kotu 10):")
    worst = sorted(city_stats.items(), key=lambda kv: kv[1][1] / kv[1][0] if kv[1][0] else 0)[:10]
    for c, (nn, hh) in worst:
        print(f"  {c:16s} {hh}/{nn} ({hh / nn * 100:.1f}%)")
    print()
    # yuksek isabetli sehirlere sabah TEK beti (in-sample secim — uyari!)
    print("sehir bazinda sabah TEK PnL (sim penceresi, $2):")
    for c, pls in sorted(tek_city_pnl.items(), key=lambda kv: sum(kv[1]), reverse=True):
        if len(pls) >= 3:
            print(f"  {c:16s} {len(pls):3d} bet, PnL ${sum(pls):+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
