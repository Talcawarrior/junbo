"""HIBRIT simulasyon: bias-top 15 cekirdek (her zaman tek esik) +
16-40 arasi sehirler SADECE guclu sinyal (fair-value / entry filtre) gecerse.

Varyantlar:
  A: 15 cekirdek filtresiz + 16-40 (fair-value filtresi)
  B: 15 cekirdek filtresiz + 16-40 (entry < 0.30)
  C: 15 cekirdek filtresiz + 16-40 (fair + entry<0.30)
  D: 15 cekirdek filtresiz + 16-25 (fair) + 26-40 (fair + entry<0.30) kademeli
  E: kontrol — tum 40 filtresiz
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
# bias siralama (en az sapan -> en cok sapan)
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

def single_bet(code, day, models, require_fair, require_low_entry):
    """Tek esik. (bet_var, won, entry)"""
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
        if found is None:
            return None
        thr, o = found
    found_mid = None
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series:
            found_mid = mid; break
    if found_mid is None:
        return None
    entry = ob_series[found_mid][0][1]
    if not (0 < entry < 0.95):
        return None
    if require_low_entry and entry >= 0.30:
        return None
    if require_fair:
        std = (max(kvals)-min(kvals))/2.0 if len(kvals) > 1 else 1.0
        fair = estimate_probability_empirical(mean, thr, 'HIGH', 'temperature_max', lag_hours=48)
        if entry >= fair:
            return None
    return (o, entry)

def run(variant):
    pnl = 0.0
    total = won = 0
    for (code, day), models in fc.items():
        if len(models) < 2:
            continue
        rank = bias_order.index(code) if code in bias_order else 999
        if rank >= 40:
            continue
        if rank < 15:
            # cekirdek: her zaman
            require_fair = require_low = False
        else:
            # genisletilmis bolge: variant'a gore
            if variant == 'A':
                require_fair, require_low = True, False
            elif variant == 'B':
                require_fair, require_low = False, True
            elif variant == 'C':
                require_fair, require_low = True, True
            elif variant == 'D':
                if rank < 25:
                    require_fair, require_low = True, False
                else:
                    require_fair, require_low = True, True
            else:  # E: filtresiz 40
                require_fair = require_low = False
        res = single_bet(code, day, models, require_fair, require_low)
        if res is None:
            continue
        o, entry = res
        fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
        gain = (STAKE/entry)-cost if o else -cost
        pnl += gain; total += 1
        if o: won += 1
    return pnl, total, won

print("=== HIBRIT SIMULASYON (tek esik, $1, orderbook fill) ===")
print(f"{'variant':<8} {'kural':<50} {'bet':>5} {'won':>4} {'winrate':>8} {'PnL':>10}")
variants = {
    'A': '15 cekirdek + 16-40(fair)',
    'B': '15 cekirdek + 16-40(entry<0.30)',
    'C': '15 cekirdek + 16-40(fair+entry<0.30)',
    'D': '15 + 16-25(fair) + 26-40(fair+entry<0.30)',
    'E': 'kontrol: tum 40 filtresiz',
}
for v in 'ABCDE':
    pnl, n, w = run(v)
    print(f"{v:<8} {variants[v]:<50} {n:>5} {w:>4} {w/max(n,1)*100:>7.1f}% {pnl:>10.2f}")
