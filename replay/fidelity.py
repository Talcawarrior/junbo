"""Replay-vs-live bet matching metrics (pure, DB-free).

Used by tests/test_replay_fidelity.py to validate replay realism:
a replay bet matches a live bet when market_id is equal,
|placed_at| <= 15 min and |entry| <= 0.05. Each live bet matches
at most one replay bet.
"""

from __future__ import annotations

from datetime import datetime, timedelta

TIME_TOL_SEC = 15 * 60
ENTRY_TOL = 0.05
_EPS = 1e-9


def strategy_of(order_id) -> str:
    """spread_* -> spread, metar_* -> metar_peak, else other."""
    if isinstance(order_id, str):
        if order_id.startswith("spread"):
            return "spread"
        if order_id.startswith("metar"):
            return "metar_peak"
    return "other"


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


def _matchable(replay_bet: dict, live_bet: dict) -> tuple[bool, tuple[float, float]]:
    """(eslesir mi, (zaman_farki_sn, entry_farki)) — siralama icin skor doner."""
    if replay_bet.get("market_id") != live_bet.get("market_id"):
        return False, (0.0, 0.0)
    try:
        dt = abs((_as_dt(replay_bet["placed_at"]) - _as_dt(live_bet["placed_at"])).total_seconds())
    except Exception:
        return False, (0.0, 0.0)
    if dt > TIME_TOL_SEC:
        return False, (0.0, 0.0)
    try:
        de = abs(float(replay_bet["entry"]) - float(live_bet["entry"]))
    except Exception:
        return False, (0.0, 0.0)
    if de > ENTRY_TOL + _EPS:
        return False, (0.0, 0.0)
    return True, (dt, de)


def compare_replay_vs_live(replay, live, window_days: int = 3) -> dict:
    """Replay vs canli eslestirme metrikleri.

    Returns {"matched", "precision", "recall", "strategy", "days"}.
    precision = eslesen / replay sayisi, recall = eslesen / canli sayisi.
    """
    replay = list(replay or [])
    live = list(live or [])
    used = [False] * len(replay)
    pairs: list[tuple[int, int]] = []
    for li, lv in enumerate(live):
        best = -1
        best_key = None
        for ri, r in enumerate(replay):
            if used[ri]:
                continue
            ok, key = _matchable(r, lv)
            if not ok:
                continue
            if best_key is None or key < best_key:
                best, best_key = ri, key
        if best >= 0:
            used[best] = True
            pairs.append((li, best))
    matched = len(pairs)
    precision = matched / len(replay) if replay else 0.0
    recall = matched / len(live) if live else 0.0

    strategy = {s: {"matched": 0, "replay": 0, "live": 0} for s in ("spread", "metar_peak", "other")}
    for r in replay:
        strategy[strategy_of(r.get("order_id"))]["replay"] += 1
    for lv in live:
        strategy[strategy_of(lv.get("order_id"))]["live"] += 1
    for li, _ri in pairs:
        strategy[strategy_of(live[li].get("order_id"))]["matched"] += 1

    ref = None
    for bets in (live, replay):
        for b in bets:
            try:
                d = _as_dt(b["placed_at"]).date()
            except Exception:
                continue
            if ref is None or d > ref:
                ref = d
    if ref is None:
        ref = datetime.utcnow().date()
    days = []
    for i in reversed(range(max(1, int(window_days)))):
        day = (ref - timedelta(days=i)).isoformat()
        d_live = [lv for lv in live if _safe_day(lv) == day]
        d_replay = [r for r in replay if _safe_day(r) == day]
        d_pairs = [(li, ri) for li, ri in pairs if _safe_day(live[li]) == day]
        days.append({"day": day, "matched": len(d_pairs), "replay": len(d_replay), "live": len(d_live)})
    return {
        "matched": matched,
        "precision": precision,
        "recall": recall,
        "strategy": strategy,
        "days": days,
    }


def _safe_day(bet: dict) -> str:
    try:
        return _as_dt(bet["placed_at"]).date().isoformat()
    except Exception:
        return ""
