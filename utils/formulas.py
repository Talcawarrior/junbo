"""Central financial formulas — single source of truth for ALL calculations.

Every caller MUST import from here instead of re-implementing the formula.
If the math changes, change it here — it propagates everywhere.

Formula inventory:
  1. max_bet_cap              — per-bet dollar ceiling
  2. conservative_value       — portfolio basis that excludes today's unrealized gains
  3. max_exposure             — total open-position ceiling
  4. unrealized_pnl           — per-bet paper PnL
  5. settlement_pnl           — per-bet realised PnL (Polymarket win/loss)
  6. polymarket_fee           — C × feeRate × p × (1-p)  (official)
  7. polymarket_fee_from_stake — stake × feeRate × (1-p)  (shortcut)
  8. settlement_payout        — gross payout when winning
  9. portfolio_total          — cash + open_exposure (book value)
  10. portfolio_current       — initial + all PnL (market value, includes unrealised)
  11. roi_pct                 — return on stake
  12. win_rate_pct            — wins / closed
  13. bet_shares              — stake / price (shares purchased)
"""

from __future__ import annotations

# Import bot_config for dynamic fee rates
from config.settings import bot_config

# ---------------------------------------------------------------------------
# 1. Max bet per position
# ---------------------------------------------------------------------------


def max_bet_cap(portfolio_value: float, max_bet_pct: float) -> float:
    """Per-bet dollar ceiling = portfolio_value × max_bet_pct.

    Used by:
      - calculator.py (:309, :325) — Kelly sizing
      - bet_placer.py (:182)       — Cap 1 hard ceiling
      - utils/kelly.py (:110)      — kelly_bet_amount wrapper
      - strategy.py (:555)         — was duplicated, now deleted
    """
    return portfolio_value * max_bet_pct


# ---------------------------------------------------------------------------
# 2. Conservative portfolio value (initial + realised closed before today)
# ---------------------------------------------------------------------------


def conservative_portfolio_value(
    initial_capital: float, realized_before_today: float
) -> float:
    """Portfolio basis that prevents the feedback loop.

    Only counts:
      - initial capital
      - PnL from bets that CLOSED before today (realised)

    Excludes:
      - today's realised PnL (would inflate today's cap)
      - unrealised PnL (paper money)

    Used by:
      - strategy.py (_conservative_portfolio_value, check_exposure_cap)
      - main.py (:340)  — API max_exposure
      - bet_placer.py   — Cap 2 log
    """
    return initial_capital + realized_before_today


# ---------------------------------------------------------------------------
# 3. Max total exposure (sum of all open bet amounts)
# ---------------------------------------------------------------------------


def max_exposure_cap(
    initial_capital: float, realized_before_today: float, total_exposure_pct: float
) -> float:
    """Total open-position ceiling.

    Formula: (initial + realized_before_today) × TOTAL_EXPOSURE_PCT

    Used by:
      - strategy.py (check_exposure_cap, calculate_position_size*)
      - main.py     (API /api/status → portfolio.max_exposure)
      - bet_placer.py (Cap 2)
    """
    return (
        conservative_portfolio_value(initial_capital, realized_before_today)
        * total_exposure_pct
    )


# ---------------------------------------------------------------------------
# 4. Unrealised PnL per bet
# ---------------------------------------------------------------------------


def unrealized_pnl(shares: float, current_price: float, entry_price: float) -> float:
    """Paper profit / loss on an open position.

    shares × (current_price − entry_price)

    Used by:
      - scheduler.py (:137)  — run_price_update
      - frontend (api.ts:462) — open positions display
    """
    return shares * (current_price - entry_price)


# ---------------------------------------------------------------------------
# 5. Settlement PnL (Polymarket win / loss)
# ---------------------------------------------------------------------------


def settlement_payout(stake: float, entry_price: float) -> float:
    """Gross payout when a bet wins: stake / entry_price.

    Used by settler.py for credit_settlement accounting.
    """
    return stake / entry_price if entry_price > 0 else 0.0


def settlement_pnl(
    stake: float, entry_price: float, entry_fee: float, won: bool
) -> float:
    """Realised PnL when Polymarket resolves a bet.

    According to Polymarket's official fee model:
      fee = C × feeRate × p × (1-p)

    The fee is charged at **trade match time** (entry), NOT at settlement.
    At settlement the outcome share price is $1.00 (won) or $0.00 (lost),
    so p × (1-p) = 0 — settlement fee is mathematically zero.

    When won:
      payout       = stake / entry_price      (= shares × $1.00)
      settlement_fee = 0                      (mathematical: p→1 ⇒ p(1-p)→0)
      net_pnl      = payout − stake − entry_fee

    When lost:
      net_pnl      = −stake − entry_fee       (stake + fee already paid)

    entry_fee is calculated at bet placement time and stored in Bet.entry_fee.
    See polymarket_fee() / polymarket_fee_from_stake() in this module.

    Used by:
      - settler.py   — _settle_market_resolution
    """
    if not won:
        return -(stake + entry_fee)

    payout = settlement_payout(stake, entry_price)
    return payout - stake - entry_fee


