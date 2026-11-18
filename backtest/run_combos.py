"""Kombinasyon grid + METAR karsilastirma, orderbook entry ile."""

import sys

sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from backtest.engine import load_data, evaluate_all  # noqa: E402
import sqlite3  # noqa: E402

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
results = evaluate_all(fc, bias_map, actual, city_icao, SOURCES, 12)

# en iyi per sehir
city_best = {}
for city in city_icao:
    cand = [(src, results[(city, src)]) for src in SOURCES if (city, src) in results and results[(city, src)]["n"] >= 3]
    if not cand:
        continue
    cand.sort(key=lambda x: (x[1]["wr"], x[1]["pnl"]), reverse=True)
    city_best[city] = cand[0][0]


def combo_pnl(src_list, city_filter=None):
    n = w = ls = 0
    pnl = stake = 0
    for (city, src), r in results.items():
        if src not in src_list:
            continue
        if city_filter and city not in city_filter:
            continue
        n += r["n"]
        w += r["w"]
        ls += r["l"]
        pnl += r["pnl"]
        stake += r["stake"]
    wr = w / n * 100 if n else 0
    roi = pnl / stake * 100 if stake else 0
    return n, w, ls, wr, pnl, roi


print("=== KOMBOS (12 gun, bias duzeltmeli, single strike) ===")
combos = [
    (["openweathermap"], "OWM tek"),
    (["visual_crossing"], "VC tek"),
    (["openweathermap", "visual_crossing"], "OWM+VC"),
    (["openweathermap", "visual_crossing", "ecmwf_ifs025"], "OWM+VC+ECMWF"),
    (["icon_global", "ecmwf_ifs025", "visual_crossing", "openweathermap", "pangu_weather"], "senin 5'li"),
    (["pangu_weather", "fourcastnet_v2"], "AI ikili"),
    (["openweathermap", "visual_crossing", "pangu_weather", "fourcastnet_v2"], "AI+OWM+VC"),
]
for srcs, label in combos:
    n, w, ls, wr, pnl, roi = combo_pnl(srcs)
    print(f"{label:<22} {str(srcs):<55} n={n:>3} WR={wr:5.1f}% PnL={pnl:+6.1f} ROI={roi:+5.1f}%")

# en iyi per sehir sinirli
best_cities = set(city_best.keys())
print("\n=== EN IYI PER SEHIR SINIRLI KOMBOS ===")
for srcs, label in combos:
    # sadece o kaynaklar icinde en iyi olan sehirler filtre
    filt = {c for c, s in city_best.items() if s in srcs}
    n, w, ls, wr, pnl, roi = combo_pnl(srcs, filt)
    print(f"{label:<22} filt={len(filt)}sehir n={n:>3} WR={wr:5.1f}% PnL={pnl:+6.1f} ROI={roi:+5.1f}%  {sorted(filt)}")
    print(f"{label:<22} filt={len(filt)}sehir n={n:>3} WR={wr:5.1f}% PnL={pnl:+6.1f} ROI={roi:+5.1f}%  {sorted(filt)}")

# tum en iyi per sehir tek bet (her sehir sadece en iyi kaynak)
print("\n=== TUM EN IYI PER SEHIR (her sehir tek kaynak, o kaynakla tek bet) ===")
n = w = ls = 0
pnl = stake = 0
for city, src in city_best.items():
    r = results[(city, src)]
    n += r["n"]
    w += r["w"]
    ls += r["l"]
    pnl += r["pnl"]
    stake += r["stake"]
wr = w / n * 100 if n else 0
roi = pnl / stake * 100 if stake else 0
monthly = pnl / 12 * 30 if pnl else 0
print(f"n={n} W={w} L={ls} WR={wr:.1f}% PnL={pnl:+.1f} ROI={roi:+.1f}% 12gun -> ay {monthly:+.0f}")

# METAR gercekle kiyas
from utils.db_paths import get_db_path  # noqa: E402

con = sqlite3.connect(Path(get_db_path()))  # 2026-11-18: D: sabit varsayilani kaldirildi
cur = con.cursor()
cur.execute(
    "SELECT count(*), sum(realized_pnl) FROM bets "
    "WHERE order_id LIKE 'metar_%' AND settled_at >= date('now','-12 days') "
    "AND status IN ('won','lost')"
)
print("\n=== METAR GERCEK (won/lost only, 12gun) ===")
print(cur.fetchone())
cur.execute(
    "SELECT status, count(*), sum(realized_pnl) FROM bets "
    "WHERE order_id LIKE 'metar_%' AND settled_at >= date('now','-12 days') "
    "GROUP BY status"
)
for r in cur.fetchall():
    print(r)
con.close()
