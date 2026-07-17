from config.settings import Config
from database.db import get_session
from database.models import Bet, Portfolio

with get_session() as s:
    pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
    initial_value = float(pf.total_value or 0)

    closed_statuses = ("closed_early", "won", "lost")
    bets = s.query(Bet).filter(Bet.status.in_(closed_statuses)).order_by(Bet.id).all()
    total_closed = len(bets)

    wins = [b for b in bets if float(b.realized_pnl or 0) > 0]
    losses = [b for b in bets if float(b.realized_pnl or 0) <= 0]
    wr = len(wins) / total_closed

    odds_list = []
    for b in bets:
        price = float(b.entry_price or b.price or 0.5)
        side = b.side
        if side == "NO":
            b_odds = price / (1 - price) if (1 - price) > 0 else 0
        else:
            b_odds = (1 - price) / price if price > 0 else 0
        odds_list.append(b_odds)

    avg_odds = sum(odds_list) / len(odds_list) if odds_list else 0

    p = wr
    q = 1 - p
    b_avg = avg_odds
    kelly = (p * b_avg - q) / b_avg if b_avg > 0 else 0
    half_kelly = kelly * 0.5
    quarter_kelly = kelly * 0.25

    print(f"\n{'='*72}")
    print("  KELLY CRITERION ANALYSIS")
    print(f"{'='*72}")
    print(f"  Win rate (p):          {p:.4f} ({p*100:.1f}%)")
    print(f"  Loss rate (q):         {1-p:.4f} ({(1-p)*100:.1f}%)")
    print(f"  Average odds (b):       {avg_odds:.4f}")
    print(f"  Full Kelly (f*):       {kelly:.4f} ({kelly*100:.2f}%)")
    print(f"  Half Kelly:            {half_kelly:.4f} ({half_kelly*100:.2f}%)")
    print(f"  Quarter Kelly:         {quarter_kelly:.4f} ({quarter_kelly*100:.2f}%)")
    print(f"  Current MAX_BET_PCT:   {Config.MAX_BET_PCT} ({Config.MAX_BET_PCT*100:.1f}%)")