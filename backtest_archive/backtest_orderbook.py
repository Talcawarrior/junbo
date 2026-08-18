"""Orderbook tabanli GERCEKCI backtest (look-ahead'siz).

C1 bulgusu: 'ilk snapshot fiyati' gercek fill'den %103 sapiyor. Bu script
orderbook.db'deki GERCEK best_ask serisini kullanir:
  - bet acilis fiyati = marketin en erken goruldugu andaki best_ask
  - (veya istege bagli: ilk N saatteki median ask / VWAP)
  - settlement = GERCEK Polymarket outcome (parse_resolved_outcome)
  - maliyet = stake + fee + gas

Karsilastirma: HAM vs KALIBRELI tahmin (walk-forward bias) ile fair-value
filtresi. Kalibrasyon bot.db'deki 205k satirlik veriden gelir.

Kullanim:
    python scripts/backtest_orderbook.py                    # HAM vs KALIBRE
    python scripts/backtest_orderbook.py --max-entry 0.95 --gap 0.00
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import math
from collections import defaultdict
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils.market_outcome import parse_resolved_outcome  # noqa: E402
from utils.probability import normal_cdf, estimate_probability_empirical  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
OB_DB = os.path.join(_REPO_ROOT, "data", "orderbook.db")
STAKE = 2.0
FEE_RATE = 0.05
GAS = 0.10


def ts(s):
    s = str(s).replace("T", " ").replace("+00:00", "").strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Orderbook tabanli gercekci backtest")
    parser.add_argument("--spread", type=int, default=3)
    parser.add_argument("--max-entry", type=float, default=0.95)
    parser.add_argument("--gap", type=float, default=0.0, help="fair-value gap (0=market<fair)")
    parser.add_argument("--min-date", default="2026-08-05", help="orderbook basladi")
    parser.add_argument("--bias-top", type=int, default=15)
    parser.add_argument(
        "--fill",
        default="first_ask",
        choices=["first_ask", "median_ask", "vwap"],
        help="acilis fiyati varsayimi: first_ask=market ilk goruldugu an, "
        "median_ask=ilk 20 snapshot medyani, vwap=ilk 20 snapshot agirlikli ort",
    )
    parser.add_argument(
        "--fair",
        default="gaussian",
        choices=["gaussian", "empirical"],
        help="fair-value modeli: gaussian (eski) veya empirical CDF (kalin kuyruk)",
    )
    args = parser.parse_args()

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # city_code -> city
    code_name = {}
    for c, code in cur.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # bias-top 15 (en az sapan)
    bs, bc = {}, {}
    for code, bias in cur.execute("SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL"):
        cn = code_name.get(code)
        if not cn:
            continue
        bs[cn] = bs.get(cn, 0) + abs(float(bias))
        bc[cn] = bc.get(cn, 0) + 1
    cb = {c: bs[c] / bc[c] for c in bs if bc[c] > 0}
    keep = {c for c, _ in sorted(cb.items(), key=lambda kv: kv[1])[: args.bias_top]}

    # market -> (city, thr, target_date, metric)
    markets = {}
    for r in cur.execute(
        "SELECT id, city, city_code, threshold, target_date, metric, raw_data, status FROM weather_markets WHERE threshold IS NOT NULL AND target_date IS NOT NULL"
    ):
        mid, city, code, thr, tdate, metric, raw, status = r
        if "max" not in (metric or ""):
            continue
        markets[str(mid)] = {
            "city": city,
            "code": code,
            "thr": float(thr),
            "day": str(tdate)[:10],
            "metric": metric,
            "raw": raw,
            "status": status,
        }

    # GERCEK outcome: market -> YES kazandi mi
    outcome = {}
    for mid, m in markets.items():
        o = parse_resolved_outcome(m["raw"]) if m["raw"] else None
        if o is not None:
            outcome[mid] = o

    # orderbook ask serisi: market -> [(t, best_ask)]
    ob = sqlite3.connect(OB_DB)
    oc = ob.cursor()
    ask_series = defaultdict(list)
    for mid, ask, st in oc.execute(
        "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
    ):
        t = ts(st)
        if t is None:
            continue
        try:
            a = float(ask)
            if 0 < a <= 1:
                ask_series[str(mid)].append((t, a))
        except (TypeError, ValueError):
            pass
    ob.close()
    for k in ask_series:
        ask_series[k].sort(key=lambda x: x[0])

    # tahminler: (code, day) -> {model: predicted_value} (son cekim)
    fc = {}
    for code, tdate, src, pv in cur.execute(
        "SELECT city, target_date, source, predicted_value FROM weather_forecasts WHERE predicted_value IS NOT NULL AND metric LIKE '%max%'"
    ):
        day = str(tdate)[:10]
        if day < args.min_date:
            continue
        fc.setdefault((code, day), {})
        fc[(code, day)].setdefault(src, float(pv))

    # C2/C3 seffaflik: eksik veri kontrolu (sessiz bos sonuc YERINE acik rapor)
    n_ob = len(ask_series)
    n_markets = len(markets)
    n_fc = len(fc)
    n_out = len(outcome)
    n_overlap = sum(1 for mid in markets if mid in ask_series and mid in outcome)
    print(
        f"[veri kontrol] orderbook_market={n_ob}, eslesen_market={n_markets}, "
        f"forecast_sehir_gun={n_fc}, cozumlenmis_market={n_out}, "
        f"orderbook+outcome ortusme={n_overlap}"
    )
    if n_overlap == 0:
        print("HATA: orderbook ile outcome arasinda eslesme YOK — backtest gecersiz.")
        return 1
    if n_overlap < 100:
        print(f"UYARI: cok az eslesme ({n_overlap}) — sonuc guvenilmez olabilir.")

    # kalibrasyon: (code, model) -> bias (tum gecmis, walk-forward yerine statik-onceki)
    cal = defaultdict(dict)
    for code, model, b in cur.execute(
        "SELECT city_code, model, AVG(bias) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code, model"
    ):
        cal[code][model] = float(b)
    db.close()

    # ---------------- simulasyon ----------------
    def run(mode):
        pnl = 0.0
        total = won = 0
        peak = 0.0
        max_dd = 0.0
        for (code, day), models in fc.items():
            city = code_name.get(code)
            if not city or city not in keep or len(models) < 2:
                continue
            vals = list(models.values())
            mean = sum(vals) / len(vals)
            std = (max(vals) - min(vals)) / 2.0
            center = round(mean)
            for thr in range(center - args.spread, center + args.spread + 1):
                # bu esigin marketini bul (orderbook'ta gorulen)
                found = None
                for mid, m in markets.items():
                    if (
                        m["city"] == city
                        and m["thr"] == float(thr)
                        and m["day"] == day
                        and mid in ask_series
                        and mid in outcome
                    ):
                        found = mid
                        break
                if found is None:
                    continue
                o = outcome[found]
                # acilis fiyati: first_ask / median_ask / vwap (ilk 20 snapshot)
                seri = ask_series[found]
                if args.fill == "first_ask":
                    entry = seri[0][1]
                elif args.fill == "median_ask":
                    asks = [a for _, a in seri[:20]]
                    entry = sorted(asks)[len(asks) // 2]
                else:  # vwap: zaman agirlikli ortalama (ilk 20 snapshot)
                    pts = seri[:20]
                    wsum = 0.0
                    vsum = 0.0
                    for i, (t, a) in enumerate(pts):
                        w = 1.0 if i == 0 else (t - pts[i - 1][0])
                        vsum += a * w
                        wsum += w
                    entry = vsum / wsum if wsum > 0 else pts[0][1]
                if not (0 < entry < args.max_entry):
                    continue
                # fair value (kalibrasyonlu veya ham)
                if mode == "ham":
                    fmean = mean
                else:
                    kvals = [p - cal.get(code, {}).get(m, 0.0) for m, p in models.items()]
                    fmean = sum(kvals) / len(kvals)
                tsd = max(math.sqrt(std**2 + 1.0), 1.0)
                if args.fair == "empirical":
                    fair = estimate_probability_empirical(fmean, float(thr), "HIGH", "temperature_max", lag_hours=48)
                else:
                    fair = max(0.01, min(0.99, 1.0 - normal_cdf((thr - fmean) / tsd)))
                if entry >= fair - args.gap:
                    continue
                fee = STAKE * FEE_RATE * (1.0 - entry)
                cost = STAKE + fee + GAS
                gain = (STAKE / entry) - cost if o else -cost
                pnl += gain
                total += 1
                if o:
                    won += 1
                if pnl > peak:
                    peak = pnl
                dd = peak - pnl
                if dd > max_dd:
                    max_dd = dd
        return pnl, total, won, max_dd

    # C2: seffaflik — veri donemi ve kapsam
    db2 = sqlite3.connect(OB_DB)
    ob_range = db2.execute("SELECT MIN(snapshot_time), MAX(snapshot_time) FROM orderbook_snapshots").fetchone()
    ob_markets = db2.execute("SELECT COUNT(DISTINCT market_id) FROM orderbook_snapshots").fetchone()[0]
    db2.close()
    resolved = sum(1 for v in outcome.values() if v is not None)
    unresolved = len(markets) - resolved
    print(
        f"=== ORDERBOOK BACKTEST (fill={args.fill}, spread={args.spread}, "
        f"max_entry={args.max_entry}, gap={args.gap}, bias-top={args.bias_top}) ==="
    )
    print(
        f"  veri donemi: {str(ob_range[0])[:16]} .. {str(ob_range[1])[:16]} | "
        f"orderbook market={ob_markets}, eslesen market={len(markets)}, "
        f"cozumlenmis={resolved}, cozumlenmemis={unresolved}"
    )
    print(f"  fill varsayimi: {args.fill} (first_ask=market ilk goruldugu an, median_ask=ilk 20 snapshot medyani)")
    print(f"  fee={FEE_RATE} gas=${GAS} stake=${STAKE}")
    for mode in ["ham", "kal"]:
        pnl, n, w, mdd = run(mode)
        print(
            f"  {mode.upper():<6} bet={n:>4} won={w:>3} winrate={w / max(n, 1) * 100:>5.1f}% "
            f"PnL=${pnl:>9.2f} max_drawdown=${mdd:>8.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
