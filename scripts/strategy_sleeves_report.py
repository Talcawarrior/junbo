#!/usr/bin/env python3
"""Separate virtual bankrolls (sleeves) for spread and metar_peak.

Reads the live bet ledger, groups realized PnL by month and bet_type, and
reports each sleeve as if it had its own $1,000 starting bankroll.
"""

import csv
import os
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_data_dir, get_db_path  # noqa: E402

DATA_DIR = Path(os.getenv("JUNBO_DATA_DIR", get_data_dir()))
BOT_DB = Path(os.getenv("JUNBO_BOT_DB", get_db_path()))
REPO = Path(__file__).resolve().parent.parent
OUT_CSV = REPO / "reports" / "strategy_sleeves_latest.csv"
OUT_MD = REPO / "reports" / "strategy_sleeves_latest.md"
START = 1000.0


def _load():
    con = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute(
            "SELECT substr(COALESCE(settled_at, placed_at),1,7), "
            "COALESCE(bet_type,'legacy'), COUNT(*), "
            "SUM(CASE WHEN status IN ('won','settled_win') THEN 1 ELSE 0 END), "
            "ROUND(SUM(COALESCE(realized_pnl,0)),2), ROUND(SUM(stake_amount),2) "
            "FROM bets WHERE COALESCE(settled_at, placed_at) >= '2026-08-01' "
            "GROUP BY 1,2 ORDER BY 1,2"
        ).fetchall()
        city = con.execute(
            "SELECT COALESCE(bet_type,'legacy'), city, COUNT(*), "
            "SUM(CASE WHEN status IN ('won','settled_win') THEN 1 ELSE 0 END), "
            "ROUND(SUM(COALESCE(realized_pnl,0)),2) "
            "FROM bets WHERE COALESCE(settled_at, placed_at) >= '2026-08-01' "
            "GROUP BY 1,2 ORDER BY 1,5 DESC"
        ).fetchall()
    finally:
        con.close()
    return rows, city


def main() -> None:
    rows, city = _load()
    sleeves = defaultdict(lambda: {"bank": START, "rows": []})
    for month, btype, n, wins, pnl, stake in rows:
        s = sleeves[btype]
        s["bank"] += pnl or 0.0
        s["rows"].append((month, n, wins, pnl or 0.0, stake or 0.0, s["bank"]))

    REPO.joinpath("reports").mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sleeve", "month", "bets", "wins", "winrate_pct", "realized_pnl", "stake", "virtual_bankroll"])
        for btype, s in sorted(sleeves.items()):
            for month, n, wins, pnl, stake, bank in s["rows"]:
                w.writerow([btype, month, n, wins, round(100 * wins / max(n, 1), 1), pnl, stake, round(bank, 2)])

    lines = [
        "# Strategy Sleeves - Separate Virtual Bankrolls",
        "",
        f"Each sleeve starts at ${START:.0f}; only realized PnL is compounded. Open positions are not marked to market here.",  # noqa: E501
        "",
        "| Sleeve | Bets | Wins | Win% | Realized PnL | Stake | Virtual bankroll | ROI |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for btype, s in sorted(sleeves.items()):
        n = sum(r[1] for r in s["rows"])
        wins = sum(r[2] for r in s["rows"])
        pnl = sum(r[3] for r in s["rows"])
        stake = sum(r[4] for r in s["rows"])
        lines.append(
            "| %s | %d | %d | %.1f%% | %.2f | %.2f | %.2f | %.1f%% |"
            % (btype, n, wins, 100 * wins / max(n, 1), pnl, stake, s["bank"], 100 * pnl / max(stake, 1))
        )
    lines += [
        "",
        "## Monthly",
        "",
        "| Sleeve | Month | Bets | Win% | PnL | Bankroll |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for btype, s in sorted(sleeves.items()):
        for month, n, wins, pnl, stake, bank in s["rows"]:
            lines.append(
                "| %s | %s | %d | %.1f%% | %.2f | %.2f |" % (btype, month, n, 100 * wins / max(n, 1), pnl, bank)
            )
    lines += ["", "## Cities", "", "| Sleeve | City | Bets | Wins | PnL |", "|---|---|---:|---:|---:|"]
    by_type = defaultdict(list)
    for btype, c, n, wins, pnl in city:
        by_type[btype].append((c, n, wins, pnl or 0.0))
    for btype, vals in sorted(by_type.items()):
        for c, n, wins, pnl in vals[:5] + vals[-5:]:
            lines.append("| %s | %s | %d | %d | %.2f |" % (btype, c, n, wins, pnl))
    lines += [
        "",
        "## Notes",
        "- This is an accounting view, not a separate portfolio in the bot.",
        "- `legacy` is the old untyped ledger; `spread` and `metar_peak` are the current tagged strategies.",
        "- Open bets are excluded from realized bankroll until settled.",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    dated_csv = REPO / "reports" / f"strategy_sleeves_{stamp}.csv"
    dated_md = REPO / "reports" / f"strategy_sleeves_{stamp}.md"
    shutil.copyfile(OUT_CSV, dated_csv)
    shutil.copyfile(OUT_MD, dated_md)
    print("CSV:", OUT_CSV)
    print("MD:", OUT_MD)
    print("DATED:", dated_csv, dated_md)
    for btype, s in sorted(sleeves.items()):
        n = sum(r[1] for r in s["rows"])
        wins = sum(r[2] for r in s["rows"])
        pnl = sum(r[3] for r in s["rows"])
        stake = sum(r[4] for r in s["rows"])
        print(
            "%-12s bets=%4d win=%3d wr=%5.1f%% pnl=$%9.2f roi=%6.1f%% bank=$%.2f"
            % (btype, n, wins, 100 * wins / max(n, 1), pnl, 100 * pnl / max(stake, 1), s["bank"])
        )


if __name__ == "__main__":
    main()
