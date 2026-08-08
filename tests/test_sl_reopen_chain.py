"""SL -> Settler -> Reopen entegrasyon testi (2026-08-08 bugfix).

2026-08-08'de bulunan bug: SL ile kapanan betin marketi hala canliyken
settler onu "expired" yapiyordu -> _reopen_after_stop_loss status='open'
aradigi icin yeni lider ASLA acilamiyordu (Toronto 30C, Miami 33.6C,
Buenos Aires 13C ornekleri). Izole unit testler bu zinciri yakalayamadi
cunku her modul ayri ayri test ediliyordu; bug iki modulun ETKILESIMINDE
ortaya cikiyor.

Bu test tam zinciri kurar:
  1. Canli market (kapanis 24:00 UTC = target_date + 12h GECMEMIS)
  2. O markette SL ile kapanmis bet
  3. settle_all() calisir -> market status 'expired' OLMAMALI
  4. _reopen_after_stop_loss() calisir -> kayip market haric en yuksek
     fiyatliya yeni bet ACILMALI
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, Portfolio, WeatherForecast, WeatherMarket

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()


def _td(offset=2):
    """Kapanis (target+12h) now+20h icinde kalacak target_date."""
    from datetime import datetime, timedelta, timezone

    # target_date = now + 2h -> kapanis = now + 14h (<= now + 20h, gecerli)
    return datetime.now(timezone.utc) + timedelta(hours=offset)


def _add_market(session, mid, yes_price, td, city="Testville", metric="temperature_max"):
    from database.models import WeatherMarket

    session.add(
        WeatherMarket(
            id=mid,
            question="Test?",
            city=city,
            city_code="TEST",
            metric=metric,
            threshold=25.0,
            target_date=td,
            latitude=41.0,
            longitude=29.0,
            market_type="HIGH",
            yes_price=yes_price,
            no_price=round(1.0 - yes_price, 4),
            status="open",
        )
    )


def _add_portfolio(session, cash=5000.0):
    from database.models import Portfolio

    session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
    session.commit()


def _add_sl_bet(session, market_id, city, closed_at):
    """SL ile kapanmis bet (market hala canli)."""

    from database.models import Bet

    session.add(
        Bet(
            market_id=market_id,
            city=city,
            city_code="TEST",
            side="YES",
            amount=10.0,
            price=0.60,
            status="closed_early",
            close_reason="stop_loss: -25.0%",
            realized_pnl=-2.5,
            closed_at=closed_at,
        )
    )
    session.commit()


class TestSlSettlerReopenChain:
    def test_settler_does_not_expire_live_market_after_sl(self):
        """Bug 1: SL sonrasi canli market (kapanis gecmemis) expired yapilmaz."""
        from datetime import datetime, timedelta, timezone

        from executor.settler import SettlementEngine

        td = _td(2)  # kapanis = now + 14h -> GECMEMIS
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.70, td)
            _add_sl_bet(s, "m1", "Testville", datetime.now(timezone.utc) - timedelta(minutes=5))
            # Kapanis gecmedigi icin settle_all marketi expired YAPMAMALI
            SettlementEngine().settle_all()
            from database.models import WeatherMarket

            m = s.query(WeatherMarket).filter_by(id="m1").first()
            assert m is not None
            assert m.status != "expired", (
                "SL sonrasi canli market expired yapildi! reopen bu yuzden yeni lider acamiyordu (2026-08-08 bug)"
            )

    def test_reopen_picks_second_highest_when_lost_market_still_top(self):
        """Bug 2: Kayip market en yuksek fiyatliysa, ikinci en yuksek secilir."""
        from datetime import datetime, timedelta, timezone

        from executor.bet_placer import BetPlacer

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            # m1 (kayip market) hala en yuksek fiyatli; m2 ikinci
            _add_market(s, "m1", 0.80, td)
            _add_market(s, "m2", 0.55, td)
            _add_sl_bet(s, "m1", "Testville", datetime.now(timezone.utc) - timedelta(minutes=5))

            bp = BetPlacer()
            reopened = bp._reopen_after_stop_loss(s)

            from database.models import Bet

            new_bet = s.query(Bet).filter(Bet.market_id == "m2", Bet.status.in_(("placed", "pending"))).first()
            assert reopened >= 1, "kayip market disindaki en yuksek fiyatliya yeni bet acilmali"
            assert new_bet is not None, "m2 (ikinci en yuksek) secilmeli, m1 degil"

    def test_full_chain_sl_settler_reopen(self):
        """Tam zincir: SL -> settle_all -> reopen, yeni bet acilmali."""
        from datetime import datetime, timedelta, timezone

        from executor.bet_placer import BetPlacer
        from executor.settler import SettlementEngine

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.80, td)
            _add_market(s, "m2", 0.55, td)
            _add_sl_bet(s, "m1", "Testville", datetime.now(timezone.utc) - timedelta(minutes=5))

        # 1) Settler: canli marketi expired yapmamali
        SettlementEngine().settle_all()

        # 2) Reopen: m2'ye yeni bet acmali
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            bp = BetPlacer()
            reopened = bp._reopen_after_stop_loss(s)

            from database.models import Bet, WeatherMarket

            m1 = s.query(WeatherMarket).filter_by(id="m1").first()
            assert m1.status != "expired", "settle_all canli marketi expired yapmamali"
            new_bet = s.query(Bet).filter(Bet.market_id == "m2", Bet.status.in_(("placed", "pending"))).first()
            assert reopened >= 1, "reopen yeni lider acmali"
            assert new_bet is not None, "m2'ye bet acilmali"
