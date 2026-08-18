"""KOMSU-SATISI backtest (2026-08-17): 3 esik + kazanan-tut + komsu-sat.

Kullanici fikri (2026-08-16): T-2'de merkez +-1C (3 esik) dusuk fiyata gir;
peak kilitlenince kazanan bucket belli olur; kazanan disindaki komsu esikler
HEMEN canli fiyattan satilir (millet uyanmadan), kazanan tutulur.

Bu script orderbook best_ask (gercek islem fiyati) ile simule eder:
  - peak   = gercek gunluk max sicaklik (actuals.db temperature_2m_max);
             kazanan bucket = round(peak) — tarihsel METAR saatlik verisi
             olmadigindan peak-kilitlenme aninin CLAIRVOYANT proxy'si.
             (Gercek METAR tespiti ~%71 dogru; bu ust sinirdir.)
  - entry  = marketin target-gunu 06:00 UTC'den ONCE gorulen ilk best_ask
             (T-2 acilis fiyati proxy'si); yoksa en erken ask
  - satis  = kapanisa N saat kala (target_ts + (12-N)*3600) civarindaki son ask
  - karsilastirma:
      HOLD_ALL  -> 3 esik de settlement'a kadar TUTULUR (eski davranis)
      SELL_PEAK -> round(peak) esigi TUTULUR (gercek outcome ile settle),
                   diger 2 komsu peak aninda satilir; round(peak) pencere
                   disindaysa TUM esikler satilir
                   (jobs/metar_peak._close_wrong_bucket_bets davranisi)

Kullanim:
    python scripts/backtest_komsu_satisi.py
    python scripts/backtest_komsu_satisi.py --hours-before 4 --min-day 2026-08-10
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils.market_outcome import parse_resolved_outcome  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
OB_DB = os.path.join(_REPO_ROOT, "data", "orderbook.db")
ACTUALS_DB = os.path.join(_REPO_ROOT, "data", "actuals.db")
STAKE = 2.0          # spread stake (settings.spread_stake_usd)
FEE_RATE = 0.05      # Polymarket fee yaklasimi (mevcut backtest'lerle tutarli)
GAS = 0.10
MAX_ENTRY = 0.95
CLOSE_HOURS = 12     # kapanis = target_date (12:00) + 12h = 24:00 UTC


def ts(s):
    s = str(s).replace("T", " ").replace("+00:00", "").strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _load_orderbook(ob_path: str):
    """orderbook best_ask serisi: market_id -> [(t, ask)] (sirali).

    Canli DB kilitliyse (bot yaziyor) bir kopya alinir — backtest bot
    CALISIYORKEN de guvenli calisir.
    """
    try:
        db = sqlite3.connect(ob_path, timeout=20)
        db.execute("PRAGMA busy_timeout=20000")
        series = defaultdict(list)
        for mid, ask, st in db.execute(
            "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
        ):
            t = ts(st)
            if t is None:
                continue
            try:
                a = float(ask)
                if 0 < a <= 1:
                    series[str(mid)].append((t, a))
            except (TypeError, ValueError):
                pass
        db.close()
        for k in series:
            series[k].sort(key=lambda x: x[0])
        return series
    except sqlite3.OperationalError as exc:
        print(f"[ob] canli okunamadi ({exc}); kopya deneniyor...")
        import shutil
        import tempfile

        snap = os.path.join(tempfile.gettempdir(), "ob_snap.db")
        shutil.copy2(ob_path, snap)
        j = ob_path + "-journal"
        if os.path.exists(j):
            shutil.copy2(j, snap + "-journal")
        return _load_orderbook(snap)


def main() -> int:
    parser = argparse.ArgumentParser(description="Komsu-satisi (3 esik) backtest")
    parser.add_argument("--hours-before", type=int, default=4, help="kapanisa N saat kala komsular satilir")
    parser.add_argument("--min-day", default="2026-08-05", help="backtest baslangic gunu")
    parser.add_argument("--max-day", default="2026-08-20", help="backtest bitis gunu")
    parser.add_argument("--ob-db", default=OB_DB, help="orderbook.db yolu")
    parser.add_argument(
        "--peak-source",
        default="outcome",
        choices=["outcome", "actuals"],
        help="kazanan bucket kaynagi: 'outcome'=clairvoyant ust sinir (gercek "
        "cozumden alinir, look-ahead), 'actuals'=gercekci proxy "
        "(round(Open-Meteo max); NOT: ~%25 bucket dogrulugu — kotu proxy).",
    )
    args = parser.parse_args()

    ask_series = _load_orderbook(args.ob_db)

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    # city -> city_code ; code -> city
    city_code = {}
    code_name = {}
    for c, code in db.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            city_code.setdefault(c, code)
            code_name.setdefault(code, c)

    # (code, day, thr) -> market_id ; (code, day, thr) -> gercek outcome
    market_id = {}
    outcome = {}
    for r in db.execute(
        "SELECT id, city_code, threshold, target_date, raw_data FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
    ):
        code, thr, day = r[1], float(r[2]), str(r[3])[:10]
        if not (args.min_day <= day <= args.max_day):
            continue
        o = parse_resolved_outcome(r[4])
        if o is None:
            continue
        key = (code, day, thr)
        market_id[key] = str(r[0])
        outcome[key] = o

    # hedef (code, day) -> target_ts (12:00 etiketi)
    target_ts = {}
    for r in db.execute(
        "SELECT city_code, target_date FROM weather_markets WHERE target_date IS NOT NULL"
    ):
        code, td = r[0], r[1]
        if not code:
            continue
        day = str(td)[:10]
        t = ts(td)
        if t is not None:
            target_ts.setdefault((code, day), t)

    # (code, day) -> {model: en son predicted_value} — model konsensusu (merkez)
    fc = {}
    for r in db.execute(
        "SELECT city, target_date, source, predicted_value, fetched_at FROM weather_forecasts "
        "WHERE predicted_value IS NOT NULL AND metric LIKE '%max%'"
    ):
        code, day, src, pv = r[0], str(r[1])[:10], r[2], float(r[3])
        if not (args.min_day <= day <= args.max_day):
            continue
        key = (code, day)
        cur = fc.get(key)
        fts = ts(r[4]) or 0
        if cur is None:
            fc[key] = {src: (pv, fts)}
        else:
            old = cur.get(src)
            if old is None or fts >= old[1]:
                cur[src] = (pv, fts)
    db.close()

    # Kazanan bucket: (code, day) -> bucket (int)
    #  outcome: gercek cozumden en yuksek kazanan esik (clairvoyant ust sinir)
    #  actuals: round(Open-Meteo gunluk max) — gercekci proxy (zayif)
    peak = {}
    if args.peak_source == "outcome":
        for (code, day, thr), o in outcome.items():
            if o is True:
                cur = peak.get((code, day))
                if cur is None or thr > cur:
                    peak[(code, day)] = int(thr)
    else:
        if os.path.exists(ACTUALS_DB):
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ad = sqlite3.connect(ACTUALS_DB, timeout=15)
            for cname, d, v in ad.execute(
                "SELECT city, date, MAX(temperature_2m_max) FROM actual_temperatures "
                "WHERE temperature_2m_max IS NOT NULL GROUP BY city, date"
            ):
                code = city_code.get(cname)
                day = str(d)[:10]
                # Bugunun actuals'i yarim gunluk olur -> atla
                if code and args.min_day <= day <= args.max_day and day < today:
                    peak.setdefault((code, day), int(round(float(v))))
            ad.close()
    n_peak = len(peak)
    print("[veri] resolved (city,day,thr)=", len(outcome),
          "orderbook market=", len(ask_series),
          "forecast (city,day)=", len(fc),
          f"peak({args.peak_source}) (city,day)=", n_peak)

    # ---------------- simulasyon ----------------
    def run(hours_before: int):
        stat = {
            "hold": {"pnl": 0.0, "n": 0, "won": 0, "day": defaultdict(float)},
            "sell": {"pnl": 0.0, "n": 0, "won": 0, "day": defaultdict(float)},
        }
        n_cases = 0
        n_win_in = 0
        n_win_out = 0
        n_sold = 0
        n_unsold = 0
        rise = []   # satilan komsu bacaklar: (entry, peak-satis fiyati, degisim orani)
        sell_less_win = 0    # satilan bacak aslinda KAZANAN cikti
        sell_less_win_val = 0.0

        for (code, day), pk in peak.items():
            w = round(pk)          # METAR-peak'in tutacagi bucket (clairvoyant)
            tgt = target_ts.get((code, day))
            models = fc.get((code, day))
            if tgt is None or not models or len(models) < 2:
                continue
            center = round(sum(v for v, _ in models.values()) / len(models))
            legs = [center - 1, center, center + 1]

            # Her bacak: market + outcome + entry (target 06:00'den once ilk ask)
            leg_data = []
            for thr in legs:
                mid = market_id.get((code, day, thr))
                if mid is None:
                    continue
                series = ask_series.get(mid)
                if not series:
                    continue
                pre_cut = tgt - 6 * 3600   # target gunu 06:00 UTC oncesi (T-2)
                entry = None
                for t, a in series:
                    if t <= pre_cut:
                        entry = a
                    else:
                        break
                if entry is None:
                    entry = series[0][1]   # orderbook T-2 kapsamiyorsa en erken ask
                if not (0 < entry < MAX_ENTRY):
                    continue
                leg_data.append({
                    "thr": thr, "mid": mid, "entry": entry, "series": series,
                    "o": outcome.get((code, day, thr)),
                })
            if not leg_data:
                continue
            n_cases += 1

            sell_ts = tgt + (CLOSE_HOURS - hours_before) * 3600

            hold_pnl = 0.0
            sell_pnl = 0.0
            held_won = 0
            for leg in leg_data:
                thr, entry, series = leg["thr"], leg["entry"], leg["series"]
                o = leg["o"]
                fee = STAKE * FEE_RATE * (1.0 - entry)
                cost = STAKE + fee + GAS
                shares = STAKE / entry
                # HOLD: settlement'a kadar tut
                hold_pnl += (shares - cost) if o else -cost
                # SELL: round(peak) bacak TUTULUR, digerleri satilir
                is_winner = (thr == w)
                if is_winner:
                    sell_pnl += (shares - cost) if o else -cost
                    if o:
                        held_won += 1
                else:
                    px = None
                    for t, a in series:
                        if t > sell_ts:
                            break
                        if sell_ts - t <= 12 * 3600:
                            px = a
                    if px is None:
                        n_unsold += 1
                        sell_pnl += -cost          # satilamadi -> tutulmus gibi
                    else:
                        sell_pnl += shares * px - cost
                        n_sold += 1
                        rise.append((entry, px, px / entry - 1.0))
                        if o is True:               # kazanan bacagi satmak kayip mu?
                            sell_less_win += 1
                            sell_less_win_val += (shares - cost) - (shares * px - cost)

            if w in legs:
                n_win_in += 1
            else:
                n_win_out += 1

            stat["hold"]["pnl"] += hold_pnl
            stat["hold"]["n"] += len(leg_data)
            stat["hold"]["won"] += held_won
            stat["hold"]["day"][day] += hold_pnl
            stat["sell"]["pnl"] += sell_pnl
            stat["sell"]["n"] += len(leg_data)
            stat["sell"]["won"] += held_won
            stat["sell"]["day"][day] += sell_pnl

        def summarize(key):
            s = stat[key]
            stake_total = s["n"] * STAKE
            return {
                "pnl": s["pnl"], "n": s["n"], "won": s["won"],
                "roi": s["pnl"] / stake_total * 100 if stake_total else 0.0,
                "winrate": s["won"] / s["n"] * 100 if s["n"] else 0.0,
                "day": dict(s["day"]),
            }

        return {
            "hours_before": hours_before,
            "hold": summarize("hold"),
            "sell": summarize("sell"),
            "n_cases": n_cases, "n_win_in": n_win_in, "n_win_out": n_win_out,
            "n_sold": n_sold, "n_unsold": n_unsold, "rise": rise,
            "sell_less_win": sell_less_win, "sell_less_win_val": sell_less_win_val,
        }

    for hb in [6, 4, 2]:
        r = run(hb)
        print(f"\n=== KOMSU-SATISI (kapanisa {hb} saat kala satis) ===")
        print(f"  {r['n_cases']} sehir-gun; kazanan pencerede {r['n_win_in']}, disinda {r['n_win_out']}")
        print(f"  satilan komsu bacak: {r['n_sold']}, satilamayan: {r['n_unsold']}")
        for key in ["hold", "sell"]:
            s = r[key]
            label = "HOLD_ALL (3 bacak tutulur)" if key == "hold" else "SELL_PEAK (komsular peak'te satilir)"
            print(f"  {label:38s} bet={s['n']:>4} winrate={s['winrate']:>5.1f}% "
                  f"PnL=${s['pnl']:>9.2f} ROI=%{s['roi']:>7.1f}")
        print("  gun bazli fark (sell-hold): " +
              ", ".join(f"{d}:${(r['sell']['day'].get(d,0)-r['hold']['day'].get(d,0)):+.1f}"
                        for d in sorted(set(r['hold']['day']) | set(r['sell']['day']))))
        if r["rise"]:
            avg_rise = sum(x[2] for x in r["rise"]) / len(r["rise"]) * 100
            up = sum(1 for _, _, d in r["rise"] if d > 0.05)
            print(f"  KOMSU FIYAT YUKSELISI (satilan {len(r['rise'])} bacak): "
                  f"ort entry->peak %{avg_rise:+.1f}, %5+ yukselen={up}/{len(r['rise'])}")
        print(f"  SATILAN KAZANAN BACAGI: {r['sell_less_win']} adet, "
              f"kayip fark ${r['sell_less_win_val']:+.2f}")

    # Komsu fiyat analizi (en iyi config: 4 saat)
    print("\n=== KOMSU FIYAT ANALIZI (kapanisa 4 saat kala) ===")
    r = run(4)
    if r["rise"]:
        entries = [e for e, _, _ in r["rise"]]
        sells = [p for _, p, _ in r["rise"]]
        avg_e = sum(entries) / len(entries)
        avg_s = sum(sells) / len(sells)
        up = sum(1 for _, _, d in r["rise"] if d > 0.05)
        print(f"  satilan bacak sayisi: {len(r['rise'])}")
        print(f"  ort entry={avg_e:.3f}, ort peak-satis={avg_s:.3f}, ort degisim=%{avg_s/avg_e*100-100:+.1f}")
        print(f"  %5+ yukselen bacak orani: {up}/{len(r['rise'])} (%{up/len(r['rise'])*100:.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
