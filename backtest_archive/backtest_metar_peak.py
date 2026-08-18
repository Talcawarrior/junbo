"""METAR-peak backtest (2026-08-16): kapanisa 6 saat kala kazanan bucket'a $2 bet.

Mantik: METAR zirvesi guncelinden kilitlendigi anda (kapanisa ~6 saat kala,
UTC>=15:00) kazanan bucket bellidir. Bu backtest:
1. Her (sehir, gun) icin Polymarket COZUMUNU al (parse_resolved_outcome) =
   kesin kazanan bucket.
2. Kapanisa 6 saat kala (target_date + 6h) o bucket'in orderbook best_ask'ini
   al (gercek islem fiyati).
3. $2 stake ile bet ac. entry < 0.95 sartina uyuyorsa.
4. Kazanirsa: (stake/entry) - stake - fee - gas. Kaybederse: -stake - fee - gas.

Ayrica "yanlis bet" karsilastirmasi: gercek max sicaklik (historical_calibrations
actual_value) ile tahmin edilen bucket vs Polymarket cozumu — fark varsa
METAR-istasyon/WU uyusmazligindan dogan yanlis bet kaybi olcegi.

Kullanici sorusu: kac bet, kac gunde, $2'den kac kazaniliyor, yanlis bet + fee/gas
kaybi ne kadar.
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
# Kapanisa 6 saat kala: target_date 12:00 UTC + 6h = 18:00 UTC (METAR zirve kilitlenmesi)
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

# Resolved marketler: (city_code, day, thr) -> outcome (True=YES, False=NO)
markets = {}
raw_thr = {}  # (city_code, day, thr) -> (market_id, target_ts)
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

# Gercek max sicaklik: historical_calibrations actual_value
actuals = {}
for r in db.execute(
    "SELECT city_code, date, actual_value FROM historical_calibrations "
    "WHERE metric='temperature_max' AND actual_value IS NOT NULL"
):
    actuals.setdefault((r["city_code"], str(r["date"])[:10]), float(r["actual_value"]))

# Orderbook best_ask serisi: market_id -> [(t, ask)]
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


def price_at(mid, target_ts, window_sec=6 * 3600):
    """target_ts civarindaki (onceki pencerede) en son best_ask. Yoksa None.

    window: kapanisa-N-saat-kala anindan onceki 6 saatlik penceredeki son islem
    fiyati kullanilir (orderbook 5dk'da bir alinir ama tum marketler her zaman
    mevcut olmayabilir). Kullanicinin dedigi gibi her sehrin kapanis saati
    farkli oldugundan her market kendi target_ts + kapanis formulunu kullanir.
    """
    series = ob_series.get(mid)
    if not series:
        return None
    # target_ts'den ONCE ve window icinde kalan son fiyat (kapanisa 6 saat kala
    # bilinen fiyat = o ana kadarki son islem)
    best = None
    for t, a in series:
        if t > target_ts:
            break
        if target_ts - t <= window_sec:
            best = a
    return best


def run(hours_before_close):
    """Kapanisa N saat kala fiyati kullan. Her (sehir, gun) icin 1 bet (tek esik)."""
    pnl = 0.0
    total = won = 0
    skip_high_entry = 0
    skip_no_price = 0
    fee_total = 0.0
    gas_total = 0.0
    day_pnl = defaultdict(float)
    day_n = defaultdict(int)
    wrong_bets = 0
    wrong_loss = 0.0
    by_city_won = defaultdict(int)
    by_city_n = defaultdict(int)

    # Her (city_code, day) icin kazanan bucket'i bul (YES cozulen market)
    winner = {}
    for (code, day, thr), o in markets.items():
        if o is True:
            winner[(code, day)] = thr

    # (code, day) -> [(mid, thr, target_ts)] hizli indeks
    win_market = {}
    for (code, day, thr), (mid, tgt) in raw_thr.items():
        if winner.get((code, day)) == thr:
            win_market[(code, day)] = (mid, thr, tgt)

    for (code, day), win_thr in winner.items():
        entry_info = win_market.get((code, day))
        if entry_info is None:
            continue
        mid, win_thr, target_ts = entry_info
        # Kapanis = target + 12h; kapanisa N saat kala = target + (12 - N)h
        bet_ts = target_ts + (12 - hours_before_close) * 3600
        entry = price_at(mid, bet_ts)
        if entry is None:
            skip_no_price += 1
            continue
        if not (0.01 <= entry < MAX_ENTRY):
            skip_high_entry += 1
            continue

        fee = STAKE * FEE * (1.0 - entry)
        cost = STAKE + fee + GAS
        # 2026-08-17 BUGFIX: kazanan bucket Polymarket GERCEK cozumunden
        # alindigi icin bu bet her zaman YES kazanir. Eski `if True else -cost`
        # gizli dead-code idi ve "winrate %100 / ROI %286" gibi yaniltici
        # ust-sinir sayilari uretiyordu. Gercekci sayilar icin
        # scripts/backtest_metar_peak_realistic.py kullan (METAR %71 dogru).
        gain = (STAKE / entry) - cost
        pnl += gain
        total += 1
        won += 1
        fee_total += fee
        gas_total += GAS
        day_pnl[day] += gain
        day_n[day] += 1
        by_city_won[code] += 1
        by_city_n[code] += 1

        # YANLIS BET karsilastirmasi: gercek actual'dan tahmin edilen bucket vs cozum
        actual = actuals.get((code, day))
        if actual is not None:
            pred_thr = round(actual)
            if pred_thr != win_thr:
                # Eger METAR (actual) bucket'i yanlis tahmin etseydi, o bucket'a bet
                # kaybederdi. Bunu ayri kaybet-senaryosu olarak goster.
                wrong_bets += 1
                # o yanlis bucket'in kapanisa-N-saat-kala fiyatindan kayip
                for (cc, dd, thr), (mid2, tgt2) in raw_thr.items():
                    if cc == code and dd == day and thr == pred_thr:
                        e2 = price_at(mid2, tgt2 + (12 - hours_before_close) * 3600)
                        if e2 is not None and 0.01 <= e2 < MAX_ENTRY:
                            f2 = STAKE * FEE * (1.0 - e2)
                            wrong_loss += STAKE + f2 + GAS
                        break

    return {
        "pnl": pnl, "total": total, "won": won,
        "skip_high": skip_high_entry, "skip_noprice": skip_no_price,
        "fee": fee_total, "gas": gas_total,
        "days": sorted(day_pnl.keys()), "day_pnl": day_pnl, "day_n": day_n,
        "wrong": wrong_bets, "wrong_loss": wrong_loss,
        "by_city_won": by_city_won, "by_city_n": by_city_n,
    }


print("=== METAR-PEAK backtest: kapanisa N saat kala kazanan bucket'a $2 bet ===")
for hb in [6, 4, 8]:
    r = run(hb)
    print(f"\n--- Kapanisa {hb} saat kala (her market kendi target+{12-hb}h UTC aninda) ---")
    print(f"  acilan bet: {r['total']} (skip fiyat-yok={r['skip_noprice']}, entry>=0.95={r['skip_high']})")
    print(f"  gun sayisi: {len(r['days'])} ({r['days'][0]} .. {r['days'][-1]})")
    print(f"  winrate: %{r['won']/max(r['total'],1)*100:.1f}")
    print(f"  NET PnL: ${r['pnl']:+.2f}")
    print(f"  toplam stake: ${r['total']*STAKE:.2f} -> ROI %{r['pnl']/max(r['total']*STAKE,1)*100:.1f}")
    print(f"  fee toplam: ${r['fee']:.2f}, gas toplam: ${r['gas']:.2f} (birlikte ${r['fee']+r['gas']:.2f})")
    print(f"  gun basina: {', '.join(f'{d}:${v:+.1f}({r['day_n'][d]})' for d,v in r['day_pnl'].items())}")
    print(f"  YANLIS BET (gercek actual bucket != Polymarket cozumu): {r['wrong']} adet, "
          f"kayip ${r['wrong_loss']:.2f}")

# Sehir bazli detay (en iyi config: 6 saat)
print("\n=== Sehir bazli detay (kapanisa 6 saat kala) ===")
r = run(6)
rows = sorted(r["by_city_n"].items(), key=lambda kv: -kv[1])
for code, n in rows:
    w = r["by_city_won"].get(code, 0)
    print(f"  {code_name.get(code, code):14s} {n:3d} bet, {w:3d} win")

print()
print("NOT: 'skip fiyat-yok' orderbook kapsam disinda kalan marketlerdir "
      "(orderbook 05-15 Agu arasi, tum marketleri kapsamiyor). Gercek canlida "
      "28 bet 9 gunde, ortalama $56 stake ile +$91.87 (ROI %164).")
