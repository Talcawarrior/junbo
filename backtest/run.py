"""Backtest runner: son 12 gun, tum kaynak*sehir, bias duzeltmeli, gercek settlement."""

import sys

sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from backtest.engine import load_data, evaluate_all  # noqa: E402
from collections import defaultdict  # noqa: E402

SOURCES = [
    "icon_global",
    "ecmwf_ifs025",
    "gfs_seamless",
    "ukmo_seamless",
    "meteofrance_seamless",
    "gem_global",
    "cma_grapes_global",
    "jma_seamless",
    "openweathermap",
    "visual_crossing",
    "weatherapi_com",
    "nws_hourly",
    "pangu_weather",
    "fourcastnet_v2",
]

city_icao, fc, bias_map, actual, mid_to_key, ob_series, cutoff = load_data(12)
print("=== VERI OZETI ===")
print(f"cutoff: {cutoff}  sehir: {len(city_icao)}  fc: {len(fc)}  bias: {len(bias_map)}  settlement: {len(actual)}")
print(f"sources: {SOURCES}")

results = evaluate_all(fc, bias_map, actual, city_icao, SOURCES, 12)

# 1) Kaynak bazinda toplu (tum sehirler birlesik)
print("\n=== 1) KAYNAK BAZINDA TOPLU (tum sehirler, tek strike center) ===")
print(f"{'kaynak':<24} {'n':>3} {'W':>3} {'L':>3} {'WR%':>6} {'PnL':>8} {'ROI%':>7}")
agg = {}
for (city, src), r in results.items():
    agg.setdefault(src, {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "stake": 0.0})
    agg[src]["n"] += r["n"]
    agg[src]["w"] += r["w"]
    agg[src]["l"] += r["l"]
    agg[src]["pnl"] += r["pnl"]
    agg[src]["stake"] += r["stake"]
for src in SOURCES:
    a = agg.get(src)
    if not a:
        continue
    wr = a["w"] / a["n"] * 100 if a["n"] else 0
    roi = a["pnl"] / a["stake"] * 100 if a["stake"] else 0
    print(f"{src:<24} {a['n']:>3} {a['w']:>3} {a['l']:>3} {wr:>5.1f}% {a['pnl']:>+7.1f} {roi:>+6.1f}%")

# 2) Sehir*Kaynak matrisi - her sehirde en iyi kaynak
print("\n=== 2) HER SEHIRDE EN IYI KAYNAK (min n>=3, WR ve PnL) ===")
print(f"{'sehir':<18} {'en iyi':<24} {'n':>2} {'WR':>6} {'PnL':>6} | 2. {'n':>2} {'WR':>5} | 3. {'n':>2} {'WR':>5}")
city_best = {}
for city in sorted(city_icao.keys()):
    cand = [(src, results[(city, src)]) for src in SOURCES if (city, src) in results and results[(city, src)]["n"] >= 3]
    if not cand:
        continue
    cand.sort(key=lambda x: (x[1]["wr"], x[1]["pnl"]), reverse=True)
    best_src, best = cand[0]
    city_best[city] = best_src
    line = f"{city:<18} {best_src:<24} {best['n']:>2} {best['wr']:>5.1f}% {best['pnl']:>+5.1f}"
    if len(cand) > 1:
        line += f" | {cand[1][0]:<16} {cand[1][1]['n']:>2} {cand[1][1]['wr']:>5.1f}%"
    if len(cand) > 2:
        line += f" | {cand[2][0]:<16} {cand[2][1]['n']:>2} {cand[2][1]['wr']:>5.1f}%"
    print(line)

# 3) Kaynak kac sehirde en iyi
print("\n=== 3) KAYNAK KAC SEHIRDE EN IYI ===")
cnt = defaultdict(int)
for city, src in city_best.items():
    cnt[src] += 1
for src, c in sorted(cnt.items(), key=lambda x: -x[1]):
    print(f"  {src:<24} {c} sehir")

# 4) En iyi kaynak per sehir ile sinirli backtest (sadece o kombinasyonlarda bet ac)
print("\n=== 4) EN IYI KAYNAK PER SEHIR - SINIRLI BACKTEST (sadece en iyi kaynak*sehir betleri) ===")
total_n = total_w = total_l = 0
total_pnl = total_stake = 0.0
for (city, src), r in results.items():
    if city_best.get(city) == src:
        total_n += r["n"]
        total_w += r["w"]
        total_l += r["l"]
        total_pnl += r["pnl"]
        total_stake += r["stake"]
wr = total_w / total_n * 100 if total_n else 0
roi = total_pnl / total_stake * 100 if total_stake else 0
print(
    f"  n={total_n} W={total_w} L={total_l} WR={wr:.1f}% PnL={total_pnl:+.1f} stake={total_stake:.0f} ROI={roi:+.1f}%"
)
print(f"  gunluk ~ {total_pnl / 12:+.1f}/12gun, ay 30gun ~ {total_pnl / 12 * 30:+.0f}")

# 5) METAR gercek (karsilastirma)
import sqlite3  # noqa: E402

from utils.db_paths import get_db_path  # noqa: E402

bot_db = Path(get_db_path())  # 2026-11-18: D: sabit varsayilani kaldirildi
con = sqlite3.connect(bot_db)
cur = con.cursor()
cur.execute(
    "SELECT status, count(*), sum(realized_pnl) FROM bets WHERE settled_at >= date('now','-12 days') GROUP BY status"
)
print("\n=== 5) GERCEK BOT SON 12 GUN (settled_at bazli) ===")
for r in cur.fetchall():
    print(r)
cur.execute(
    "SELECT CASE WHEN order_id LIKE 'metar_%' THEN 'METAR' "
    "WHEN order_id LIKE 'spread%' THEN 'SPREAD' ELSE 'OTHER' END as t, "
    "status, count(*), sum(realized_pnl) FROM bets "
    "WHERE settled_at >= date('now','-12 days') GROUP BY t,status ORDER BY t,status"
)
for r in cur.fetchall():
    print(r)
con.close()

# 6) IC (bias) dagilimi gostermek
print("\n=== 6) BIAS DAGILIMI (sehir bazinda, ornek) ===")
for city in ["Istanbul", "Karachi", "Seoul", "Singapore", "Kuala Lumpur"]:
    print(f" {city}:")
    for src in SOURCES:
        b = bias_map.get((city, src))
        if b is not None:
            print(f"   {src:<24} {b:+5.2f}", end="; ")
    print()
