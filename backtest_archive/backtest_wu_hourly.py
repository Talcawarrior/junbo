"""WU GUN-ICI OLCUM SIMULASYONU: her sehrin yerel 16-17-18 saat verisiyle
kazanan bucket'i sec, o anki orderbook fiyatindan YES al.

Fikir (kullanici): WU kapanis verisini alanlar son dakikada kazanan bucket'a basiyor.
Biz de Open-Meteo ARCHIVE hourly (gercek olcum) ile yerel 16-17-18 degerlerini
alip, o ana kadar gorulen max sicakliga en yakin bucket'i secip bet atalim.

Metod:
- Her (sehir, gun) icin archive hourly (timezone=auto) -> yerel saat degerleri
- Yerel 16:00, 17:00, 18:00 degerleri -> o ana kadar max (cummax)
- En yakin threshold bucket'i sec (RANGE 'be X')
- O bucket'in orderbook fiyati: yerel o an -> UTC karsiligi, en yakin ask
- Gercek outcome ile karsilastir

Karsilastirma: yerel 16 vs 17 vs 18 (sadece o saate kadar veri kullan — look-ahead yok)
"""
import sqlite3, sys, math
from collections import defaultdict
from datetime import datetime, timedelta
import requests, time as _time
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')
sys.stdout.reconfigure(encoding='utf-8')
from utils.market_outcome import parse_resolved_outcome

BOT_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\bot.db'
OB_DB = r'C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db'
STAKE = 1.0; FEE = 0.05; GAS = 0.10
ARCHIVE = 'https://archive-api.open-meteo.com/v1/archive'
START = '2026-08-05'
END = '2026-08-14'

def ts(s):
    s = str(s).replace('T',' ').replace('+00:00','').strip()
    try: return datetime.fromisoformat(s).timestamp()
    except: return None

db = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=10)
cur = db.cursor()
code_name = {}
for c, code in cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"):
    if code and c: code_name.setdefault(code, c)

# market outcome + coords
markets = {}
coords = {}
for code, thr, tdate, raw, lat, lon in cur.execute(
    "SELECT city_code, threshold, target_date, raw_data, latitude, longitude FROM weather_markets "
    "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL AND latitude != 0"
):
    o = parse_resolved_outcome(raw)
    if o is None: continue
    day = str(tdate)[:10]
    markets[(code, day, float(thr))] = o
    coords.setdefault(code, (float(lat), float(lon)))

ob = sqlite3.connect(f"file:{OB_DB}?immutable=1", uri=True, timeout=10)
oc = ob.cursor()
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

def ask_at_utc(code, day, thr, until_t):
    for mid, key in mid_key.items():
        if key == (code, day, thr) and mid in ob_series:
            best = None
            for t, a in ob_series[mid]:
                if t <= until_t: best = a
                else: break
            return best
    return None

# Archive hourly'i her sehir icin tek istekte cek (05-14)
hourly_cache = {}
for code, (lat, lon) in coords.items():
    _time.sleep(0.6)
    try:
        r = requests.get(ARCHIVE, params={
            'latitude': lat, 'longitude': lon,
            'hourly': 'temperature_2m',
            'start_date': START, 'end_date': END,
            'temperature_unit': 'celsius', 'timezone': 'auto',
        }, timeout=20)
        r.raise_for_status()
        d = r.json()
        offset = d.get('utc_offset_seconds', 0)
        times = d['hourly']['time']
        temps = d['hourly']['temperature_2m']
        # (day, hour_local) -> temp
        by_day = defaultdict(dict)
        for i, t in enumerate(times):
            day = t[:10]
            hh = int(t[11:13])
            if temps[i] is not None:
                by_day[day][hh] = float(temps[i])
        hourly_cache[code] = (offset, by_day)
        print(f"  {code}: {len(by_day)} gun okundu")
    except Exception as exc:
        print(f"  {code}: HATA {exc}")

# Simule: yerel X saatinde o ana kadar cummax ile bucket sec
def simulate(local_hour):
    pnl = 0.0; n = w = 0; entry_sum = 0.0
    for (code, day, thr), o in markets.items():
        if code not in hourly_cache: continue
        offset, by_day = hourly_cache[code]
        if day not in by_day: continue
        temps = by_day[day]
        # o ana kadar (0..local_hour) max sicaklik — sadece gecmis saatler
        hours_so_far = [temps[h] for h in range(0, local_hour + 1) if h in temps]
        if not hours_so_far: continue
        observed_max = max(hours_so_far)
        # en yakin bucket
        nearest = round(observed_max)
        # bu bucket'a market var mi + outcome var mi
        o2 = markets.get((code, day, float(nearest)))
        if o2 is None: continue
        # o anki orderbook fiyati: local_hour -> UTC
        # local time = day T local_hour:00 ; UTC = local - offset
        local_dt = datetime.fromisoformat(f"{day}T{local_hour:02d}:00:00")
        utc_ts = local_dt.timestamp() - offset
        entry = ask_at_utc(code, day, float(nearest), utc_ts)
        if entry is None or not (0 < entry < 0.95): continue
        fee = STAKE*FEE*(1.0-entry); cost = STAKE+fee+GAS
        gain = (STAKE/entry)-cost if o2 else -cost
        pnl += gain; n += 1; entry_sum += entry
        if o2: w += 1
    return pnl, n, w, entry_sum

print()
print("=== WU GUN-ICI: yerel X saatinde olcumle bucket sec, o anki fiyattan YES ===")
print(f"{'yerel saat':<10} {'bet':>5} {'won':>4} {'winrate':>8} {'ort_entry':>10} {'PnL':>10}")
for lh in [16, 17, 18, 19, 20, 21, 22, 23]:
    pnl, n, w, es = simulate(lh)
    print(f"{lh:>10} {n:>5} {w:>4} {w/max(n,1)*100:>7.1f}% {es/max(n,1):>10.3f} {pnl:>10.2f}")
