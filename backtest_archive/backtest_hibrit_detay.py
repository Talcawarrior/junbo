"""Hibrit D detayi: 15 cekirdek vs genisletilmis bolge (16-40) ayri PnL."""
import sqlite3, sys, math
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')
sys.stdout.reconfigure(encoding='utf-8')
from utils.market_outcome import parse_resolved_outcome
from utils.probability import estimate_probability_empirical

BOT_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\bot.db'
OB_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db'
STAKE = 1.0; FEE = 0.05; GAS = 0.10

def ts(s):
    s = str(s).replace('T',' ').replace('+00:00','').strip()
    try: return datetime.fromisoformat(s).timestamp()
    except: return None

db = sqlite3.connect(BOT_DB); cur = db.cursor()
code_name = {}
for c, code in cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"):
    if code and c: code_name.setdefault(code, c)
bias = {}
for code, b in cur.execute("SELECT city_code, AVG(ABS(bias)) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code").fetchall():
    bias[code] = float(b)
bias_order = [c for c, _ in sorted(bias.items(), key=lambda kv: kv[1])]
markets = {}
for code, thr, tdate, raw in cur.execute("SELECT city_code, threshold, target_date, raw_data FROM weather_markets WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"):
    o = parse_resolved_outcome(raw)
    if o is None: continue
    markets[(code, str(tdate)[:10], float(thr))] = o
cal = defaultdict(dict)
for code, model, b in cur.execute("SELECT city_code, model, AVG(bias) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code, model").fetchall():
    cal[code][model] = float(b)
fc = {}
for code, tdate, src, pv in cur.execute("SELECT city, target_date, source, predicted_value FROM weather_forecasts WHERE predicted_value IS NOT NULL AND metric='temperature_max'"):
    fc.setdefault((code, str(tdate)[:10]), {})
    fc[(code, str(tdate)[:10])].setdefault(src, float(pv))
ob = sqlite3.connect(OB_DB); oc = ob.cursor()
ob_series = defaultdict(list)
for mid, ask, st in oc.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"):
    t = ts(st)
    if t is None: continue
    try:
        a = float(ask)
        if 0 < a <= 1: ob_series[str(mid)].append((t, a))
    except: pass
ob.close()
for k in ob_series: ob_series[k].sort(key=lambda x: x[0])
mid_key = {}
for r in cur.execute("SELECT id, city_code, target_date, threshold FROM weather_markets WHERE threshold IS NOT NULL"):
    mid_key[str(r[0])] = (r[1], str(r[2])[:10], float(r[3]))
db.close()

def bet(code, day, models, rf, rl):
    kvals = [p - cal.get(code, {}).get(m, 0.0) for m, p in models.items()]
    mean = sum(kvals)/len(kvals); thr = float(round(mean))
    o = markets.get((code, day, thr))
    if o is None:
        found = None
        for off in [1,-1,2,-2,3,-3]:
            cand = markets.get((code, day, thr+off))
            if cand is not None: found = (thr+off, cand); break
        if found is None: return None
        thr, o = found
    found_mid = None
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series: found_mid = mid; break
    if found_mid is None: return None
    entry = ob_series[found_mid][0][1]
    if not (0 < entry < 0.95): return None
    if rl and entry >= 0.30: return None
    if rf:
        std = (max(kvals)-min(kvals))/2.0 if len(kvals) > 1 else 1.0
        fair = estimate_probability_empirical(mean, thr, 'HIGH', 'temperature_max', lag_hours=48)
        if entry >= fair: return None
    return (o, entry)

core = {'n':0,'w':0,'pnl':0.0}
ext = {'n':0,'w':0,'pnl':0.0}
ext_detail = []
for (code, day), models in fc.items():
    if len(models) < 2: continue
    rank = bias_order.index(code) if code in bias_order else 999
    if rank >= 40: continue
    if rank < 15:
        res = bet(code, day, models, False, False)
        tgt = core
    elif rank < 25:
        res = bet(code, day, models, True, False)
        tgt = ext
    else:
        res = bet(code, day, models, True, True)
        tgt = ext
    if res is None: continue
    o, entry = res
    fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
    gain = (STAKE/entry)-cost if o else -cost
    tgt['n'] += 1; tgt['pnl'] += gain
    if o: tgt['w'] += 1
    ext_detail.append((code_name.get(code,code), o, gain, entry, rank))

print("=== HIBRIT D: cekirdek vs genisletilmis bolge ===")
for name, t in [('15 CEKIRDEK', core), ('16-40 GENISLETILMIS', ext)]:
    print(f"  {name:<22} bet={t['n']:>3} won={t['w']:>3} winrate={t['w']/max(t['n'],1)*100:>5.1f}% PnL=${t['pnl']:>7.2f}")

print()
print("=== 16-40 bolgesi betleri (sehir, kazandi, pnl, entry, rank) ===")
for c in sorted(ext_detail, key=lambda x: -x[2]):
    mark = 'KAZANDI' if c[1] else 'kaybetti'
    print(f"  {c[0]:<16} {mark:8} {c[2]:>7.2f} entry={c[3]:.3f} rank={c[4]}")
