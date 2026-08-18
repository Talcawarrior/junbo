"""CLAIRVOYANT sim: kazanan bucket'i ONCEDEN bilseydik (WU kapanis verisi gibi),
kapanisa X saat kala o markete girseydik ne olurdu? Bu, son dakika WU bet'lerinin edge'ini olcer.

Soru: 'WU kapanis verisi alanlar son dakikada bet basiyor, onlar nasil ediyor?'
Cevap icin: kazanan marketin kapanisa X saat kala fiyati ne? O fiyattan YES alirsak?
"""
import sqlite3, sys
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')
sys.stdout.reconfigure(encoding='utf-8')
from utils.market_outcome import parse_resolved_outcome

BOT_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\bot.db'
OB_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db'
STAKE = 1.0; FEE = 0.05; GAS = 0.10

def ts(s):
    s = str(s).replace('T',' ').replace('+00:00','').strip()
    try: return datetime.fromisoformat(s).timestamp()
    except: return None

db = sqlite3.connect(BOT_DB); cur = db.cursor()
markets = {}
market_td = {}
for code, thr, tdate, raw in cur.execute("SELECT city_code, threshold, target_date, raw_data FROM weather_markets WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"):
    o = parse_resolved_outcome(raw)
    if o is None: continue
    day = str(tdate)[:10]
    markets[(code, day, float(thr))] = o
    market_td[(code, day, float(thr))] = ts(str(tdate)) or 0

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

def ask_before(code, day, thr, until_t):
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series:
            best = None
            for t, a in ob_series[mid]:
                if t <= until_t: best = a
                else: break
            return best
    return None

def last_ask_before_close(code, day, thr, td_ts, hours_before):
    """Kapanis (td+12h) - X saat onceki fiyat. Eger o anda market acik degilse None."""
    until_t = td_ts + 43200 - hours_before * 3600
    return ask_before(code, day, thr, until_t)

# Kazanan marketler (YES kazanan): onlari bilseydik, kapanisa X saat kala girseydik
print("=== CLAIRVOYANT: kazanan bucket'i bilip kapanisa X saat kala YES al ===")
print(f"{'giris zamani':<14} {'bet':>5} {'won':>4} {'winrate':>8} {'ort_entry':>10} {'PnL':>10}")
for hb in [48, 24, 12, 6, 3, 1, 0]:
    pnl = 0.0; n = w = 0; entry_sum = 0.0
    for (code, day, thr), o in markets.items():
        if not o:
            continue  # sadece KAZANAN marketler
        td = market_td[(code, day, thr)]
        entry = last_ask_before_close(code, day, thr, td, hb)
        if entry is None or not (0 < entry < 0.95):
            continue
        # kazanan biliyoruz -> YES kazandi -> payout = stake/entry - cost
        fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
        gain = (STAKE/entry) - cost
        pnl += gain; n += 1; w += 1; entry_sum += entry
    print(f"{('kapanis-%dh' % hb) if hb else 'kapanis aninda':<14} {n:>5} {w:>4} "
          f"{w/max(n,1)*100:>7.1f}% {entry_sum/max(n,1):>10.3f} {pnl:>10.2f}")

print()
print("=== KAYBEDENLER: onlardan uzak duruyoruz (NO almak yerine) ===")
print("-> Clairvoyant sadece kazananlari alir, kaybedenlere girmez (NO almaz, sadece atlar)")
print("-> WU insanlari da boyle: gercek olcumu gorup SADECE kazanacak bucket'a YES basar")
