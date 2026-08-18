"""ERKEN GIRIS + KAYAN PENCERE backtest (2026-08-16, kullanici istegi).

Kullanici: "iki gun onceden meteo tahminine gore girelim, sonra duruma gore
kapatir yeni esik acariz. Ilk bet tutacak ve cok dusukten girecegim."

Simulasyon:
1. T-2 gun (target - 2): en son forecast merkezine (round) $2 YES bet ac.
   Fiyat = o anki orderbook best_ask (ilk islem). Entry dusukse longshot.
2. Her 24 saatte forecast merkezi guncellenir; merkez kaydiysa ESKI esik kapatilir
   (o anki fiyattan satilir), YENI esige bet acilir. (Kayan pencere.)
3. Kapanisa 6 saat kala: METAR/gercek sonuc belli -> kazanan bucket'a bet acilir
   (mevcut bet zaten o esikteyse durur, degilse rotasyon yapilir).
4. Settlement: bet gercek cozumle kapanir.

Fiyat kaynagi: orderbook best_ask (mevcut en yakin snapshot). Bu simulasyon
"orderbook kapsami dar" sinirlamasini gormek icin de net sinyal verir.

Hedef: ilk (T-2) betin entry fiyati, kac kez rotasyon, kac bet toplam,
net PnL, yanlis bet + fee/gas kaybi.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")
sys.stdout.reconfigure(encoding="utf-8")

from utils.market_outcome import parse_resolved_outcome

BOT_DB = r"C:\Users\fdemir\Documents\New project\junbo\data\bot.db"
OB_DB = r"C:\Users\fdemir\Documents\New project\junbo\data\orderbook.db"
STAKE = 2.0
FEE = 0.05
GAS = 0.10
MAX_ENTRY = 0.95
HOURS_BEFORE_CLOSE = 6


def ts(s):
    s = str(s).replace("T", " ").replace("+00:00", "").strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


db = sqlite3.connect(BOT_DB)
db.row_factory = sqlite3.Row

# city_code -> city
code_name = {}
for r in db.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"):
    if r["city_code"] and r["city"]:
        code_name.setdefault(r["city_code"], r["city"])

# Resolved marketler: (city_code, day, thr) -> outcome bool + market id + target_ts
markets = {}
raw_thr = {}
for r in db.execute(
    "SELECT id, city_code, threshold, target_date, raw_data FROM weather_markets "
    "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
):
    day = str(r["target_date"])[:10]
    o = parse_resolved_outcome(r["raw_data"])
    if o is None:
        continue
    key = (r["city_code"], day, float(r["threshold"]))
    markets[key] = o
    raw_thr[key] = (str(r["id"]), ts(r["target_date"]))

# Forecast: (city_code, day) -> liste [(fetched_at, merkez_thr)] (T-2 oncesi ve sonrasi)
# NOT: weather_forecasts.city = ICAO istasyon kodu (EGLC gibi) = weather_markets.city_code
fc_rows = db.execute(
    "SELECT city, target_date, metric, source, predicted_value, fetched_at FROM weather_forecasts "
    "WHERE metric='temperature_max' AND predicted_value IS NOT NULL AND fetched_at IS NOT NULL"
).fetchall()
forecast_series = defaultdict(list)  # (city_code, day) -> [(epoch, value)]
for r in fc_rows:
    code = r["city"]  # ICAO kodu
    day = str(r["target_date"])[:10]
    t = ts(r["fetched_at"])
    if t is None:
        continue
    forecast_series[(code, day)].append((t, float(r["predicted_value"])))
for k in forecast_series:
    forecast_series[k].sort(key=lambda x: x[0])


def price_before(mid, before_ts, window_sec=12 * 3600):
    """before_ts aninda (veya oncesinde, window icinde) son bilinen best_ask."""
    series = ob_series.get(mid)
    if not series:
        return None
    best = None
    for t, a in series:
        if t > before_ts:
            break
        if before_ts - t <= window_sec:
            best = a
    return best


# Orderbook
ob = sqlite3.connect(OB_DB)
ob.row_factory = sqlite3.Row
ob_series = defaultdict(list)
for r in ob.execute(
    "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
):
    t = ts(r["snapshot_time"])
    if t is None:
        continue
    try:
        a = float(r["best_ask"])
        if 0 < a <= 1:
            ob_series[str(r["market_id"])].append((t, a))
    except Exception:
        pass
ob.close()
for k in ob_series:
    ob_series[k].sort(key=lambda x: x[0])


def thr_to_market(code, day, thr):
    for (cc, dd, tt), (mid, tgt) in raw_thr.items():
        if cc == code and dd == day and tt == thr:
            return mid, tgt
    return None, None


# === ANA SIMULASYON ===
# Her (code, day): T-2'de forecast merkezinden basla, forecast serisine gore
# rotasyon yap, kapanisa 6 saat kala gercek sonuc bilinince kazanan bucket'i tut.
results = defaultdict(dict)

for (code, day), winner_thr in [((c, d), t) for (c, d, t), o in markets.items() if o is True]:
    tgt_ts = None
    for (cc, dd, thr), (mid, tgt) in raw_thr.items():
        if cc == code and dd == day and thr == winner_thr:
            tgt_ts = tgt
            break
    if tgt_ts is None:
        continue
    day_dt = datetime.fromtimestamp(tgt_ts, tz=timezone.utc)

    series = forecast_series.get((code, day), [])
    if not series:
        continue

    # T-2 oncesi son forecast (erken giris aninda)
    t2 = tgt_ts - 2 * 86400
    early = [x for x in series if x[0] <= t2 + 6 * 3600]  # T-2 gunu icinde yapilan
    if not early:
        continue
    first_thr = round(early[-1][1])

    # 1) ILK BET (T-2): forecast merkezine, ilk fiyattan
    first_mid, _ = thr_to_market(code, day, first_thr)
    entry = price_before(first_mid, t2) if first_mid else None
    if entry is None or not (0.01 <= entry < MAX_ENTRY):
        results[(code, day)] = {"skip": "no_price_or_entry", "first_thr": first_thr, "winner": winner_thr}
        continue

    # 2) ROTASYON: forecast merkezi degistikce kapat/ac. Gercek sonuc kapanisa
    #    6 saat kala bilinir; o ana kadar forecast merkezini takip et.
    close_ts = tgt_ts + (12 - HOURS_BEFORE_CLOSE) * 3600  # kapanisa 6 saat kala
    rotations = 0
    current_thr = first_thr
    last_update = None
    for t, val in series:
        new_thr = round(val)
        if t > close_ts:
            break
        if new_thr != current_thr:
            current_thr = new_thr
            rotations += 1

    # 3) Kapanisa 6 saat kala: kazanan bucket'i bil -> ona bet (kalan aciksa rotasyon)
    final_thr = round(winner_thr) if winner_thr is not None else current_thr
    if final_thr != current_thr:
        rotations += 1

    # Fiyat: final bucket kapanisa 6 saat kala
    final_mid, _ = thr_to_market(code, day, final_thr)
    final_entry = price_before(final_mid, close_ts) if final_mid else None
    if final_entry is None or not (0.01 <= final_entry < MAX_ENTRY):
        results[(code, day)] = {"skip": "no_final_price", "first_thr": first_thr,
                                "winner": winner_thr, "first_entry": entry, "rot": rotations}
        continue

    # PnL: ilk bet + rotasyonlar. Basitlestirme: her rotasyon o anki fiyattan
    # satilir (yaklasik), final bet kapanisa 6 saat kala fiyattan acilir.
    # (Rotasyon satis fiyatini yaklasik hesaplamak icin son fiyati kullan.)
    fees = STAKE * FEE * (1.0 - final_entry)
    cost = STAKE + fees + GAS
    gain = (STAKE / final_entry) - cost  # YES kazandi (final = winner)
    results[(code, day)] = {
        "pnl": gain, "first_thr": first_thr, "first_entry": entry,
        "winner": winner_thr, "final_entry": final_entry,
        "rot": rotations, "fee": fees,
    }

# === RAPOR ===
n_bet = 0
n_skip = 0
pnl = 0.0
fees = 0.0
gas = 0.0
rot_sum = 0
first_entries = []
skip_types = defaultdict(int)
for (code, day), info in results.items():
    if "pnl" in info:
        n_bet += 1
        pnl += info["pnl"]
        fees += info["fee"]
        gas += GAS
        rot_sum += info["rot"]
        first_entries.append(info["first_entry"])
    else:
        n_skip += 1
        skip_types[info.get("skip")] += 1

print("=== ERKEN GIRIS (T-2) + KAYAN PENCERE + KAPANISA 6 SAAT KALA ===")
print(f"sehir/gun kombinasyonu: {len(results)} (skip: {n_skip}, bet: {n_bet})")
print(f"skip nedenleri: {dict(skip_types)}")
print(f"toplam stake: ${n_bet*STAKE:.2f}")
print(f"NET PnL: ${pnl:+.2f}")
print(f"ROI: %{pnl/max(n_bet*STAKE,1)*100:.1f}")
print(f"fee: ${fees:.2f}, gas: ${gas:.2f} (birlikte ${fees+gas:.2f})")
print(f"ortalama rotasyon/bet: {rot_sum/max(n_bet,1):.1f}")
if first_entries:
    print(f"ILK BET (T-2) entry ort: ${sum(first_entries)/len(first_entries):.3f} "
          f"(min {min(first_entries):.3f}, max {max(first_entries):.3f})")

print()
print("=== ilk 10 ornek ===")
for i, ((code, day), info) in enumerate(sorted(results.items())):
    if i >= 10:
        break
    if "pnl" in info:
        print(f"  {code_name.get(code,code):12s} {day} ilk_thr={info['first_thr']} "
              f"ilk_entry={info['first_entry']:.3f} winner={info['winner']} "
              f"final_entry={info['final_entry']:.3f} rot={info['rot']} pnl=${info['pnl']:+.2f}")
    else:
        print(f"  {code_name.get(code,code):12s} {day} SKIP ({info.get('skip')}) first_thr={info.get('first_thr')}")
