"""Analyze genuine first-peak "climb" hours for city weather-markets.

Only markets that RISE from a low baseline before snapshot collection) and
HOLD are useful for timing the first peak. Markets already priced high
(>= HOLD_FLOOR) at their first snapshot are excluded -- that is a batch
artifact (decided before snapshot collection), not a climb.

CLIMB qualified when:
  - first snapshot yes_price < CLIMB_FLOOR_MAX (started low), AND
  - later reaches >= HOLD_FLOOR, AND
  - from that reach onward it holds at/above HOLD_FLOOR (never drops back).

Reported:
  - first_hold : first cross >= HOLD_FLOOR that then holds.
  - first_peak : within the hold, time reached PEAK_FRACTION * peak.

Data:
  - snapshots.db : historical market_snapshots (Aug 4-5).
  - bot.db       : weather_markets (city names) + bets_archive (outcomes).
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

CLIMB_START_MAX = 0.50  # first snapshot must be BELOW this (genuine climb)
HOLD_FLOOR = 0.50  # price must stay >= this after first cross
MIN_SNAPS = 4
PEAK_FRACTION = 0.90  # "peak" = 90% of final peak price


@dataclass
class Climb:
    market_id: str
    city: str
    start_price: float
    peak_price: float
    first_hold: datetime
    first_peak: datetime | None

    @property
    def first_hold_hour(self) -> int:
        return self.first_hold.hour


def load_outcomes() -> dict[str, dict]:
    cdb = sqlite3.connect(str(BOT_DB))
    c = cdb.cursor()
    c.execute("SELECT market_id, city, status, entry_price FROM bets_archive WHERE status IN ('won', 'lost')")
    out: dict[str, dict] = {}
    for mid, city, status, entry in c.fetchall():
        out[str(mid)] = {"city": city, "status": status, "entry": entry}
    cdb.close()
    return out


def load_snapshots() -> dict[str, list[dict]]:
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


def get_city(market_id: str) -> str | None:
    cdb = sqlite3.connect(str(BOT_DB))
    c = cdb.cursor()
    c.execute("SELECT city FROM weather_markets WHERE id=?", (market_id,))
    row = c.fetchone()
    cdb.close()
    return row[0] if row and row[0] else None


def classify(snaps: list[dict]) -> tuple[datetime, float, float] | None:
    """Return (first_hold, peak_price, start_price) for a genuine CLIMB."""
    if not snaps:
        return None
    first_p = snaps[0]["p"]
    if first_p >= CLIMB_START_MAX:
        return None  # started high: batch artifact, not a climb

    p = [x["p"] for x in snaps]
    t = [x["t"] for x in snaps]
    if len(p) < MIN_SNAPS:
        return None

    peak = max(p)
    if peak < HOLD_FLOOR + 0.05:
        return None

    for i in range(len(p)):
        if p[i] >= HOLD_FLOOR and all(v >= HOLD_FLOOR for v in p[i:]):
            return (t[i], peak, first_p)
    return None


def find_first_peak(snaps, peak, first_hold):
    target = PEAK_FRACTION * peak
    for x in snaps:
        if x["t"] >= first_hold and x["p"] >= target:
            return x["t"]
    return None


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outcomes = load_outcomes()
    snapshots = load_snapshots()

    climbs: list[Climb] = []
    seen: set[str] = set()

    for mid, meta in outcomes.items():
        snaps = snapshots.get(mid)
        if not snaps:
            continue
        res = classify(snaps)
        if res is None:
            continue
        first_hold, peak, start = res
        climbs.append(
            Climb(
                market_id=mid,
                city=meta["city"] or "?",
                start_price=start,
                peak_price=peak,
                first_hold=first_hold,
                first_peak=find_first_peak(snaps, peak, first_hold),
            )
        )
        seen.add(mid)

    for mid, snaps in snapshots.items():
        if mid in seen or mid in outcomes:
            continue
        res = classify(snaps)
        if res is None:
            continue
        first_hold, peak, start = res
        city = get_city(mid) or "?"
        climbs.append(
            Climb(
                market_id=mid,
                city=city,
                start_price=start,
                peak_price=peak,
                first_hold=first_hold,
                first_peak=find_first_peak(snaps, peak, first_hold),
            )
        )
        seen.add(mid)

    climbs = [c for c in climbs if c.city != "?"]

    print("=" * 70)
    print("FIRST-PEAK CLIMB ANALYSIS (genuine climbs only)")
    print("=" * 70)
    print(f"Genuine climb markets: {len(climbs)}")
    print(f"(criteria: start<{CLIMB_START_MAX}, reach>={HOLD_FLOOR} and hold, >= {MIN_SNAPS} snaps)")

    print("\n--- Per-market first hold/peak (UTC) ---")
    hdr = f"{'market':10s} {'city':14s} {'start':>6s} {'peak':>5s} {'holdUTC':>7s} {'peakUTC':>7s}"
    print(hdr)
    print("-" * len(hdr))
    for c in sorted(climbs, key=lambda x: x.first_hold):
        pk = c.first_peak.strftime("%H:%M") if c.first_peak else "  -"
        print(
            f"{c.market_id:10s} {c.city:14s} {c.start_price:6.2f} {c.peak_price:5.2f} "
            f"{c.first_hold.strftime('%H:%M'):>7s} {pk:>7s}"
        )

    print("\n--- Genuine climbs by FIRST-HOLD UTC hour ---")
    hold_buckets: dict[int, list[str]] = defaultdict(list)
    for c in climbs:
        hold_buckets[c.first_hold.hour].append(f"{c.city}({c.market_id})")
    for hh in sorted(hold_buckets):
        print(f"  {hh:02d}:00  [{len(hold_buckets[hh])}]  {', '.join(hold_buckets[hh])}")

    print("\n--- Genuine climbs by FIRST-PEAK UTC hour ---")
    pk_buckets: dict[int, list[str]] = defaultdict(list)
    for c in climbs:
        if c.first_peak is not None:
            pk_buckets[c.first_peak.hour].append(f"{c.city}({c.market_id})")
    for hh in sorted(pk_buckets):
        print(f"  {hh:02d}:00  [{len(pk_buckets[hh])}]  {', '.join(pk_buckets[hh])}")

    print("\n--- City-level first-hold windows ---")
    by_city: dict[str, list[datetime]] = defaultdict(list)
    for c in climbs:
        by_city[c.city].append(c.first_hold)
    for city in sorted(by_city):
        times = sorted(by_city[city])
        print(f"  {city:14s} n={len(times):2d}  windows={[t.strftime('%H:%M') for t in times]}")

    report = {
        "generated_at": datetime.now().isoformat(),
        "params": {
            "climb_start_max": CLIMB_START_MAX,
            "hold_floor": HOLD_FLOOR,
            "peak_fraction": PEAK_FRACTION,
            "min_snaps": MIN_SNAPS,
        },
        "climbs": [
            {
                "market_id": c.market_id,
                "city": c.city,
                "start_price": c.start_price,
                "peak_price": c.peak_price,
                "first_hold_utc": c.first_hold.isoformat(),
                "first_hold_hour_utc": c.first_hold.hour,
                "first_peak_utc": c.first_peak.isoformat() if c.first_peak else None,
            }
            for c in climbs
        ],
    }
    out = OUT_DIR / "first_peak_climb_report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, default=str, indent=2)
    print(f"\nReport saved: {out}")


if __name__ == "__main__":
    main()
