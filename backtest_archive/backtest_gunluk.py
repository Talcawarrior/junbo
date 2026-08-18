"""GUN GUN orderbook backtest — botun 2026-08-18 su anki modu (kullanici istegi).

Botun su anki calisma sekli birebir modellenir:
  SPREAD (spread_placer.py):
    - bias-top 15 sehir (METAR-aligned historical_calibrations |bias| siralamasi)
    - radius=0 -> TEK ESIK: center = round(ortalama model tahmini)
    - giris: ilk orderbook ask < spread_max_entry (0.95), stake $2
    - fair-value/edge filtresi YOK (2026-08-16 kullanici karari: 0.01-0.95 arasi)
    - kapanis: settlement'a TUTULUR; ama METAR-peak `_close_wrong_bucket_bets`
      sehrin kazanan bucket'i disindaki acik spread betlerini canli fiyattan kapatir.
      Model: peak kilitlendiginde (P=round(METAR max)) X != P ise o anki ask'ten
      SATILIR; X == P ise settlement'a tutulur (gercek cozumle cozulur).
  METAR-PEAK (jobs/metar_peak.py):
    - bias-top 40 sehir (BIAS_TOP_CITIES=40), bucket = round(KILITLI peak)
    - peak KILITLI: `detect_peak` (yerel saat >= 13 + 2 ardısık dusus) — bot
      final max'i BILMEZ, kilitlenen degerle girer (look-ahead YOK).
    - MIN_ENTRY 0.10, MIN_HOURS_BEFORE_CLOSE=2 (kapanisa <2sa kala bet yok)
    - giris fiyati: kilitlenme zamanindaki CLOB/orderbook fiyati
    - settlement'a tutulur, gercek Polymarket outcome ile cozulur.

Cozum = parse_resolved_outcome (weather_markets.raw_data) — GERCEK settlement,
look-ahead YOK. Maliyet = stake + fee + gas (fee = stake*0.05*(1-entry), gas=$0.10).

Kullanim:
    python scripts/backtest_gunluk.py [--days 2026-08-16,2026-08-17] [--fill first_ask]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils.market_outcome import parse_resolved_outcome  # noqa: E402
from scrapers.metar import city_utc_offset  # noqa: E402  (M3: gercek saat dilimi)

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
OB_DB = os.path.join(_REPO_ROOT, "data", "orderbook.db")

FEE_RATE = 0.05
GAS = 0.10
SPREAD_STAKE = 2.0
PEAK_STAKE = 3.0
MAX_ENTRY = 0.95
PEAK_MIN_ENTRY = 0.10
BIAS_TOP = 15  # spread_max_cities (spread_placer.py)
BIAS_TOP_PEAK = 40  # BIAS_TOP_CITIES (jobs/metar_peak.py)
MIN_HOURS_BEFORE_CLOSE = 2.0  # jobs/metar_peak.py
CLOSE_WINDOW_SEC = 6 * 3600  # peak zamanina en yakin ask icin arama penceresi


def ts(s) -> float | None:
    s = str(s).replace("T", " ").replace("+00:00", "").strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def ask_at_or_after(series, t, window_sec=CLOSE_WINDOW_SEC) -> float | None:
    """t zamanindan itibaren, window icindeki ilk ask (bot anlik girer)."""
    best = None
    for s, a in series:
        if s < t:
            continue
        if s - t > window_sec:
            break
        best = a
        break
    return best


def first_ask_below(series, max_entry) -> float | None:
    """Botun ilk giris ani: fiyat < max_entry oldugu ilk snapshot ask'i."""
    for _t, a in series:
        if 0 < a < max_entry:
            return a
    return None


def peak_lock(rows: list[tuple[float, float]], utc_off: float, min_hour: int = 13) -> tuple[float | None, float | None]:
    """KILITLI METAR peak + kilitlenme epoch'u — scrapers/metar.py detect_peak
    ile BIREBIR ayni kural: yerel saat >= 13 olduktan sonra cummax'in ardindan
    2 ardısık dusus -> peak teyit. (peak_temp, lock_epoch) ya da (None, None).
    """
    if len(rows) < 3:
        return (None, None)
    cummax = rows[0][1]
    drop_count = 0
    for epoch, cur in rows[1:]:
        local_dt = datetime.fromtimestamp(epoch + utc_off * 3600, tz=timezone.utc)
        if local_dt.hour < min_hour:
            cummax = max(cummax, cur)
            drop_count = 0
            continue
        if cur > cummax:
            cummax = cur
            drop_count = 0
        elif cur < cummax:
            drop_count += 1
            if drop_count >= 2:
                return (cummax, epoch)
        else:
            drop_count = 0
    return (None, None)


