"""Settlement davranis testleri (2026-08-08 bugfix sonrasi).

Kural: Kapanis (24:00 UTC = target_date + 12h) GECMEDEN acik beti olmayan
market "expired" yapilmaz. Bu, SL ile kapanan betin marketi hala canliyken
reopen'in yeni lider acabilmesi icin kritikti (Toronto 30C, Miami 33.6C,
Buenos Aires 13C bug'lari).
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_db():
    from config.settings import bot_config
    from database.db import get_session
    from database.models import Bet, Portfolio, WeatherForecast, WeatherMarket

    # SL->settler->reopen zinciri EDGE moduna ozgu (spread'de SL devre disi).
    bot_config.strategy.betting_strategy = "edge"
    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()


def _td(hours_ahead):
    """target_date = now + hours_ahead (kapanis = target + 12h)."""
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(hours=hours_ahead)


def _add_market(session, mid, yes_price, td, city="Testville"):
    from database.models import WeatherMarket

    session.add(
        WeatherMarket(
            id=mid,
            question="T?",
            city=city,
            city_code="TEST",
            metric="temperature_max",
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


def _add_closed_bet(session, market_id, city, status="closed_early", reason="stop_loss: -25.0%"):
    from datetime import datetime, timedelta, timezone

    from database.models import Bet

    session.add(
        Bet(
            market_id=market_id,
            city=city,
            city_code="TEST",
            side="YES",
            amount=10.0,
            price=0.60,
            status=status,
            close_reason=reason,
            realized_pnl=-2.5,
            closed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    session.commit()


class TestSettlementExpiredRule:
    def test_no_open_bet_market_not_yet_closed_not_expired(self):
        """Kapanis gecmemis + acik bet yok -> expired OLMAMALI (2026-08-08 bug)."""
        from executor.settler import SettlementEngine

        td = _td(2)  # kapanis = now + 14h -> gecmemis
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.70, td)
            _add_closed_bet(s, "m1", "Testville")  # SL ile kapandi

            SettlementEngine().settle_all()

            from database.models import WeatherMarket

            m = s.query(WeatherMarket).filter_by(id="m1").first()
            assert m.status != "expired", "kapanis gecmemis market expired yapilmamali - reopen yeni lider acamaz"

    def test_no_open_bet_market_past_close_expired(self):
        """Kapanis GECTI + acik bet yok -> expired olur (normal)."""
        from executor.settler import SettlementEngine

        td = _td(-13)  # target -13h -> kapanis (target+12h) = -1h -> GECTI
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m2", 0.70, td)
            _add_closed_bet(s, "m2", "Testville")

            SettlementEngine().settle_all()

            from database.models import WeatherMarket

            m = s.query(WeatherMarket).filter_by(id="m2").first()
            assert m.status == "expired", "kapanis gecti + bet yoksa expired olmali"

    def test_open_bet_market_not_expired(self):
        """Acik bet varsa market asla expired yapilmaz."""
        from executor.settler import SettlementEngine

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m3", 0.70, td)
            from database.models import Bet
            from datetime import datetime, timezone

            s.add(
                Bet(
                    market_id="m3",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    price=0.60,
                    status="placed",
                    placed_at=datetime.now(timezone.utc),
                )
            )
            s.commit()

            SettlementEngine().settle_all()

            from database.models import WeatherMarket

            m = s.query(WeatherMarket).filter_by(id="m3").first()
            assert m.status != "expired", "acik betli market expired yapilmaz"


class TestSettlementFullChain:
    def test_sl_then_settler_then_reopen_opens_new_bet(self):
        """SL -> settle_all -> reopen tam zincir: yeni bet acilmali."""
        from executor.bet_placer import BetPlacer
        from executor.settler import SettlementEngine

        td = _td(2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_market(s, "m1", 0.80, td)  # kayip market, hala en yuksek
            _add_market(s, "m2", 0.55, td)  # ikinci en yuksek -> yeni lider
            _add_closed_bet(s, "m1", "Testville")

        # 1) Settler araya girip marketi expired yapmamali
        SettlementEngine().settle_all()

        # 2) Reopen m2'ye acmali
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            bp = BetPlacer()
            reopened = bp._reopen_after_stop_loss(s)

            from database.models import Bet, WeatherMarket

            m1 = s.query(WeatherMarket).filter_by(id="m1").first()
            assert m1.status != "expired", "settle_all canli marketi expired yapmamali"
            new_bet = s.query(Bet).filter(Bet.market_id == "m2", Bet.status.in_(("placed", "pending"))).first()
            assert reopened >= 1, "reopen yeni lider acmali"
            assert new_bet is not None, "m2'ye bet acilmali"