# ---------------------------------------------------------------------------
# 6. Polymarket taker fee — official formula
# ---------------------------------------------------------------------------


def polymarket_fee(shares: float, price: float, fee_rate: float | None = None) -> float:
    """Polymarket taker fee at trade match time.

    Official formula (per docs.polymarket.com):
      fee = C × feeRate × p × (1-p)

    Where:
      C        = number of shares traded
      feeRate  = fetched from Polymarket API (default: 0.05 for weather)
      p        = trade price (0.01–0.99)

    Fee is collected at order match time, NOT at market settlement.
    Settlement fee is always zero (p→1 ⇒ p(1-p)→0).
    """
    if fee_rate is None:
        fee_rate = bot_config.strategy.current_fee_rate

    return shares * fee_rate * price * (1.0 - price)


def polymarket_fee_from_stake(stake: float, price: float, fee_rate: float | None = None) -> float:
    """Stake-based shortcut for polymarket_fee.

    Since shares = stake / price, the fee formula simplifies to:
      fee = (stake / price) × feeRate × p × (1-p) = stake × feeRate × (1-p)

    Fee rate is fetched from Polymarket API (default: 0.05 for weather).
    """
    if price <= 0:
        return 0.0

    if fee_rate is None:
        fee_rate = bot_config.strategy.current_fee_rate

    return stake * fee_rate * (1.0 - price)


# ---------------------------------------------------------------------------
# 7. Shares purchased
# ---------------------------------------------------------------------------


def bet_shares(stake: float, fill_price: float) -> float:
    """Number of outcome shares the stake buys at the given fill price."""
    if fill_price <= 0:
        return 0.0
    return stake / fill_price


# ---------------------------------------------------------------------------
# 8. Portfolio book value (accounting view)
# ---------------------------------------------------------------------------


def portfolio_total_value(cash_balance: float, open_exposure: float) -> float:
    """Book value: cash on hand + stakes locked in open bets.

    Excludes unrealised PnL (paper gains/losses).

    Used by:
      - scheduler.py (:200)  — run_price_update
      - settler.py   (:99)   — post-settlement sync
    """
    return round(cash_balance + open_exposure, 2)


# ---------------------------------------------------------------------------
# 9. Portfolio market value (dashboard view)
# ---------------------------------------------------------------------------


def portfolio_current_value(
    initial_capital: float, realized_pnl: float, unrealized_pnl: float
) -> float:
    """Market value: initial + all PnL (includes unrealised paper gains).

    Used by:
      - main.py      (:332)  — API /api/status → portfolio.current
      - frontend     (api.ts:350, :391, :416)
    """
    return initial_capital + realized_pnl + unrealized_pnl


# ---------------------------------------------------------------------------
# 10. ROI percentage
# ---------------------------------------------------------------------------


def pnl_ratio(current_price: float, entry_price: float) -> float:
    """Fiyat değişimi oranı (0-1 arası ratio, percentage DEĞİL).

    pnl_ratio = (current_price - entry_price) / entry_price

    Tüm exit check'ler bu fonksiyonu kullanmalı.
    1.0 = %100 kâr, -0.3 = %30 zarar.

    Kullanım:
      - check_take_profit: pnl_ratio >= cfg.take_profit_pct
      - check_stop_loss: pnl_ratio <= -cfg.stop_loss_pct
      - check_time_decay: pnl_ratio <= cfg.time_decay_threshold
    """
    if entry_price <= 0:
        return 0.0
    return (current_price - entry_price) / entry_price


def drop_ratio(peak_price: float, current_price: float) -> float:
    """Tepeden düşüş oranı (trailing stop için).

    drop_ratio = (peak_price - current_price) / peak_price

    Kullanım:
      - check_trailing_stop: drop_ratio >= cfg.trailing_stop_pct
    """
    if peak_price <= 0:
        return 0.0
    return (peak_price - current_price) / peak_price


def roi_pct(pnl: float, stake: float) -> float:
    """Return on investment as a percentage.

    ROI = (pnl / stake) × 100

    Kullanım: API display, historical stats
    """
    if stake <= 0:
        return 0.0
    return (pnl / stake) * 100


# profit_pct KALDIRILDI — pnl_ratio() * 100 kullanın


# ---------------------------------------------------------------------------
# 11. Daily PnL
# ---------------------------------------------------------------------------


def daily_pnl(today_realized: float, open_bets: list) -> float:
    """Today's total PnL = realised today + sum(unrealised on open bets)."""
    unrealized_total = sum(getattr(b, "unrealized_pnl", 0) or 0 for b in open_bets)
    return today_realized + float(unrealized_total)


# ---------------------------------------------------------------------------
# 12. Win rate
# ---------------------------------------------------------------------------


def win_rate_pct(wins: int, total_closed: int) -> float:
    """Win rate as a percentage.

    win_rate = (wins / total_closed) × 100

    Used by:
      - main.py (:898, :1307, :1363)
    """
    if total_closed <= 0:
        return 0.0
    return (wins / total_closed) * 100



