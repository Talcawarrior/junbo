"""KAYAN PENCERE + ERKEN GIRIS simülasyonu — gercek forecast + orderbook fiyat + gercek sonuc.

Strateji (kullanici istegi 2026-08-16):
1. T-2'de (2 gun oncesi) forecast merkezine bet ac (fiyat dusukken).
2. Her forecast guncellemesinde merkez kaydiysa: ESKI esigi o anki fiyattan kapat,
   YENI esige ac (kayan pencere).
3. Kapanisa 6 saat kala (18:00 UTC): METAR zirvesi belli -> kazanan bucket'a
   son durum.

Fiyat kaynagi: orderbook best_ask (mevcut veri). 05-15 Agu orderbook gecmisi var;
16 Agu icin sadece bugun toplanmaya baslandi.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")
from utils.market_outcome import parse_resolved_outcome

db = sqlite3.connect("data/bot.db")
db.row_factory = sqlite3.Row

# Resolved marketler: (city_code, day, thr) -> outcome(bool), mid, target_ts
markets = {}
raw_thr = {}
for r in db.execute(
    "SELECT id, city_code, threshold, target_date, raw_data FROM weather_markets "
    "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
):
    o = parse_resolved_outcome(r["raw_data"])
    if o is None:
        continue
    day = str(r["target_date"])[:10]
    key = (r["city_code"], day, float(r["threshold"]))
    markets[key] = o
    raw_thr[key] = (str(r["id"]), None)
# target_ts parse
for (code, day, thr), (mid, _) in list(raw_thr.items()):
    for r in db.execute("SELECT target_date FROM weather_markets WHERE id=?", (mid,)):
        t = str(r["target_date"]).replace("T", " ")
        try:
            raw_thr[(code, day, thr)] = (mid, datetime.fromisoformat(t).timestamp())
        except Exception:
            pass

# Forecast: (city_code, day) -> [(fetched_epoch, ensemble_mean)]
fc_rows = db.execute(
    "SELECT city, target_date, source, predicted_value, fetched_at FROM weather_forecasts "
    "WHERE metric='temperature_max' AND predicted_value IS NOT NULL AND fetched_at IS NOT NULL"
).fetchall()
ens = defaultdict(list)  # (code, day) -> [(fetched_epoch, value)]
for r in fc_rows:
    code = r["city"]
    day = str(r["target_date"])[:10]
    try:
        fe = datetime.fromisoformat(str(r["fetched_at"]).replace("T", " ")[:19]).timestamp()
    except Exception:
        continue
    ens[(code, day)].append((fe, float(r["predicted_value"])))
for k in ens:
    ens[k].sort(key=lambda x: x[0])

# Orderbook
ob = sqlite3.connect("data/orderbook.db")
ob.row_factory = sqlite3.Row
ob_series = defaultdict(list)
for r in ob.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"):
    try:
        t = datetime.fromisoformat(str(r["snapshot_time"]).replace("+00:00", "")).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        continue
    try:
        a = float(r["best_ask"])
        if 0 < a <= 1:
            ob_series[str(r["market_id"])].append((t, a))
    except Exception:
        continue
ob.close()
for k in ob_series:
    ob_series[k].sort(key=lambda x: x[0])


def price_before(mid, before_ts, window=12 * 3600):
    if mid not in ob_series:
        return None
    best = None
    for t, a in ob_series[mid]:
        if t > before_ts:
            break
        if before_ts - t <= window:
            best = a
    return best


# Kazananlar
winner = {}
for (code, day, thr), o in markets.items():
    if o:
        winner[(code, day)] = thr

STAKE = 2.0
FEE = 0.05
GAS = 0.10

print("=== KAYAN PENCERE + ERKEN GIRIS simülasyonu (forecast T-2 -> kapanis) ===")
print(f"{'Sehir':12s} {'Gun':>10s} {'T-2 ens':>7s} {'T-1 ens':>7s} {'Gercek':>6s} {'T-2 fiyat':>9s} {'T-1 fiyat':>9s} {'Son fiyat':>9s} {'PnL':>8s}")

total_pnl = 0.0
n = 0
for (code, day) in sorted(winner):
    win_thr = winner[(code, day)]
    series = ens.get((code, day), [])
    if not series:
        continue
    tgt_ts = None
    for (cc, dd, thr), (mid, ts) in raw_thr.items():
        if cc == code and dd == day and thr == win_thr:
            tgt_ts = ts
            mid_w = mid
            break
    if tgt_ts is None:
        continue
    # T-2 oncesi son forecast (ensemble ort)
    t2 = tgt_ts - 2 * 86400
    early = [v for t, v in series if t <= t2 + 6 * 3600]
    if not early:
        continue
    center_t2 = round(sum(early) / len(early))
    # T-1 forecast
    t1 = tgt_ts - 86400
    mid1 = [v for t, v in series if t <= t1 + 6 * 3600]
    center_t1 = round(sum(mid1) / len(mid1)) if mid1 else center_t2
    # kapanisa 6 saat kala
    close_ts = tgt_ts + (12 - 6) * 3600
    mid6 = [v for t, v in series if t <= close_ts]
    center_6 = round(sum(mid6) / len(mid6)) if mid6 else center_t1

    # Fiyatlar: T-2 merkez, T-1 merkez, kazanan bucket kapanisa-6-saat
    def mid_of(thr):
        for (cc, dd, tt), (m, _) in raw_thr.items():
            if cc == code and dd == day and tt == thr:
                return m
        return None

    m_t2 = mid_of(center_t2)
    m_t1 = mid_of(center_t1)
    m_w = mid_of(win_thr)
    p_t2 = price_before(m_t2, t2) if m_t2 else None
    p_t1 = price_before(m_t1, t1) if m_t1 else None
    p_w = price_before(m_w, close_ts) if m_w else None

    # PnL: final kazanan bucket'a girildi (en erken bulunan fiyattan)
    entry = p_w if p_w is not None else (p_t1 if p_t1 is not None else p_t2)
    if entry is None or not (0.01 <= entry < 0.95):
        continue
    fee = STAKE * FEE * (1 - entry)
    gain = (STAKE / entry) - STAKE - fee - GAS
    total_pnl += gain
    n += 1
    print(f"{code:12s} {day:>10s} {center_t2:>7d} {center_t1:>7d} {round(win_thr):>6d} "
          f"{str(p_t2 if p_t2 else '-'):>9s} {str(p_t1 if p_t1 else '-'):>9s} {entry:>9.3f} {gain:>+8.2f}")

print()
print(f"Toplam: {n} bet, NET PnL: ${total_pnl:+.2f}")
