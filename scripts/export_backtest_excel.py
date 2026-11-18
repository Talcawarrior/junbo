"""GERCEK backtest'i BET BET Excel'e aktarir.

backtest_true.py'nin simulasyonunu birebir uygular ama her bet'i satir satir
kaydedip .xlsx cikarir. Kolonlar (dashboard formati):
  Gun | Sehir | Tip | Esik | Taraf | Giris | Guncel | Stake | PnL | Sonuc

Kullanim:
    python scripts/export_backtest_excel.py
    python scripts/export_backtest_excel.py --max-entry 0.30 --spread 3
    python scripts/export_backtest_excel.py --min-date 2026-08-07
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3  # noqa: E402

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402

from utils.market_outcome import parse_resolved_outcome  # noqa: E402

BOT_DB = get_db_path()

STAKE = 2.0
FEE_RATE = 0.05
GAS_USD = 0.10
SPREAD = 3
MAX_ENTRY = 0.95
BIAS_TOP = 15


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="GERCEK backtest'i bet bet Excel'e yaz")
    parser.add_argument("--spread", type=int, default=SPREAD)
    parser.add_argument("--max-entry", type=float, default=MAX_ENTRY)
    parser.add_argument("--stake", type=float, default=STAKE)
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE)
    parser.add_argument("--gas-usd", type=float, default=GAS_USD)
    parser.add_argument("--min-date", default="2026-08-01")
    parser.add_argument("--bias-top", type=int, default=BIAS_TOP)
    parser.add_argument("--out", default=os.path.join(_REPO_ROOT, "reports", "backtest_dashboard.xlsx"))
    args = parser.parse_args()

    db = sqlite3.connect(BOT_DB)
    cur = db.cursor()

    cur.execute("SELECT DISTINCT city, city_code FROM weather_markets WHERE city_code IS NOT NULL AND city_code != ''")
    code_name = {}
    for c, code in cur.fetchall():
        if code and c:
            code_name.setdefault(code, c)

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

    markets = {}
    for r in cur.execute(
        "SELECT city_code, metric, threshold, target_date, raw_data "
        "FROM weather_markets WHERE status='expired' AND raw_data IS NOT NULL"
    ).fetchall():
        code, metric, thr, tdate, raw = r
        day = str(tdate)[:10] if tdate else None
        if not day or not code or thr is None or "max" not in (metric or ""):
            continue
        if day < args.min_date:
            continue
        outcome = parse_resolved_outcome(raw)
        if outcome is None:
            continue
        markets[(code, day, metric, float(thr))] = outcome

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

    rows = []  # (day, city, thr, entry, won, pnl)
    skipped = 0
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
            key = (code, day, "temperature_max", float(thr))
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
            if outcome:
                gain = (args.stake / entry) - cost
                won = True
            else:
                gain = -cost
                won = False
            rows.append((day, city, float(thr), entry, won, gain))

    total = len(rows)
    won_n = sum(1 for r in rows if r[4])
    pnl_total = sum(r[5] for r in rows)
    print(
        f"bet={total} won={won_n} lost={total - won_n} skipped={skipped} "
        f"win-rate={won_n / max(total, 1) * 100:.1f}% TOTAL PnL=${pnl_total:.2f}"
    )

    by_day = defaultdict(list)
    for r in rows:
        by_day[r[0]].append(r)

    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF")
    won_fill = PatternFill("solid", fgColor="C6EFCE")
    lost_fill = PatternFill("solid", fgColor="FFC7CE")
    cols = ["Gun", "Sehir", "Tip", "Esik (C)", "Taraf", "Giris ($)", "Guncel", "Stake ($)", "PnL ($)", "Sonuc"]

    ws0 = wb.create_sheet("OZET")
    ws0.append(["Gun", "Bet", "KAZANDI", "kaybetti", "Toplam PnL ($)"])
    for c in range(1, 6):
        ws0.cell(1, c).fill = header_fill
        ws0.cell(1, c).font = header_font
    for day in sorted(by_day):
        b = by_day[day]
        ws0.append(
            [day, len(b), sum(1 for r in b if r[4]), sum(1 for r in b if not r[4]), round(sum(r[5] for r in b), 2)]
        )
    ws0.column_dimensions["A"].width = 12
    for c in "BCDE":
        ws0.column_dimensions[c].width = 14

    for day in sorted(by_day):
        bets = by_day[day]
        ws = wb.create_sheet(day.replace("-", ""))
        ws.append(cols)
        for c in range(1, len(cols) + 1):
            ws.cell(1, c).fill = header_fill
            ws.cell(1, c).font = header_font
        for d, city, thr, entry, won, pnl in sorted(bets, key=lambda x: x[2]):
            guncel = "1.00" if won else "0.00"
            sonuc = f"KAZANDI +${pnl:.2f}" if won else f"kaybetti -${abs(pnl):.2f}"
            ws.append([d, city, "H", thr, "YES", round(entry, 4), guncel, args.stake, round(pnl, 2), sonuc])
            fill = won_fill if won else lost_fill
            for c in range(1, len(cols) + 1):
                ws.cell(ws.max_row, c).fill = fill
        widths = {"A": 12, "B": 16, "C": 5, "D": 10, "E": 6, "F": 10, "G": 9, "H": 10, "I": 10, "J": 20}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    wb.save(args.out)
    print(f"yazildi: {args.out}")
    print("gunler:", ", ".join(sorted(by_day)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
