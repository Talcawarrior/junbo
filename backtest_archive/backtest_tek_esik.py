"""TEK ESIK + BIAS-TOP N + $2 STAKE backtest — orderbook fill, gercek bias.

Kullanici karari (2026-08-16): 'her sehre meteo ne diyorsa TEK bet, tam merkez,
fiyat ne olursa olsun 0.01-0.95 arasi ilk 40 markete ac'. Bu backtest o configi
simule eder:
- Tek esik (spread=0): forecast merkezi (kalibreli ensemble ort) -> en yakin esik
- Sehir secimi: |bias| en DUSUK (en az sapan) ilk N sehir
- Stake: $2 (canli config spread_stake_usd=2.0)
- Gunluk bet limiti: ilk 40 (spread_max_bets_per_day=40) — gun basina en fazla 40 bet
- Fill: orderbook ilk ask (market acildigindaki gercek fiyat)
- Entry araligi: 0.01 <= entry < 0.95 (canli config max_entry=0.95)
- Outcome: gercek Polymarket cozumu (parse_resolved_outcome)
- Fee+gas dahil

Karsilastirma: bias-top 15 vs 40 vs 49, fair-value filtresi acik/kapali.
"""
import sqlite3, sys, math
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')
sys.stdout.reconfigure(encoding='utf-8')
from utils.market_outcome import parse_resolved_outcome
from utils.probability import normal_cdf, estimate_probability_empirical

BOT_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\bot.db'
OB_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db'
STAKE = 2.0
FEE = 0.05
GAS = 0.10
MAX_ENTRY = 0.95
MAX_BETS_PER_DAY = 40

def ts(s):
    s = str(s).replace('T',' ').replace('+00:00','').strip()
    try: return datetime.fromisoformat(s).timestamp()
    except: return None

db = sqlite3.connect(BOT_DB)
cur = db.cursor()

# city_code -> city
code_name = {}
for c, code in cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"):
    if code and c:
        code_name.setdefault(code, c)

# bias-top N: |bias| en dusuk (en az sapan) sehirler
bias = {}
for code, b in cur.execute("SELECT city_code, AVG(ABS(bias)) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code").fetchall():
    bias[code] = float(b)

# market outcome: (city_code, day, thr) -> YES/NO
markets = {}
for mid, code, thr, tdate, raw in cur.execute(
    "SELECT id, city_code, threshold, target_date, raw_data FROM weather_markets "
    "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
):
    if 'max' not in (''):  # sadece max
        pass
    o = parse_resolved_outcome(raw)
    if o is None:
        continue
    markets[(code, str(tdate)[:10], float(thr))] = o

# kalibrasyon bias (dogru istasyon) - ortalama per (city,model)
cal = defaultdict(dict)
for code, model, b in cur.execute(
    "SELECT city_code, model, AVG(bias) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code, model"
).fetchall():
    cal[code][model] = float(b)

# forecast: (city_code, target_date) -> {model: val} (son cekim)
fc = {}
for code, tdate, metric, src, pv in cur.execute(
    "SELECT city, target_date, metric, source, predicted_value FROM weather_forecasts WHERE predicted_value IS NOT NULL AND metric='temperature_max'"
):
    fc.setdefault((code, str(tdate)[:10]), {})
    fc[(code, str(tdate)[:10])].setdefault(src, float(pv))

# orderbook ask series: market_id -> [(t, ask)]
ob = sqlite3.connect(OB_DB)
oc = ob.cursor()
ob_series = defaultdict(list)
for mid, ask, st in oc.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"):
    t = ts(st)
    if t is None: continue
    try:
        a = float(ask)
        if 0 < a <= 1:
            ob_series[str(mid)].append((t, a))
    except: pass
ob.close()
for k in ob_series:
    ob_series[k].sort(key=lambda x: x[0])

# market_id -> (code, day, thr)
mid_key = {}
for r in cur.execute("SELECT id, city_code, target_date, threshold FROM weather_markets WHERE threshold IS NOT NULL"):
    mid_key[str(r[0])] = (r[1], str(r[2])[:10], float(r[3]))
db.close()

def run(bias_top, fair_filter):
    pnl = 0.0
    total = won = 0
    by_city_won = defaultdict(int)
    by_city_n = defaultdict(int)
    day_bets = defaultdict(int)
    # en az sapan bias_top sehir
    top_codes = {c for c, _ in sorted(bias.items(), key=lambda kv: kv[1])[:bias_top]}
    for (code, day), models in fc.items():
        if code not in top_codes or len(models) < 2:
            continue
        # gunluk limit: ilk 40 bet (canli config spread_max_bets_per_day=40)
        if day_bets[day] >= MAX_BETS_PER_DAY:
            continue
        # kalibreli merkez
        kvals = [p - cal.get(code, {}).get(m, 0.0) for m, p in models.items()]
        mean = sum(kvals)/len(kvals)
        center = round(mean)
        thr = float(center)
        o = markets.get((code, day, thr))
        if o is None:
            # en yakin acik esigi bul (tek esik)
            found = None
            for off in [1, -1, 2, -2, 3, -3]:
                cand = markets.get((code, day, thr + off))
                if cand is not None:
                    found = (thr + off, cand)
                    break
            if found is None:
                continue
            thr, o = found
        # orderbook fill (ilk ask)
        found_mid = None
        for mid, key in mid_key.items():
            if key == (code, day, thr) and mid in ob_series:
                found_mid = mid
                break
        if found_mid is None:
            continue
        entry = ob_series[found_mid][0][1]
        # canli config: 0.01 <= entry < 0.95 (2026-08-16 kullanici karari)
        if not (0.01 <= entry < MAX_ENTRY):
            continue
        # fair-value filtresi (opsiyonel)
        if fair_filter:
            std = (max(kvals)-min(kvals))/2.0 if len(kvals) > 1 else 1.0
            fair = estimate_probability_empirical(mean, thr, 'HIGH', 'temperature_max', lag_hours=48)
            if entry >= fair:
                continue
        fee = STAKE * FEE * (1.0 - entry)
        cost = STAKE + fee + GAS
        gain = (STAKE/entry) - cost if o else -cost
        pnl += gain
        total += 1
        day_bets[day] += 1
        by_city_n[code] += 1
        if o:
            won += 1
            by_city_won[code] += 1
    return pnl, total, won, by_city_won, by_city_n

print("=== TEK ESIK + BIAS-TOP + $2 STAKE + ORDERBOOK FILL + GUNLUK 40 ===")
print(f"{'bias_top':>9} {'fair':>6} {'bet':>5} {'won':>4} {'winrate':>8} {'PnL':>10}")
for top in [15, 40, 49]:
    for ff in [False, True]:
        pnl, n, w, cw, cn = run(top, ff)
        print(f"{top:>9} {str(ff):>6} {n:>5} {w:>4} {w/max(n,1)*100:>7.1f}% {pnl:>10.2f}")
    print()
