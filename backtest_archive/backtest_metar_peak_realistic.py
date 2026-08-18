"""METAR-peak GERCEKCI backtest (2026-08-17): bucket tahmini = round(actual).

Neden: backtest_metar_peak.py 'winrate %100 / ROI %286' veriyor - cunku kazanan
bucket'i Polymarket'in GERCEK cozumunden aliyor (clairvoyant/look-ahead) ve
`gain = ... if True else -cost` (line 166) her beti win sayiyor. Bu UST SINIRDIR.

Gercek METAR-peak: bucket tahmini CANLI METAR/actual'dan yapilir, %71 dogru
(16-Agu 42 sehirden 30). Burada tahmin = round(actuals bucket) (METAR proxy'si),
cozum = Polymarket gercek cozumu. Yani: tahmin tutarsa YES win, tutmazsa o
market NO oldu -> tam kayip.

Kullanim:
    python scripts/backtest_metar_peak_realistic.py [--hours-before 6] [--stake 3.0]
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
FEE_RATE = 0.05
GAS = 0.10
MAX_ENTRY = 0.95
CLOSE_HOURS = 12


def ts(s):
    s = str(s).replace("T", " ").replace("+00:00", "").strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _load_orderbook(ob_path: str):
    """market_id -> [(t, ask)] - kilitliyse kopya al (bot calisirken guvenli)."""
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


def price_at(series, bet_ts, window_sec=6 * 3600):
    """bet_ts oncesi, window icindeki son ask. Yoksa None."""
    best = None
    for t, a in series:
        if t > bet_ts:
            break
        if bet_ts - t <= window_sec:
            best = a
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description="METAR-peak gercekci backtest")
    parser.add_argument("--hours-before", type=int, default=6)
    parser.add_argument("--stake", type=float, default=3.0)
    parser.add_argument("--min-day", default="2026-08-05")
    parser.add_argument("--max-day", default="2026-08-16")
    args = parser.parse_args()
    stake = args.stake

    ask_series = _load_orderbook(OB_DB)

    db = sqlite3.connect(BOT_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    code_name = {}
    for c, code in db.execute(
        "SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''"
    ):
        if code and c:
            code_name.setdefault(code, c)

    # (code, day, thr) -> (mid, target_ts, outcome)
    market = {}
    for r in db.execute(
        "SELECT id, city_code, threshold, target_date, raw_data FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
    ):
        code, thr, day = r[1], float(r[2]) if r[2] is not None else None, str(r[3])[:10]
        if thr is None or not code:
            continue
        if not (args.min_day <= day <= args.max_day):
            continue
        o = parse_resolved_outcome(r[4])
        t = ts(r[3])
        market[(code, day, int(thr))] = (str(r[0]), t, o)

    # Gercek cozumden kazanan bucket: (code, day) -> thr (YES cozulen en yuksek esik)
    winner = {}
    for (code, day, thr), (_, _, o) in market.items():
        if o is True:
            cur = winner.get((code, day))
            if cur is None or thr > cur:
                winner[(code, day)] = thr

    # Gercek max sicaklik (Open-Meteo archive / historical_calibrations)
    actuals = {}
    for r in db.execute(
        "SELECT city_code, date, actual_value FROM historical_calibrations "
        "WHERE metric='temperature_max' AND actual_value IS NOT NULL"
    ):
        code, day = r[0], str(r[1])[:10]
        if code:
            actuals.setdefault((code, day), float(r[2]))
    db.close()

    # AYNI evrende iki mod:
    #   "actual" -> bucket = round(actual)   (METAR detektoru proxy'si, ~%30 dogru)
    #   "clair"  -> bucket = gercek cozum    (look-ahead UST SINIR, %100 dogru)
    # Gercek METAR detektoru ~%71 dogru oldugu icin (16-Agu: 30/42), gercek PnL
    # bu iki senaryonun ~%71 ile agirlikli ortalamasina yakin olur.
    actual_bet = []
    clair_bet = []
    for (code, day), pred in actuals.items():
        if winner.get((code, day)) is None:
            continue
        w = winner[(code, day)]

        for label, thr in (("actual", int(round(pred))), ("clair", w)):
            m = market.get((code, day, thr))
            if m is None:
                continue
            mid, tgt, o = m
            if tgt is None:
                continue
            bet_ts = tgt + (CLOSE_HOURS - args.hours_before) * 3600
            series = ask_series.get(mid)
            entry = price_at(series, bet_ts) if series else None
            if entry is None or not (0.01 <= entry < MAX_ENTRY):
                continue
            fee = stake * FEE_RATE * (1.0 - entry)
            cost = stake + fee + GAS
            win = (thr == w)
            gain = (stake / entry - cost) if win else -cost
            bucket = (code, day)
            if label == "actual":
                actual_bet.append((bucket, entry, win, gain))
            else:
                clair_bet.append((bucket, entry, win, gain))

    def summarize(bets):
        n = len(bets)
        wins = sum(1 for b in bets if b[2])
        pnl = sum(b[3] for b in bets)
        return n, wins, pnl

    def print_mode(name, bets):
        n, wins, pnl = summarize(bets)
        stk = n * stake
        print(f"  [{name}] bet={n}, winrate=%{wins/max(n,1)*100:.1f}, "
              f"NET ${pnl:+.2f}, ROI %{pnl/max(stk,1)*100:.1f}, ort ${pnl/max(n,1):+.2f}/bet")
        return pnl

    print("=== METAR-PEAK GERCEKCI backtest (ayni evren, iki senaryo) ===")
    print(f"  kapanisa {args.hours_before} saat kala, stake=${stake:g}, "
          f"kandidat (code,day)={len(actuals)}")
    pnl_actual = print_mode("tahmin=round(actual) (~%30 dogru, METAR proxy'si)", actual_bet)
    pnl_clair = print_mode("clairvoyant   (cozumden %100 dogru, UST SINIR)", clair_bet)

    # Gercek detektor ~%71: p*(clair) + (1-p)*(actual) yaklasimi
    p = 0.71
    blend = p * pnl_clair + (1 - p) * pnl_actual
    print(f"  GERCEKCI KARISIM (detektor %{p*100:.0f} dogru): "
          f"0.71*clair + 0.29*actual = ${blend:+.2f} (ayni bet seti uzerinde)")
    print(f"  bucket dogrulugu (round(actual) vs cozum): "
          f"%{sum(1 for b in actual_bet if b[2])/max(len(actual_bet),1)*100:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
