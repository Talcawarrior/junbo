"""Kapat 8 Agustos (24h kurali disi) betler - kural ihlali pozisyonlar."""

import sys
from datetime import datetime, timedelta, timezone

from database.db import get_session
from database.models import Bet, WeatherMarket
from executor.bet_placer import BetPlacer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

now = datetime.now(timezone.utc)
limit = now + timedelta(hours=24)
print(f"now={now}  limit={limit}")

placer = BetPlacer()
closed = 0
with get_session() as s:
    bets = s.query(Bet).filter(Bet.status.in_(("placed", "active"))).all()
    for bet in bets:
        wm = s.query(WeatherMarket).filter(WeatherMarket.id == bet.market_id).first()
        if not wm or not wm.target_date:
            continue
        td = wm.target_date
        if getattr(td, "tzinfo", None) is None:
            td = td.replace(tzinfo=timezone.utc)
        if td <= limit:
            continue
        # 24h kurali disi -> kapat
        yes_price = float(wm.yes_price or 0.5)
        if bet.side and bet.side.upper() == "NO":
            cur = max(0.0, min(1.0, 1.0 - yes_price))
        else:
            cur = max(0.0, min(1.0, yes_price))
        print(f"CLOSE bet#{bet.id} {bet.city} target={td} cur={cur:.4f}")
        placer.close_bet_for_rotation(bet, cur, s)
        closed += 1
print(f"closed={closed}")
