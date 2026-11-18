"""Dashboard 'Acik Pozisyonlar' formatinda tum bet'leri EXCEL'e yazar.
Her gun ayri sayfa. Kolonlar:
  Sehir | Tip | Esik | Acilis | Taraf | Giris | Guncel | Stake | Kapanis | Sonuc

Kullanim:
    python scripts/export_bets_excel.py                    # tum gunler -> reports/bets_dashboard.xlsx
    python scripts/export_bets_excel.py --day 2026-08-11   # tek gun
    python scripts/export_bets_excel.py --out dosya.xlsx
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

BOT_DB = get_db_path()


def bet_rows(db, min_date):
    cur = db.cursor()
    return cur.execute(
        "SELECT b.city, b.entry_price, b.status, b.realized_pnl, b.shares, b.amount, "
        "       wm.threshold, wm.target_date, wm.market_type, b.placed_at, wm.yes_price, b.current_price, "
        "       wm.metric "
        "FROM bets b JOIN weather_markets wm ON b.market_id=wm.id "
        "WHERE wm.target_date >= ? "
        "ORDER BY wm.target_date, b.city, wm.threshold",
        (min_date,),
    ).fetchall()


def classify(status, rpnl):
    rpnl = rpnl or 0.0
    if status in ("won", "lost") or (status in ("closed", "closed_early") and rpnl):
        return ("KAZANDI", rpnl) if rpnl > 0 else ("kaybetti", -abs(rpnl))
    return ("ACIK", None)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Tum bet'leri Excel'e aktar")
    parser.add_argument("--min-date", default="2026-08-01")
    parser.add_argument("--day", default=None)
    parser.add_argument("--out", default=os.path.join(_REPO_ROOT, "reports", "bets_dashboard.xlsx"))
    args = parser.parse_args()

    db = sqlite3.connect(BOT_DB)
    rows = bet_rows(db, args.min_date)
    db.close()

    by_day = defaultdict(list)
    for r in rows:
        day = str(r[7])[:10] if r[7] else "?"
        by_day[day].append(r)

    days = [args.day] if args.day else sorted(by_day)

    wb = Workbook()
    wb.remove(wb.active)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF")
    won_fill = PatternFill("solid", fgColor="C6EFCE")
    lost_fill = PatternFill("solid", fgColor="FFC7CE")
    open_fill = PatternFill("solid", fgColor="FFEB9C")

    cols = ["Şehir", "Tip", "Eşik (°C)", "Açılış", "Taraf", "Giriş ($)", "Güncel", "Stake ($)", "Kapanış", "Sonuç"]

    # Ozet sayfasi
    ws0 = wb.create_sheet("OZET")
    ws0.append(["Gün", "Bet", "KAZANDI", "kaybetti", "Açık", "Toplam PnL ($)"])
    for c in range(1, 7):
        ws0.cell(1, c).fill = header_fill
        ws0.cell(1, c).font = header_font
    for day in days:
        bets = by_day[day]
        won = sum(1 for b in bets if classify(b[2], b[3])[0] == "KAZANDI")
        lost = sum(1 for b in bets if classify(b[2], b[3])[0] == "kaybetti")
        open_n = len(bets) - won - lost
        total = sum((b[3] or 0) for b in bets if b[3] is not None)
        ws0.append([day, len(bets), won, lost, open_n, round(total, 2)])
    ws0.column_dimensions["A"].width = 12
    for c in "BCDEF":
        ws0.column_dimensions[c].width = 12

    for day in days:
        bets = by_day[day]
        ws = wb.create_sheet(day.replace("-", ""))
        ws.append(cols)
        for c in range(1, len(cols) + 1):
            ws.cell(1, c).fill = header_fill
            ws.cell(1, c).font = header_font

        for b in sorted(bets, key=lambda x: x[1]):
            city, entry, status, rpnl, shares, amt, thr, tdate, mtype, placed, yp, curp, metric = b
            result, pnl = classify(status, rpnl)
            acilis = str(placed)[5:16] if placed else ""
            kapanis = str(tdate)[:16] if tdate else ""
            giris = round(float(entry), 2) if entry else 0
            guncel = round(float(curp or 0), 2)
            stake = round(float(amt or 0), 2)
            if result == "ACIK":
                sonuc_txt = f"ACIK ({guncel:.2f})"
            elif result == "KAZANDI":
                sonuc_txt = f"KAZANDI +${pnl:.2f}"
            else:
                sonuc_txt = f"kaybetti -${abs(pnl):.2f}"
            row = [city, "H", thr, acilis, "YES", giris, guncel, stake, kapanis, sonuc_txt]
            ws.append(row)
            fill = won_fill if result == "KAZANDI" else (lost_fill if result == "kaybetti" else open_fill)
            for c in range(1, len(cols) + 1):
                ws.cell(ws.max_row, c).fill = fill

        widths = {"A": 16, "B": 5, "C": 10, "D": 14, "E": 6, "F": 10, "G": 9, "H": 10, "I": 16, "J": 20}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    wb.save(args.out)
    print(f"yazildi: {args.out}")
    print("gunler:", ", ".join(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
