"""METAR bucket vs GERCEK Polymarket kapanisi testi (2026-08-18).

Kullanici sorusu: "METAR sonuclari WU'dan aliyor, Polymarket da WU'dan aliyor,
neden tutmuyor?"

Test:
  1. metar_observations arsivinden her (city_code, day) icin METAR max -> bucket = round(max)
  2. weather_markets.raw_data -> parse_resolved_outcome ile GERCEK kazanan bucket
  3. historical_calibrations.actual_value (Open-Meteo Archive gercek) -> bucket
  4. Karsilastir: METAR-bucket vs kazanan-bucket (METAR dogrulugu),
     actual-bucket vs kazanan-bucket (Open-Meteo dogrulugu), METAR vs actual (istasyon farki)

Ayrica: kazanan bucket = round(gercek max) kurali gercekten geciyor mu
(METAR max'in 2 derece uzeri/esik sinirindaki esler)?

Kullanim: python scripts/test_metar_vs_settlement.py [--min-day 2026-08-15] [--max-day 2026-08-17]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-day", default="2026-08-13")
    ap.add_argument("--max-day", default="2026-08-17")
    args = ap.parse_args()

    db = sqlite3.connect(BOT_DB, timeout=20)
    db.execute("PRAGMA busy_timeout=20000")
    cur = db.cursor()

    # 1) METAR arsivi: (city_code, day) -> max temp
    metar_max: dict[tuple[str, str], float] = {}
    try:
        for code, day, tmax in cur.execute(
            "SELECT city_code, day, MAX(temp_c) FROM metar_observations "
            "WHERE city_code IS NOT NULL AND day IS NOT NULL "
            "AND day >= ? AND day <= ? GROUP BY city_code, day",
            (args.min_day, args.max_day),
        ):
            if code and tmax is not None:
                metar_max[(code, day)] = float(tmax)
    except sqlite3.OperationalError as exc:
        print(f"metar_observations okunamadi: {exc}")
        return 1

    # 2) Gercek cozum: (city_code, day) -> kazanan bucket.
    #    SADECE temperature_max + RANGE (tam bucket) marketlerinden — botun
    #    hedefi bu. Lowest-temp veya HIGH/LOW marketleri karistirilmaz
    #    (2026-08-18 bugfix: HK 08-15 'kazanan 26' aslinda lowest-temp'ti,
    #    highest METAR 33 ile karismis -> yanlis fark +7C raporlaniyordu).
    code_name: dict[str, str] = {}
    winner: dict[tuple[str, str], int] = {}
    for city, code, thr, day, raw, mtype, metric in cur.execute(
        "SELECT city, city_code, threshold, target_date, raw_data, market_type, metric "
        "FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND raw_data IS NOT NULL"
    ):
        if mtype != "RANGE" or metric != "temperature_max":
            continue
        d = str(day)[:10]
        if not (args.min_day <= d <= args.max_day):
            continue
        if code:
            code_name.setdefault(code, city)
        o = parse_resolved_outcome(raw)
        if o is None:
            continue
        try:
            t = int(thr)
        except (TypeError, ValueError):
            continue
        if o is True and (winner.get((code, d)) is None or winner.get((code, d), -999) < t):
            winner[(code, d)] = t

    # 3) Open-Meteo actual: (city_code, day) -> gercek max (kac modelden olursa, son deger)
    actual_val: dict[tuple[str, str], float] = {}
    for code, day, av in cur.execute(
        "SELECT city_code, date, actual_value FROM historical_calibrations "
        "WHERE metric='temperature_max' AND actual_value IS NOT NULL"
    ):
        d = str(day)[:10]
        if code and args.min_day <= d <= args.max_day:
            actual_val[(code, d)] = float(av)
    db.close()

    print("=== METAR bucket vs GERCEK Polymarket kapanisi ===")
    print(
        f"  pencere: {args.min_day} .. {args.max_day} | metar_cities={len(metar_max)} "
        f"resolved_cities={len(winner)} actual_cities={len(actual_val)}"
    )
    print(f"{'city':12s} {'day':10s} {'METARmax':>8s} {'METARbkt':>7s} {'WIN':>3s} {'ACT':>5s} {'tutuyor':>7s}")

    metar_ok = act_ok = n = 0
    metar_vs_act_diff: list[tuple[float | None, float | None]] = []
    mismatches = []
    for code, day in sorted(winner):
        city = code_name.get(code, code)
        w = winner[(code, day)]
        mb = None
        if (code, day) in metar_max:
            mb = int(round(metar_max[(code, day)]))
        ab = None
        if (code, day) in actual_val:
            ab = int(round(actual_val[(code, day)]))
            mx = metar_max[(code, day)] if (code, day) in metar_max else None
            metar_vs_act_diff.append((mx, actual_val[(code, day)]))
        hit = "?" if mb is None else ("TUTUYOR" if mb == w else "X")
        if mb is not None:
            n += 1
            if mb == w:
                metar_ok += 1
            else:
                mismatches.append((city, day, metar_max[(code, day)], mb, w))
        if ab is not None:
            act_ok += int(ab == w)
        row = f"{city:12s} {day:10s}"
        row += f" {metar_max.get((code, day), float('nan')):8.1f}" if (code, day) in metar_max else f" {'-':>8s}"
        row += f" {mb if mb is not None else '-':>7d}" if mb is not None else f" {'-':>7s}"
        row += f" {w:>3d}"
        row += f" {ab if ab is not None else '-':>5d}" if ab is not None else f" {'-':>5s}"
        row += f" {hit:>7s}"
        print(row)

    print()
    if n:
        print(
            f"METAR bucket dogrulugu (esitlik): {metar_ok}/{n} = %{metar_ok / max(n, 1) * 100:.0f} "
            f"(round(METAR max) == Polymarket kazanan bucket)"
        )
    na = len(actual_val)
    print(
        f"Open-Meteo actual dogrulugu (esitlik): {act_ok}/{na} = %{act_ok / max(na, 1) * 100:.0f} "
        f"(round(actual) == kazanan bucket)"
    )

    # IKI MARKET TIPI (kullanici 2026-08-18 duzeltmesi):
    #   RANGE (tam bucket, "exactly 32C"): YES sadece round(gercek max) == b ise.
    #     -> winrate = esitlik (yukaridaki metar_ok = %68).
    #   HIGH (or-above, ">= 32C"): YES gercek max >= b ise (b <= kazanan bucket).
    #     -> winrate = b <= w (daha yuksek).
    # Bot su an TUM market tiplerine bet aciyor (21 RANGE + 3 HIGH + 2 LOW + 4 min).
    print("\n--- IKI MARKET TIPI ICIN AYRI WINRATE ---")
    print(
        f"  RANGE (tam bucket 'exactly bC'): %{metar_ok / max(n, 1) * 100:.0f} ({metar_ok}/{n})  <- botun ana bet tipi"
    )
    bets = wins = 0
    loss_list = []
    for (code, day), w in winner.items():
        mx = metar_max.get((code, day))
        if mx is None:
            continue
        b = int(round(mx))
        bets += 1
        if b <= w:
            wins += 1
        else:
            loss_list.append((code_name.get(code, code), day, mx, b, w, b - w))
    print(f"  HIGH  (or-above '>= bC'): %{wins / max(bets, 1) * 100:.0f} ({wins}/{bets})  <- daha kolay kazanan tip")
    print("\n  --- HIGH icin kayiplar (METAR abartti: b > kazanan) ---")
    for city, day, mx, b, w, over in sorted(loss_list, key=lambda x: -x[5]):
        print(f"    {city:12s} {day}  METARmax={mx:5.1f}->{b}C  KAZANAN={w}C  abartma={over}C")

    # METAR vs actual fark dagilimi (istasyon uyusmazligi)
    pairs = [d for d in metar_vs_act_diff if d[0] is not None and d[1] is not None]
    if pairs:
        diffs: list[float] = []
        for a, b in pairs:
            if a is not None and b is not None:
                diffs.append(round(float(a) - float(b), 1))
        over = sum(1 for x in diffs if x >= 1.5)
        under = sum(1 for x in diffs if x <= -1.5)
        avg = sum(diffs) / len(diffs) if diffs else 0.0
        print(
            f"METAR max vs Open-Meteo actual fark: n={len(diffs)} ort={avg:+.1f}C "
            f"(METAR>=actual+1.5C: {over}, METAR<=actual-1.5C: {under})"
        )

    if mismatches:
        print("\n--- UYUSMAYANLAR (METAR yanlis bucket dedi) ---")
        for city, day, mx, mb, w in mismatches:
            print(f"  {city:12s} {day}  METARmax={mx:.1f}->bucket{mb}  KAZANAN={w}  (fark {mx - (w):+.1f}C)")

    # Cozum kurali dogrulamasi: kazanan bucket == round(gercek max)?
    print("\n--- Cozum kurali: kazanan bucket == round(actual max)? ---")
    rule_n = rule_ok = 0
    for (code, day), w in winner.items():
        av = actual_val.get((code, day))
        if av is None:
            continue
        rule_n += 1
        if int(round(av)) == w:
            rule_ok += 1
    print(f"  round(actual)==kazanan: {rule_ok}/{rule_n} = %{rule_ok / max(rule_n, 1) * 100:.0f}")

    # PnL tahmini: RANGE (tam bucket) marketi, orderbook giris fiyati ile.
    # Strateji: METAR max -> b=round(max) -> (code, day, b) RANGE marketine bet.
    # Giris = kapanisa HOURS_BEFORE saat kala orderbook best_ask. Kazanirsa
    # stake/entry doner (win = b == kazanan bucket).
    print("\n--- RANGE (tam bucket) PnL tahmini (orderbook giris, son 3 gun) ---")
    _pnl_hours = 6
    _stake = 3.0
    _fee = 0.05
    _gas = 0.10

    def _ts(s):
        s = str(s).replace("T", " ").replace("+00:00", "").strip()
        try:
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    ob_series = None
    try:
        import sqlite3 as _sq

        ob = _sq.connect(os.path.join(_REPO_ROOT, "data", "orderbook.db"), timeout=15)
        ob.execute("PRAGMA busy_timeout=15000")
        ob_series = defaultdict(list)
        for mid, ask, st in ob.execute(
            "SELECT market_id, best_ask, snapshot_time FROM orderbook_snapshots WHERE best_ask IS NOT NULL"
        ):
            t = _ts(st)
            if t is None:
                continue
            try:
                a = float(ask)
                if 0 < a <= 1:
                    ob_series[str(mid)].append((t, a))
            except (TypeError, ValueError):
                pass
        for k in ob_series:
            ob_series[k].sort(key=lambda x: x[0])
        ob.close()
    except Exception as exc:
        print(f"  (orderbook okunamadi: {exc})")

    market_range = {}  # (code, day, thr) -> (mid, target_ts)
    _db2 = sqlite3.connect(BOT_DB, timeout=20)
    for r in _db2.execute(
        "SELECT id, city_code, threshold, target_date, market_type, metric FROM weather_markets "
        "WHERE threshold IS NOT NULL AND target_date IS NOT NULL AND market_type='RANGE' AND metric='temperature_max'"
    ):
        mid, code, thr, tdate = r[0], r[1], r[2], r[3]
        d = str(tdate)[:10]
        if code and args.min_day <= d <= args.max_day:
            t = _ts(tdate)
            if t is not None:
                market_range[(code, d, int(thr))] = (str(mid), t)
    _db2.close()

    def _price_at(series, bet_ts, window_sec=8 * 3600):
        best = None
        if not series:
            return None
        for t, a in series:
            if t > bet_ts:
                break
            if bet_ts - t <= window_sec:
                best = a
        return best

    if ob_series is not None:
        pnl_bets = []
        for (code, day), mx in metar_max.items():
            if winner.get((code, day)) is None:
                continue
            b = int(round(mx))
            w = winner[(code, day)]
            m = market_range.get((code, day, b))
            if m is None:
                continue
            mid, tgt = m
            if tgt is None:
                continue
            bet_ts = tgt + (12 - _pnl_hours) * 3600
            entry = _price_at(ob_series.get(mid), bet_ts)
            if entry is None or not (0.01 <= entry < 0.95):
                continue
            fee = _stake * _fee * (1.0 - entry)
            cost = _stake + fee + _gas
            win = b == w
            gain = (_stake / entry - cost) if win else -cost
            pnl_bets.append((code_name.get(code, code), day, b, entry, win, gain))
        n_b = len(pnl_bets)
        wins = sum(1 for p in pnl_bets if p[4])
        net = sum(p[5] for p in pnl_bets)
        stk = n_b * _stake
        print(
            f"  bet={n_b}  win=%{wins / max(n_b, 1) * 100:.0f}  "
            f"NET ${net:+.2f}  ROI %{net / max(stk, 1) * 100:.1f}  (stake=${_stake:g}, kapanisa {_pnl_hours}h kala)"
        )
        print("  bet detay (ilk 15):")
        for city, day, b, entry, win, gain in pnl_bets[:15]:
            print(f"    {city:12s} {day}  bkt={b:3d}  entry={entry:6.3f}  {'WIN' if win else 'LOST'}  ${gain:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
