"""HIBRIT 2 tam: bias-top 15 TEK ESIK (forecast kalibreli) + 16-40 PIYASA TAKIBI.

Core (15 sehir): forecast kalibreli merkez -> en yakin esik, orderbook ilk ask.
Ext  (16-40):   o sehrin/gunun piyasasinda kapanisa X saat kala EN YUKSEK ask'li
                markete YES bet (piyasa takibi). Cekirdege hic dokunmaz.

Look-ahead yok: ext giris fiyati kapanis (target+12h) anindan onceki son ask.
"""
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
market_td = {}
for code, thr, tdate, raw in cur.execute("SELECT city_code, threshold, target_date, raw_data FROM weather_markets WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"):
    o = parse_resolved_outcome(raw)
    if o is None: continue
    day = str(tdate)[:10]
    markets[(code, day, float(thr))] = o
    market_td[(code, day, float(thr))] = ts(str(tdate)) or 0

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

def ask_until(code, day, thr, until_t):
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series:
            best = None
            for t, a in ob_series[mid]:
                if t <= until_t: best = a
                else: break
            return best
    return None

def first_ask(code, day, thr):
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series:
            return ob_series[mid][0][1]
    return None

# CORE: forecast tek esik
def core_bet(code, day, models):
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
    entry = first_ask(code, day, thr)
    if entry is None or not (0 < entry < 0.95): return None
    return (o, entry)

def run(ext_hours_before):
    core = {'n':0,'w':0,'pnl':0.0}
    ext = {'n':0,'w':0,'pnl':0.0}
    for (code, day), models in fc.items():
        if len(models) < 2: continue
        rank = bias_order.index(code) if code in bias_order else 999
        if rank >= 40: continue
        if rank < 15:
            res = core_bet(code, day, models)
            if res is None: continue
            o, entry = res
            fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
            gain = (STAKE/entry)-cost if o else -cost
            core['n'] += 1; core['pnl'] += gain
            if o: core['w'] += 1
        else:
            # PIYASA TAKIBI: kapanis anindan onceki en yuksek ask
            day_markets = [(thr, o) for (c, d, thr), o in markets.items() if c == code and d == day]
            if not day_markets: continue
            td_ts = market_td.get((code, day, day_markets[0][0]), 0)
            until_t = td_ts + 43200 - ext_hours_before * 3600  # kapanis - X saat
            best = None
            for thr, o in day_markets:
                ask = ask_until(code, day, thr, until_t)
                if ask is None: continue
                if best is None or ask > best[1]:
                    best = (thr, ask, o)
            if best is None: continue
            thr, entry, o = best
            if not (0 < entry < 0.95): continue
            fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
            gain = (STAKE/entry)-cost if o else -cost
            ext['n'] += 1; ext['pnl'] += gain
            if o: ext['w'] += 1
    return core, ext

print("=== HIBRIT 2: 15 TEK ESIK + 16-40 PIYASA TAKIBI (en yuksek YES) ===")
print(f"{'ext giris':<14} {'core bet':>6} {'core win':>6} {'core PnL':>9} | {'ext bet':>6} {'ext win':>6} {'ext wr':>7} {'ext PnL':>9} | {'TOPLAM':>9}")
for hb in [24, 12, 6, 0]:
    core, ext = run(hb)
    tot = core['pnl'] + ext['pnl']
    label = f"{hb}h once" if hb else "kapanis aninda"
    print(f"{label:<14} {core['n']:>6} {core['w']:>6} {core['pnl']:>9.2f} | "
          f"{ext['n']:>6} {ext['w']:>6} {ext['w']/max(ext['n'],1)*100:>6.1f}% {ext['pnl']:>9.2f} | {tot:>9.2f}")