def cost_of(stake: float, entry: float) -> float:
    fee = stake * FEE_RATE * (1.0 - entry)
    return stake + fee + GAS


def main() -> int:
    parser = argparse.ArgumentParser(description="Gun gun gercekci backtest (su anki mod)")
    parser.add_argument("--days", default="2026-08-16,2026-08-17", help="virgullu target gunler")
    parser.add_argument("--detail", action="store_true", help="bet-bazli detay tablosu")
    args = parser.parse_args()
    days = [d.strip() for d in args.days.split(",") if d.strip()]

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    # city_code -> city
    code_name: dict[str, str] = {}
    for c, code in db.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # bias-top sehirler (en az sapan |bias|, METAR-aligned). SPREAD bias-top
    # 15 (spread_max_cities), METAR-PEAK bias-top 40 (BIAS_TOP_CITIES).
    bs, bc = {}, {}
    for code, bias in db.execute("SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL"):
        cn = code_name.get(code)
        if not cn:
            continue
        bs[cn] = bs.get(cn, 0) + abs(float(bias))
        bc[cn] = bc.get(cn, 0) + 1
    cb = {c: bs[c] / bc[c] for c in bs if bc[c] > 0}
    ordered = [c for c, _ in sorted(cb.items(), key=lambda kv: kv[1])]
    keep = {c for c in ordered[:BIAS_TOP]}
    keep_peak = {c for c in ordered[:BIAS_TOP_PEAK]}

    # market: (code, day, thr) -> (mid, target_ts, outcome, market_type)
    # SADECE temperature_max (spread + metar-peak max bucket'ina bet acar);
    # min marketleri ayni threshold'ta eslesip yanlis bet uretmesin diye filtrelenir.
    # threshold TAM degerle anahtarlanir: kesirli esikler (Atlanta 31.7) botun
    # `_find_market` threshold==center tam eslemesine girmeyecegi icin bet uretmez
    # (int(float()) bircok sehri yanlis eslestirip sahte bet ekliyordu).
    market: dict[tuple[str, str, float], tuple[str, float | None, bool | None, str]] = {}
    for r in db.execute(
        "SELECT id, city_code, threshold, target_date, raw_data, market_type FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL "
        "AND metric = 'temperature_max'"
    ):
        code, thr, day = r[1], r[2], str(r[3])[:10]
        if thr is None or not code or day not in days:
            continue
        o = parse_resolved_outcome(r[4])
        t = ts(r[3])
        market[(code, day, float(thr))] = (str(r[0]), t, o, r[5])

    # METAR KILITLI peak (look-ahead YOK): bot final max'i bilmez, `detect_peak`
    # (yerel saat >= 13 + 2 ardısık dusus) kilitlediginde girer. Final max ile
    # giris yapmak gecen gunun sonucunu "bilirdi" — yanlis yuksek winrate uretir.
    # metar_peak[(code, day)] = (kilitli_peak_temp, kilitlenme_epoch).
    lon: dict[str, float] = {}
    for code, lg in db.execute(
        "SELECT DISTINCT city_code, longitude FROM weather_markets "
        "WHERE city_code IS NOT NULL AND longitude IS NOT NULL"
    ):
        try:
            lon.setdefault(code, float(lg))
        except (TypeError, ValueError):
            pass
    day_rows: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for code, tmax, obs in db.execute(
        "SELECT city_code, temp_c, obs_time FROM metar_observations WHERE temp_c IS NOT NULL AND obs_time IS NOT NULL"
    ):
        day = str(obs)[:10]
        t = ts(obs)
        if code and day in days and t is not None:
            day_rows[(code, day)].append((t, float(tmax)))
    metar_peak: dict[tuple[str, str], tuple[float, float]] = {}
    for (code, day), rows in day_rows.items():
        rows.sort(key=lambda x: x[0])
        # 2026-08-18 audit fix (M3): gercek saat dilimi (zoneinfo + DST) —
        # round(lon/15) China/Seoul/London icin 1 saat yanlis veriyordu.
        utc_off = city_utc_offset(code, day, lon.get(code))
        pk_, lock_epoch = peak_lock(rows, utc_off)
        if pk_ is not None and lock_epoch is not None:
            metar_peak[(code, day)] = (float(pk_), lock_epoch)

    # tahminler: (code, day) -> model -> predicted_value (max)
    # Bot (spread_placer.py) EN SON fetched_at batch'ini kullanir
    # (func.max(fetched_at) per city+metric), sonra o batch'teki TUM modellerin
    # ortalamasi. Eski batch gecmiste kalmis tahmini kullanmaz — backtest de
    # aynisini yapar (ilk satir degil, en guncel fetch).
    fc: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    latest_fetch: dict[tuple[str, str], str] = {}
    raw_fc: list[tuple[str, str, str, str, float]] = []
    for code, tdate, src, f_at, pv in db.execute(
        "SELECT city, target_date, source, fetched_at, predicted_value FROM weather_forecasts "
        "WHERE predicted_value IS NOT NULL AND metric LIKE '%max%'"
    ):
        day = str(tdate)[:10]
        if day not in days or not code or f_at is None:
            continue
        key = (code, day)
        f_at_s = str(f_at)
        if key not in latest_fetch or f_at_s > latest_fetch[key]:
            latest_fetch[key] = f_at_s
        raw_fc.append((code, day, src, f_at_s, float(pv)))
    for code, day, src, f_at_s, pv in raw_fc:
        if f_at_s == latest_fetch.get((code, day)):
            fc[(code, day)].setdefault(src, pv)

    # Botun marketi ilk ne zaman gorebilecegi (scan oncesi bet yok).
    first_seen: dict[str, float] = {}
    for mid, fs in db.execute("SELECT id, first_seen FROM weather_markets WHERE first_seen IS NOT NULL"):
        t = ts(fs)
        if t is not None:
            first_seen[str(mid)] = t

    # Gercek fiyat serisi: CLOB prices-history (Polymarket'in kendi islem/quote
    # gecmisi, en yetkili kaynak) + orderbook best_ask (botun 5dk okumalari).
    # market_snapshots.yes_price KULLANILMAZ: market fonde olmadan once botun
    # yes_price okumasi 0'a yakin artefakt uretiyor (Seoul 27C: snapshot 0.015,
    # gercek CLOB fiyati 0.19) — boyle bir fiyattan emir dolmaz, giris sahte
    # WIN/PnL uretir. Giris fiyati her zaman GERCEK piyasa fiyatidir.
    price_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    ob = sqlite3.connect(OB_DB, timeout=30)
    ob.execute("PRAGMA busy_timeout=30000")
    try:
        ob_rows = list(
            ob.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL")
        )
    except sqlite3.OperationalError:
        import shutil
        import tempfile

        snap = os.path.join(tempfile.gettempdir(), "ob_gunluk.db")
        shutil.copy2(OB_DB, snap)
        ob.close()
        ob = sqlite3.connect(snap)
        ob_rows = list(
            ob.execute("SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL")
        )
    for mid, ask, st in ob_rows:
        t = ts(st)
        if t is None:
            continue
        try:
            a = float(ask)
            if 0 < a <= 1:
                price_series[str(mid)].append((t, a))
        except (TypeError, ValueError):
            pass
    ob.close()

    # CLOB prices-history (backfill_price_history.py): marketin TUM fiyat
    # gecmisi — ANA kaynak. orderbook onu tamamlar (CLOB bos ama bot okumus).
    bp_db = os.path.join(_REPO_ROOT, "data", "backtest_prices.db")
    if os.path.exists(bp_db):
        bp = sqlite3.connect(bp_db, timeout=30)
        try:
            bp_rows = list(bp.execute("SELECT market_id, ts, price FROM price_history WHERE price > 0 AND price <= 1"))
        except sqlite3.OperationalError:
            bp_rows = []
        bp.close()
        for mid, t, p in bp_rows:
            try:
                price_series[str(mid)].append((float(t), float(p)))
            except (TypeError, ValueError):
                pass
    for k in price_series:
        price_series[k].sort(key=lambda x: x[0])

    db.close()

    # ---- simülasyon ----
    # spread_bet: (day, city, code, bucket, entry, stake, pnl, won, exit_tip)
    # exit_tip: hold_seen (X==P), sold_peak (X!=P), hold_nopeak (METAR yok)
    spread: list[dict] = []
    peak: list[dict] = []

    for (code, day), models in fc.items():
        city = code_name.get(code)
        if not city or city not in keep or len(models) < 2:
            continue
        vals = list(models.values())
        # 2026-08-18 audit fix (C2): banker's round() yerine half-up (bot ile ayni)
        center = int(sum(vals) / len(vals) + 0.5)
        m = market.get((code, day, float(center)))
        if m is None:
            continue
        mid, tgt, outcome, _ = m
        seri = price_series.get(mid)
        if not seri:
            continue
        # bot marketi first_seen'den once goremez -> o oncesi fiyat noktalari
        # giris icin kullanilamaz (market acilmis ama bot kesfetmemis).
        fs = first_seen.get(mid)
        if fs is not None:
            seri = [pt for pt in seri if pt[0] >= fs]
        if not seri:
            continue
        # veri penceresi: ilk snapshot settlement sonrasi ise canli bet penceresi
        # gozlemlenememis -> giris fiyati yaniltici (0.001 cozum-sonrasi artefakti)
        if tgt is None or seri[0][0] >= tgt:
            continue
        entry = first_ask_below(seri, MAX_ENTRY)
        if entry is None:
            continue
        stake = SPREAD_STAKE
        cost = cost_of(stake, entry)
        shares = stake / entry

        pk = metar_peak.get((code, day))
        exit_tip = "hold_settlement"
        pnl = 0.0
        won = None
        if pk is not None:
            p_bucket, pk_t = pk
            P = int(p_bucket + 0.5)  # audit C2: half-up
            if P != center:
                # yanlis bucket: peak aninda canli fiyattan satilir
                pk_ask = ask_at_or_after(seri, pk_t) if pk_t else None
                if pk_ask is not None and 0 < pk_ask <= 1:
                    exit_tip = "sold_peak"
                    # kapanis maliyeti: satis tarafinda fee = stake*0.05*(1-ask) + gas
                    pnl = (pk_ask - entry) * shares - stake * FEE_RATE * (1.0 - pk_ask) - GAS
                    won = pk_ask > entry
                else:
                    exit_tip = "hold_settlement_nopk_ask"
        if exit_tip == "hold_settlement" or exit_tip == "hold_settlement_nopk_ask":
            if outcome is None:
                continue  # cozum yok -> bet sonuclanamaz
            exit_tip = "hold_settlement"
            won = outcome
            pnl = (stake / entry - cost) if outcome else -cost
        if won is None:
            continue
        spread.append(
            {
                "day": day,
                "city": city,
                "code": code,
                "bucket": center,
                "entry": entry,
                "stake": stake,
                "pnl": pnl,
                "won": won,
                "exit": exit_tip,
            }
        )

    # METAR-PEAK legi: bias-top 40, KILITLI peak bucket'i, SADECE RANGE
    for (code, day), (peak_temp, lock_epoch) in metar_peak.items():
        if day not in days:
            continue
        city = code_name.get(code)
        if not city or city not in keep_peak:
            continue
        B = int(peak_temp + 0.5)  # audit C2: half-up
        m = market.get((code, day, float(B)))
        # 2026-08-18 kullanici karari: peak bet'i SADECE RANGE marketlerine
        if m is None or m[3] != "RANGE":
            continue
        mid, tgt, outcome, _ = m
        # MIN_HOURS_BEFORE_CLOSE=2: kapanisa <2sa kala peak kilitlenirse bet yok
        if tgt is None or tgt - lock_epoch < MIN_HOURS_BEFORE_CLOSE * 3600:
            continue
        seri = price_series.get(mid)
        if not seri:
            continue
        fs = first_seen.get(mid)
        if fs is not None:
            seri = [pt for pt in seri if pt[0] >= fs]
        if not seri:
            continue
        entry = ask_at_or_after(seri, lock_epoch)
        if entry is None or not (PEAK_MIN_ENTRY <= entry < MAX_ENTRY):
            continue
        if outcome is None:
            continue
        stake = PEAK_STAKE
        cost = cost_of(stake, entry)
        won = outcome
        pnl = (stake / entry - cost) if won else -cost
        peak.append(
            {
                "day": day,
                "city": code_name.get(code, code),
                "code": code,
                "bucket": B,
                "entry": entry,
                "stake": stake,
                "pnl": pnl,
                "won": won,
                "exit": "hold_settlement",
            }
        )

    # ---- rapor ----
    print("=== GUN GUN BACKTEST (botun 2026-08-18 su anki modu, gercek orderbook + gercek cozum) ===")
    print(
        f"  SPREAD: bias-top {BIAS_TOP}, radius=0 (tek esik), stake=${SPREAD_STAKE:.0f}, "
        f"max_entry={MAX_ENTRY}, fair-filter YOK; kapanis: METAR-peak yanlis bucket satisi + settlement"
    )
    print(
        f"  METAR-PEAK: bias-top {BIAS_TOP_PEAK}, stake=${PEAK_STAKE:.0f}, MIN_ENTRY={PEAK_MIN_ENTRY}, "
        f"bucket=round(KILITLI METAR peak), kapanisa<{MIN_HOURS_BEFORE_CLOSE:.0f}sa YOK"
    )
    print(f"  fee=%{FEE_RATE*100:.0f} gas=${GAS:.2f}  |  gunler: {', '.join(days)}")
    print()

    for leg_name, bets in (("SPREAD", spread), ("METAR-PEAK", peak)):
        for day in days:
            db_ = [b for b in bets if b["day"] == day]
            if not db_:
                print(f"  [{leg_name:<10}] {day}: bet yok")
                continue
            n = len(db_)
            won = sum(1 for b in db_ if b["won"])
            pnl = sum(b["pnl"] for b in db_)
            staked = sum(b["stake"] for b in db_)
            fees = sum(b["stake"] * FEE_RATE * (1.0 - b["entry"]) for b in db_) + n * GAS
            print(
                f"  [{leg_name:<10}] {day}: bet={n:>3} kazandi={won:>3} "
                f"winrate=%{won / n * 100:>5.1f}  yatirilan=${staked:>7.2f}+fees ${fees:>6.2f} "
                f"NET=${pnl:>+8.2f} ROI=%{pnl / staked * 100:>+7.1f}"
            )
        n_all = len(bets)
        if n_all:
            won = sum(1 for b in bets if b["won"])
            pnl = sum(b["pnl"] for b in bets)
            staked = sum(b["stake"] for b in bets)
            print(
                f"  [{leg_name:<10}] TOPLAM:  bet={n_all:>3} kazandi={won:>3} "
                f"winrate=%{won / n_all * 100:>5.1f}  yatirilan=${staked:>7.2f} "
                f"NET=${pnl:>+8.2f} ROI=%{pnl / staked * 100:>+7.1f}"
            )
        print()

    if args.detail:
        print("--- bet detay ---")
        for b in sorted(spread + peak, key=lambda x: (x["day"], x["city"])):
            print(
                f'  {b["day"]} {b["city"]:<14} {"S" if b in spread else "P"} '
                f'bucket={b["bucket"]:<3} entry={b["entry"]:.3f} '
                f'{"WIN" if b["won"] else "LOSS":<4} pnl=${b["pnl"]:+.2f} exit={b["exit"]}'
            )
        print()

    comb = spread + peak
    pnl = sum(b["pnl"] for b in comb)
    staked = sum(b["stake"] for b in comb)
    won = sum(1 for b in comb if b["won"])
    print(
        f"  [BIRLESIK    ] bet={len(comb):>3} kazandi={won:>3} winrate=%{won / len(comb) * 100:>5.1f} "
        f"yatirilan=${staked:>7.2f} NET=${pnl:>+8.2f} ROI=%{pnl / staked * 100:>+7.1f}"
    )

    if spread:
        ex = defaultdict(int)
        for b in spread:
            ex[b["exit"]] += 1
        print("  spread kapanis dagilimi:", dict(ex))
    return 0


if __name__ == "__main__":
    sys.exit(main())
