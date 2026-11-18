"""0-stake / cok kucuk cop betleri kapat (2026-08-19 tek seferlik temizlik).

Derinlik limiti bugi (kod artik KALDIRILDI) 0.00 ve 0.40 USD'lik betler
acmisti: 0 share'li betler PnL uretmez, gunluk limiti sisirir ve metar icin
_existing_metar_bet kilidi olur (Madrid 36C). Uygulama katmanindan
close_bet_for_rotation ile kapatilir — DB'ye direkt SQL YOK (anayasa).

Kullanim: python scripts/close_zero_stake_bets.py [--dry-run]
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from database.db import get_session
from database.models import Bet, WeatherMarket
from executor.bet_placer import BetPlacer
from utils.activity_log import log_event


def main() -> int:
    dry = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date().isoformat()
    placer = BetPlacer()
    closed = 0
    with get_session() as session:
        rows = (
            session.query(Bet, WeatherMarket)
            .join(WeatherMarket, WeatherMarket.id == Bet.market_id)
            .filter(
                Bet.placed_at.like(f"{today}%"),
                Bet.amount < 1.0,
                Bet.status.in_(("placed", "active", "open", "pending")),
            )
            .all()
        )
        for bet, wm in rows:
            try:
                live = float(wm.yes_price) if wm.yes_price and float(wm.yes_price) > 0 else float(bet.entry_price or 0)
            except (TypeError, ValueError):
                live = 0.0
            price = max(0.01, min(0.99, live))
            if dry:
                print(f"[dry] kapatilacak: {bet.order_id} {bet.city} stake=${bet.amount:.2f} price={price:.3f}")
                continue
            ok = placer.close_bet_for_rotation(bet, price, session)
            if ok:
                closed += 1
                print(f"kapatildi: {bet.order_id} {bet.city} stake=${bet.amount:.2f} price={price:.3f}")
                log_event("bet_closed", str(bet.city), f"cop bet temizligi (stake=${bet.amount:.2f})")
        session.commit()
    print(f"toplam kapatilan: {closed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
