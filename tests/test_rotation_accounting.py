"""Rotation kapanis muhasebesi testleri.

Gecmis: 2026-09-16'da free-roll (``run_freeroll``) ve fee-gate
(``net_win_profit``) ozellikleri README'ye eklenmisti. Bu ozellikler
daha sonra urun kodundan geri alindi; testleri kalmisti ve suite'i
surekli kirmizi tutuyordu (3 test ImportError). 2026-10-27'de (S14)
olu testler kaldirildi, bu dosya yalnizca gecerli rotation muhasebe
testini tasir.
"""

from datetime import datetime, timezone


def test_rotation_close_writes_both_pnl_columns(market_factory):
    """2026-09-18: rotation kapanis pnl + portfolio toplamini da yazar."""
    from database.db import get_session
    from database.models import Bet, Portfolio
    from executor.bet_placer import BetPlacer

    with get_session() as s:
        m = market_factory(
            city="Testville",
            city_code="TEST",
            metric="temperature_max",
            market_type="RANGE",
            threshold=25.0,
            target_date=datetime.now(timezone.utc).replace(tzinfo=None),
            status="open",
            yes_price=0.30,
        )
        if s.query(Portfolio).filter_by(id=1).first() is None:
            s.add(
                Portfolio(
                    id=1,
                    initial_value=1000.0,
                    current_value=1000.0,
                    cash_balance=1000.0,
                    total_value=1000.0,
                    total_realized_pnl=0.0,
                )
            )
        b = Bet(
            market_id=str(m),
            city="Testville",
            city_code="TEST",
            side="YES",
            amount=2.0,
            stake_amount=2.0,
            price=0.20,
            entry_price=0.20,
            shares=10.0,
            current_price=0.20,
            strike_temp=25.0,
            status="placed",
            realized_pnl=0.0,
            pnl=0.0,
            order_id="test_rot_1",
            entry_fee=0.02,
            placed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        s.add(b)
        s.commit()
        pf_before = s.query(Portfolio).filter_by(id=1).first().total_realized_pnl or 0.0
        placer = BetPlacer.__new__(BetPlacer)
        assert placer.close_bet_for_rotation(b, 0.10, s) is True
        s.commit()
        nb = s.query(Bet).filter_by(id=b.id).first()
        assert nb.status == "closed"
        assert nb.realized_pnl < 0  # 10*0.10 - 2.02
        assert nb.pnl == nb.realized_pnl  # iki kolon ayni
        pf_after = s.query(Portfolio).filter_by(id=1).first().total_realized_pnl
        assert pf_after < pf_before  # portfolio toplamina islendi
        assert pf_after == pf_before + nb.realized_pnl  # tam tutar, yaklasik degil
