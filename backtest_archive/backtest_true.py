"""GERCEK backtest — bot'un yaptigi gibi, GERCEK Polymarket sonucuyla.

Kullanici istegi (2026-08-12): meteo tahminini al, market acilisinda ILK
snapshot fiyatindan spread ac (center +/- spread), settlement'a kadar tut,
GERCEK sonuca gore kazan/kaybet.

Fark (diger backtest'lerden):
- outcome: actuals.db DEGIL, `utils.market_outcome.parse_resolved_outcome`
  (Polymarket resmi cozumu, raw_data.outcomePrices — JSON string bug'i cozuldu)
- fiyat: ILK snapshot fiyati (market acilisindaki dusuk giris)
- maliyet: stake + taker fee + gas dahil

Kullanim:
    python scripts/backtest_true.py
    python scripts/backtest_true.py --spread 3 --max-entry 0.95 --stake 2.0
    python scripts/backtest_true.py --min-date 2026-08-07
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

from utils.market_outcome import parse_resolved_outcome  # noqa: E402

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")

STAKE = 2.0
FEE_RATE = 0.05
GAS_USD = 0.10
SPREAD = 3
MAX_ENTRY = 0.95


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="GERCEK spread backtest")
    parser.add_argument("--spread", type=int, default=SPREAD, help="center +/- derece")
    parser.add_argument("--max-entry", type=float, default=MAX_ENTRY, help="ust fiyat siniri (0 = sinirsiz)")
    parser.add_argument("--stake", type=float, default=STAKE, help="bet basina stake (USD)")
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE, help="Polymarket taker fee")
    parser.add_argument("--gas-usd", type=float, default=GAS_USD, help="islem basina gas (USD)")
    parser.add_argument("--min-date", default="2026-08-01", help="bu tarihten itibaren (dahil)")
    parser.add_argument("--bias-top", type=int, default=15, help=">0 ise en az sapan ilk N sehir")
    parser.add_argument("--only-resolved", action="store_true", help="yalnizca resolved marketler (varsayilan)")
    args = parser.parse_args()

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # city_code -> city
    cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''")
    code_name = {}
    for c, code in cur.fetchall():
        if code and c:
            code_name.setdefault(code, c)

    # en az sapan sehirler (bias) — secimli
    keep = None
    if args.bias_top > 0:
        bias_sums: dict[str, float] = {}
        bias_cnt: dict[str, int] = {}
        for code, bias in cur.execute(
            "SELECT city_code, bias FROM historical_calibrations WHERE bias IS NOT NULL AND city_code IS NOT NULL"
        ).fetchall():
            cname = code_name.get(code)
            if not cname:
                continue
            bias_sums[cname] = bias_sums.get(cname, 0.0) + abs(float(bias))
            bias_cnt[cname] = bias_cnt.get(cname, 0) + 1
        city_bias = {c: bias_sums[c] / bias_cnt[c] for c in bias_sums if bias_cnt[c] > 0}
        keep = {c for c, _ in sorted(city_bias.items(), key=lambda kv: kv[1])[: args.bias_top]}

    # resolved marketler + GERCEK outcome (raw_data)
    markets = {}  # (code, day, metric, thr) -> yes_won
    for r in cur.execute(
        "SELECT city_code, metric, threshold, target_date, raw_data "
        "FROM weather_markets WHERE status='expired' AND raw_data IS NOT NULL"
    ).fetchall():
        code, metric, thr, tdate, raw = r
        day = str(tdate)[:10] if tdate else None
        if not day or not code or thr is None or 'max' not in (metric or ''):
            continue
        if day < args.min_date:
            continue
        outcome = parse_resolved_outcome(raw)
        if outcome is None:
            continue
        markets[(code, day, metric, float(thr))] = outcome

    # ILK snapshot fiyati per (code, day, metric, thr)
    snap = {}
    for r in cur.execute(
        "SELECT wm.city_code, wm.metric, wm.threshold, wm.target_date, s.yes_price, s.snapshot_time "
        "FROM market_snapshots s JOIN weather_markets wm ON s.market_id=wm.id "
        "WHERE wm.status='expired' ORDER BY s.snapshot_time ASC"
    ).fetchall():
        code, metric, thr, tdate, yp, stime = r
        day = str(tdate)[:10] if tdate else None
        key = (code, day, metric, float(thr))
        if key not in snap and yp is not None:
            try:
                p = float(yp)
                if 0 < p < 1:
                    snap[key] = p
            except (TypeError, ValueError):
                pass

    # forecast merkezi (ilk ensemble per code,day)
    fc = {}
    for r in cur.execute(
        "SELECT city, target_date, source, predicted_value, fetched_at "
        "FROM weather_forecasts WHERE predicted_value IS NOT NULL AND metric LIKE '%max%' "
        "ORDER BY fetched_at ASC"
    ).fetchall():
        code, tdate, src, pv, ft = r
        day = str(tdate)[:10] if tdate else None
        if not day or day < args.min_date:
            continue
        key = (code, day)
        fc.setdefault(key, {})
        if src not in fc[key]:
            fc[key][src] = float(pv)

    db.close()

    # ---------------- simulasyon ----------------
    pnl_total = 0.0
    total = won = lost = skipped = 0
    per_day = defaultdict(lambda: {"bet": 0, "won": 0, "lost": 0, "pnl": 0.0})
    per_city = defaultdict(lambda: {"bet": 0, "won": 0, "lost": 0, "pnl": 0.0})

    for (code, day), models in fc.items():
        city = code_name.get(code)
        if not city:
            continue
        if keep is not None and city not in keep:
            continue
        vals = list(models.values())
        if not vals:
            continue
        center = round(sum(vals) / len(vals))

        for thr in range(center - args.spread, center + args.spread + 1):
            key = (code, day, 'temperature_max', float(thr))
            outcome = markets.get(key)
            if outcome is None:
                skipped += 1
                continue
            entry = snap.get(key)
            if entry is None:
                skipped += 1
                continue
            if args.max_entry > 0 and not (0 < entry < args.max_entry):
                skipped += 1
                continue

            fee = args.stake * args.fee_rate * (1.0 - entry) if args.fee_rate > 0 else 0.0
            cost = args.stake + fee + args.gas_usd
            if outcome:  # YES kazandi
                gain = (args.stake / entry) - cost
                won += 1
                per_day[day]['won'] += 1
                per_city[city]['won'] += 1
            else:
                gain = -cost
                lost += 1
                per_day[day]['lost'] += 1
                per_city[city]['lost'] += 1
            total += 1
            pnl_total += gain
            per_day[day]['bet'] += 1
            per_day[day]['pnl'] += gain
            per_city[city]['bet'] += 1
            per_city[city]['pnl'] += gain

    # ---------------- rapor ----------------
    wr = won / max(total, 1) * 100
    bias_s = f" bias-top={args.bias_top}" if keep is not None else ""
    print(f"\n=== GERCEK BACKTEST (meteo merkez +/- {args.spread}, ilk fiyat, GERCEK outcome) ===")
    print(f"max_entry<{args.max_entry} stake=${args.stake} fee={args.fee_rate} gas=${args.gas_usd}{bias_s}")
    print(f"bet={total} won={won} lost={lost} skipped(veri yok)={skipped} win-rate={wr:.1f}%")
    print(f"TOTAL PnL: ${pnl_total:.2f}  (avg ${pnl_total / max(total, 1):.2f}/bet)")

    print("\nGUN GUN:")
    for day in sorted(per_day):
        d = per_day[day]
        if d['bet'] == 0:
            continue
        dwr = d['won'] / max(d['bet'], 1) * 100
        print(f"  {day}: bet={d['bet']:>3} won={d['won']:>3} lost={d['lost']:>3} "
              f"wr={dwr:>5.1f}% PnL=${d['pnl']:>9.2f}")

    print("\nSEHIR (top 10 PnL):")
    for c in sorted(per_city, key=lambda c: -per_city[c]['pnl'])[:10]:
        pc = per_city[c]
        cwr = pc['won'] / max(pc['bet'], 1) * 100
        print(f"  {c:<16} bet={pc['bet']:>3} wr={cwr:>5.1f}% PnL=${pc['pnl']:>8.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
