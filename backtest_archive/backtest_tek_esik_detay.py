"""Detay: bias-top 40, tek esik, $1, orderbook fill — gun gun + sehir dagilimi."""
import sqlite3, sys, math
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')
sys.stdout.reconfigure(encoding='utf-8')
from utils.market_outcome import parse_resolved_outcome

BOT_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\bot.db'
OB_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db'
STAKE = 1.0
FEE = 0.05
GAS = 0.10

def ts(s):
    s = str(s).replace('T',' ').replace('+00:00','').strip()
    try: return datetime.fromisoformat(s).timestamp()
    except: return None

db = sqlite3.connect(BOT_DB)
cur = db.cursor()
code_name = {}
for c, code in cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"):
    if code and c:
        code_name.setdefault(code, c)
bias = {}
for code, b in cur.execute("SELECT city_code, AVG(ABS(bias)) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code").fetchall():
    bias[code] = float(b)
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
mid_key = {}
for r in cur.execute("SELECT id, city_code, target_date, threshold FROM weather_markets WHERE threshold IS NOT NULL"):
    mid_key[str(r[0])] = (r[1], str(r[2])[:10], float(r[3]))
db.close()

top40 = {c for c, _ in sorted(bias.items(), key=lambda kv: kv[1])[:40]}
per_day = defaultdict(lambda: {'n':0, 'w':0, 'pnl':0.0})
per_city = defaultdict(lambda: {'n':0, 'w':0, 'pnl':0.0})
rows_detail = []
for (code, day), models in fc.items():
    if code not in top40 or len(models) < 2: continue
    kvals = [p - cal.get(code, {}).get(m, 0.0) for m, p in models.items()]
    mean = sum(kvals)/len(kvals)
    thr = float(round(mean))
    o = markets.get((code, day, thr))
    if o is None:
        found = None
        for off in [1,-1,2,-2,3,-3]:
            cand = markets.get((code, day, thr+off))
            if cand is not None:
                found = (thr+off, cand); break
        if found is None: continue
        thr, o = found
    found_mid = None
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series:
            found_mid = mid; break
    if found_mid is None: continue
    entry = ob_series[found_mid][0][1]
    if not (0 < entry < 0.95): continue
    fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
    gain = (STAKE/entry)-cost if o else -cost
    per_day[day]['n'] += 1; per_city[code]['n'] += 1
    per_day[day]['pnl'] += gain; per_city[code]['pnl'] += gain
    if o:
        per_day[day]['w'] += 1; per_city[code]['w'] += 1
    rows_detail.append((day, code_name.get(code,code), thr, entry, o, gain))

print("=== GUN GUN (bias-top 40, tek esik, $1) ===")
for day in sorted(per_day):
    d = per_day[day]
    print(f"  {day}: bet={d['n']:>3} won={d['w']:>3} winrate={d['w']/max(d['n'],1)*100:>5.1f}% PnL=${d['pnl']:>8.2f}")

print()
print("=== SEHIR DAGILIMI (en karli 15) ===")
for code in sorted(per_city, key=lambda c: -per_city[c]['pnl'])[:15]:
    c = per_city[code]
    print(f"  {code_name.get(code,code):<16} bet={c['n']:>3} won={c['w']:>3} winrate={c['w']/max(c['n'],1)*100:>5.1f}% PnL=${c['pnl']:>7.2f}")

print()
print("=== TOPLAM ===")
tn = sum(d['n'] for d in per_day.values()); tw = sum(d['w'] for d in per_day.values()); tp = sum(d['pnl'] for d in per_day.values())
print(f"  bet={tn} won={tw} winrate={tw/max(tn,1)*100:.1f}% PnL=${tp:.2f}")
