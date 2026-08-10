"""Early-entry + spread backtest on real snapshot data.

Strategy (best config found on 2026-08-06..10 data):
  - Take the earliest per-model weather forecast for each (city, target_date,
    metric) — the 2-day-ahead prediction known at market open.
  - Open YES bets on the ``spread`` integer thresholds around the rounded
    ensemble forecast (forecast +/- spread) at the market's FIRST-SEEN
    snapshot price, but only when that price is below ``max_entry``.
  - Resolve each bet against Archive actuals (actuals.db): a max-market YES
    wins when actual_max >= threshold, a min-market YES when actual_min
    <= threshold. PnL = (1-entry)/entry * stake on a win, -stake on a loss.

Default parameters reproduce the best observed config:
  spread=3, max_entry=0.30, calibration=off (RAW forecasts outperformed the
  calibrated forecasts in the spread strategy).

Usage:
    python scripts/backtest_early_spread.py                    # defaults
    python scripts/backtest_early_spread.py --spread 1 --max-entry 0.10
    python scripts/backtest_early_spread.py --calibrated        # enable MBE
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

STAKE = 2.0

BOT_DB = os.path.join(_REPO_ROOT, "data", "bot.db")
ACTUALS_DB = os.path.join(_REPO_ROOT, "data", "actuals.db")


def main() -> int:
    parser = argparse.ArgumentParser(description="Early-entry + spread backtest")
    parser.add_argument("--spread", type=int, default=3, help="thresholds +/- around forecast")
    parser.add_argument("--max-entry", type=float, default=0.30, help="skip strikes priced >= this")
    parser.add_argument("--calibrated", action="store_true", help="apply per-city/model MBE correction")
    parser.add_argument("--min-bets", type=int, default=0, help="only print days with >= this many bets")
    args = parser.parse_args()

    from utils.calibration import _get_calibration

    ce = _get_calibration() if args.calibrated else None
    if args.calibrated:
        print(f"calibration engine: {len(ce.bias_map) if ce else 0} cities loaded")

    adb = sqlite3.connect(ACTUALS_DB)
    actuals = {}
    for r in adb.execute("SELECT city, date, temperature_2m_max, temperature_2m_min FROM actual_temperatures"):
        actuals[(r[0], r[1])] = (r[2], r[3])
    adb.close()

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    # city_code -> city display name
    cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''")
    code_name = {}
    for c, code in cur.fetchall():
        if code and c:
            code_name.setdefault(code, c)

    # earliest per-model forecast per (city_code, target_date, metric)
    cur.execute(
        """
        SELECT wf.city, wf.target_date, wf.metric, wf.source, wf.predicted_value
        FROM weather_forecasts wf WHERE wf.predicted_value IS NOT NULL
        ORDER BY wf.fetched_at ASC
        """
    )
    forecasts: dict[tuple, dict] = {}
    for code, tdate, metric, source, pval in cur.fetchall():
        td = str(tdate)[:10] if tdate else None
        if not td or not code or not metric or not source:
            continue
        key = (code, td, metric)
        forecasts.setdefault(key, {})
        if source not in forecasts[key]:
            forecasts[key][source] = float(pval)

    # first-seen snapshot price per strike (market-open price)
    cur.execute(
        """
        SELECT city, metric, target_date, threshold, snapshot_time, yes_price
        FROM market_snapshots ORDER BY snapshot_time ASC
        """
    )
    strike_first = {}
    for city, metric, tdate, thr, stime, yp in cur.fetchall():
        td = str(tdate)[:10] if tdate else None
        if not td or thr is None:
            continue
        key = (city, td, metric, float(thr))
        if key not in strike_first:
            try:
                p = float(yp)
                if 0 < p < 1:
                    strike_first[key] = p
            except (TypeError, ValueError):
                continue

    # ---------------- simulation ----------------
    pnl_total = 0.0
    total = won = lost = unresolved = 0
    per_day = defaultdict(lambda: {"bets": 0, "win": 0, "loss": 0, "pnl": 0.0})
    per_city = defaultdict(lambda: {"bets": 0, "win": 0, "loss": 0, "pnl": 0.0})

    for (code, td, metric), per_model in forecasts.items():
        city_name = code_name.get(code)
        if not city_name:
            continue
        is_min = "min" in (metric or "")
        act = actuals.get((city_name, td))
        if act is None:
            unresolved += 1
            continue
        actual = act[1] if is_min else act[0]
        if actual is None:
            unresolved += 1
            continue

        # ensemble forecast (optionally calibrated per model)
        vals = []
        for model, raw in per_model.items():
            if ce is not None:
                vals.append(ce.get_calibrated_temperature(code, metric, model, raw))
            else:
                vals.append(raw)
        fval = sum(vals) / len(vals) if vals else None
        if fval is None:
            continue

        center = round(fval)
        for offset in range(-args.spread, args.spread + 1):
            thr = center + offset
            entry = strike_first.get((city_name, td, metric, thr))
            if entry is None or not (0 < entry < args.max_entry):
                continue
            hit = (actual >= thr) if not is_min else (actual <= thr)
            total += 1
            gain = (1.0 - entry) * STAKE / entry if hit and entry > 0 else -STAKE
            pnl_total += gain
            per_day[td]["bets"] += 1
            per_city[city_name]["bets"] += 1
            if hit:
                won += 1
                per_day[td]["win"] += 1
                per_city[city_name]["win"] += 1
            else:
                lost += 1
                per_day[td]["loss"] += 1
                per_city[city_name]["loss"] += 1
            per_day[td]["pnl"] += gain
            per_city[city_name]["pnl"] += gain

    db.close()

    # ---------------- report ----------------
    label = f"spread={args.spread} max_entry<{args.max_entry} {'CALIB' if args.calibrated else 'RAW'}"
    print(f"\n=== BACKTEST: early-entry + spread ({label}) ===")
    print(f"bets={total} win={won} loss={lost} win-rate={won / max(total, 1) * 100:.1f}% unresolved={unresolved}")
    print(f"TOTAL PnL (${STAKE}/bet): ${pnl_total:.2f}  avg=${pnl_total / max(total, 1):.3f}/bet")

    print("\nper-day:")
    for d in sorted(per_day):
        pd = per_day[d]
        if pd["bets"] < args.min_bets:
            continue
        wr = pd["win"] / pd["bets"] * 100 if pd["bets"] else 0
        print(
            f"  {d}: bets={pd['bets']:>3} win={pd['win']:>3} loss={pd['loss']:>3} wr={wr:4.1f}% PnL=${pd['pnl']:9.2f}"
        )

    print("\nper-city (top 10 by PnL):")
    for c in sorted(per_city, key=lambda c: -per_city[c]["pnl"])[:10]:
        pc = per_city[c]
        wr = pc["win"] / pc["bets"] * 100 if pc["bets"] else 0
        print(f"  {c:<16} bets={pc['bets']:>3} wr={wr:4.1f}% PnL=${pc['pnl']:8.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
