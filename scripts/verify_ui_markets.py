#!/usr/bin/env python3
"""Safe DB-side market sanity check used by the two-hour verifier schedule.

The script intentionally does not place, cancel, or settle bets. A browser/UI
adapter can be added later; this baseline verifies that active weather rows
have the fields required by the UI and strategy.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/verify_ui_markets.py` from the repository root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.db import get_session
from database.models import WeatherMarket


def main() -> int:
    with get_session() as session:
        rows = session.query(WeatherMarket).filter(WeatherMarket.status == "open").all()
        invalid = [
            m.id
            for m in rows
            if not m.city or not m.target_date or not m.metric or m.yes_price is None
        ]
    if invalid:
        print(f"UI market verification: {len(invalid)} invalid open markets: {invalid[:10]}")
        return 1
    print(f"UI market verification: {len(rows)} open markets OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
