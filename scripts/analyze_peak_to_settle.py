"""Per-city FIRST-PEAK time and duration from that peak to settlement.

User's scenario (example): Ankara priced ~20% at noon, then 45%, 95% at
midnight, settles 100%. The first-peak value is NOT fixed -- it can be 20%,
70%, anything. We only want, per settled market:
  - first_peak_time  : when the YES price first reached its (near-final) peak
                        AND held there until settlement (never dropped back).
  - peak_price       : that locked-in price.
  - settlement_time  : actual settlement timestamp.
  - peak_to_settle_hours : hours from first_peak_time to settlement.

No start-price filter. A market needs >= MIN_SNAPS snapshots.
Data: snapshots.db (price series) + bets_archive (city, settlement, entry).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from utils.db_paths import get_db_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = Path(__file__).resolve().parent.parent
SNAP_DB = BASE / "data" / "snapshots.db"
BOT_DB = Path(get_db_path())
OUT_DIR = BASE / "data" / "analysis_results"

MIN_SNAPS = 4
SUSTAIN_FRAC = 0.95  # first_peak = first q where p>=0.95*peak and all after stay >=0.95*peak


@dataclass
class PeakInfo:
    market_id: str
    city: str
    status: str
    entry_price: float
    peak_price: float
    first_peak: datetime | None
    settlement: datetime | None
    hours_peak_to_settle: float | None
    peak_hour_utc: int | None


def load_settled() -> dict[str, dict]:
    cdb = sqlite3.connect(str(BOT_DB))
    c = cdb.cursor()
    c.execute(
        "SELECT market_id, city, status, entry_price, settled_at "
        "FROM bets_archive WHERE status IN ('won','lost') "
        "ORDER BY settled_at"
    )
    out: dict[str, dict] = {}
    for mid, city, status, entry, settled in c.fetchall():
        out[str(mid)] = {
            "city": city,
            "status": status,
            "entry": entry or 0.0,
            "settled": datetime.fromisoformat(settled) if settled else None,
        }
    cdb.close()
    return out


def load_series() -> dict[str, list[dict]]:
    sdb = sqlite3.connect(str(SNAP_DB))
    s = sdb.cursor()
    s.execute("SELECT market_id, yes_price, snapshot_time FROM market_snapshots ORDER BY market_id, snapshot_time")
    by_market: dict[str, list[dict]] = defaultdict(list)
    for mid, price, t in s.fetchall():
        if price is None:
            continue
        by_market[str(mid)].append({"t": datetime.fromisoformat(t), "p": float(price)})
    sdb.close()
    return by_market


def first_peak(snaps: list[dict]) -> datetime | None:
    """Earliest time price reached >= SUSTAIN_FRAC*peak and then held there."""
    if not snaps:
        return None
    p = [x["p"] for x in snaps]
    t = [x["t"] for x in snaps]
    peak = max(p)
    if peak <= 0.0:
        return None
    thr = SUSTAIN_FRAC * peak
    # find earliest i with p[i]>=thr and all later p[j]>=thr
    for i in range(len(p)):
        if p[i] >= thr and all(v >= thr for v in p[i:]):
            return t[i]
    return None


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    settled = load_settled()
    series = load_series()

    infos: list[PeakInfo] = []
    for mid, meta in sorted(settled.items()):
        snaps = series.get(mid)
        if not snaps or len(snaps) < MIN_SNAPS:
            continue
        pk = first_peak(snaps)
        peak = max(x["p"] for x in snaps)
        settle = meta["settled"]
        dur = hours_between(pk, settle) if (pk and settle) else None
        infos.append(
            PeakInfo(
                market_id=mid,
                city=meta["city"] or "?",
                status=meta["status"],
                entry_price=meta["entry"],
                peak_price=peak,
                first_peak=pk,
                settlement=settle,
                hours_peak_to_settle=dur,
                peak_hour_utc=pk.hour if pk else None,
            )
        )

    print("=" * 72)
    print("FIRST-PEAK & DURATION TO SETTLEMENT (per settled market)")
    print("=" * 72)
    print(f"Markets with trajectory: {len(infos)}  (MIN_SNAPS={MIN_SNAPS}, SUSTAIN_FRAC={SUSTAIN_FRAC})")

    hdr = (
        f"{'market':10s} {'city':12s} {'sts':>3s} {'entry':>6s} {'peak':>5s} "
        f"{'firstPeak':>10s} {'settle':>10s} {'hrToSettle':>9s}"
    )
    print(hdr)
    print("-" * len(hdr))
    for i in infos:
        fp = i.first_peak.strftime("%d %H:%M") if i.first_peak else "       -"
        st = i.settlement.strftime("%d %H:%M") if i.settlement else "       -"
        d = f"{i.hours_peak_to_settle:6.1f}" if i.hours_peak_to_settle is not None else "     -"
        print(
            f"{i.market_id:10s} {i.city:14s} {i.status:>3s} {i.entry_price:6.3f} "
            f"{i.peak_price:5.2f} {fp:>10s} {st:>10s} {d:>9s}"
        )

    print("\n--- First-PEAK hour (UTC) distribution ---")
    by_hour: dict[int, list[str]] = defaultdict(list)
    for i in infos:
        if i.peak_hour_utc is not None:
            by_hour[i.peak_hour_utc].append(f"{i.city}({i.market_id})")
    for hh in sorted(by_hour):
        print(f"  {hh:02d}:00  [{len(by_hour[hh]):2d}]  {', '.join(by_hour[hh])}")

    print("\n--- Per-city: first-peak hours + hours peak->settle ---")
    by_city: dict[str, list[PeakInfo]] = defaultdict(list)
    for i in infos:
        by_city[i.city].append(i)
    for city in sorted(by_city):
        items = by_city[city]
        hrs = [i.peak_hour_utc for i in items if i.peak_hour_utc is not None]
        durs = [i.hours_peak_to_settle for i in items if i.hours_peak_to_settle is not None]
        hr_s = f"{mins(hrs)}" if hrs else " -"
        dur_s = f"{round(mean(durs), 1)}h" if durs else " -"
        entries = ", ".join(
            f"{i.market_id}:{i.first_peak.strftime('%H:%M') if i.first_peak else '?'}({i.hours_peak_to_settle:.1f}h)"
            if i.first_peak and i.hours_peak_to_settle is not None
            else f"{i.market_id}:?"
            for i in items
        )
        print(f"  {city:14s} n={len(items)}  peakHrs={hr_s}  dur->S={dur_s}  {entries}")

    report = {
        "generated_at": datetime.now().isoformat(),
        "params": {"min_snaps": MIN_SNAPS, "sustain_frac": SUSTAIN_FRAC},
        "markets": [
            {
                "market_id": i.market_id,
                "city": i.city,
                "status": i.status,
                "entry_price": i.entry_price,
                "peak_price": i.peak_price,
                "first_peak_utc": i.first_peak.isoformat() if i.first_peak else None,
                "first_peak_hour_utc": i.peak_hour_utc,
                "settlement_utc": i.settlement.isoformat() if i.settlement else None,
                "hours_peak_to_settle": i.hours_peak_to_settle,
            }
            for i in infos
        ],
    }
    out = OUT_DIR / "first_peak_to_settle_report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, default=str, indent=2)
    print(f"\nReport saved: {out}")


def mins(nums: list[int]) -> str:
    lo, hi = min(nums), max(nums)
    if lo == hi:
        return f"{lo:02d}"
    return f"{lo:02d}-{hi:02d}"


def mean(nums: list[float]) -> float:
    return sum(nums) / len(nums)


if __name__ == "__main__":
    main()
