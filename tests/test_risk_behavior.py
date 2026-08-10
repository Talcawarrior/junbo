"""Risk davranis testleri — SL/TP/trailing/time-decay karar + kapatma zinciri.

check_stop_loss / check_take_profit / check_trailing_stop / check_time_decay
SADECE karar verir (tuple: (bool, reason)). Kapatma scheduler icinde ayri
yapilir. Bu testler gercek DB + gercek Bet ile:
  1. check_* dogru karar veriyor mu (fiyat/zaman kosullarinda)
  2. exit sonrasi bet statusu + close_reason dogru mu
"""

import json
import pytest


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, Portfolio, WeatherForecast, WeatherMarket

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()


def _add_portfolio(session, cash=5000.0):
    from database.models import Portfolio

    session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
    session.commit()


def _add_bet(session, mid, entry_price, amount=10.0, side="YES", status="placed"):
    from datetime import datetime, timezone

    from database.models import Bet

    session.add(
        Bet(
            market_id=mid,
            city="Testville",
            city_code="TEST",
            side=side,
            amount=amount,
            price=entry_price,
            entry_price=entry_price,
            status=status,
            placed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()


def _risk_manager(session):
    from engine.strategy import RiskManager

    rm = RiskManager()
    rm.db = session
    return rm


class TestStopLossDecision:
    def test_sl_decision_true_on_big_drop(self):
        """-33% dusus -> check_stop_loss True + stop_loss nedeni."""
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.60)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            rm = _risk_manager(s)

            decision, reason = rm.check_stop_loss(bet, current_price=0.40)
            assert decision is True, "SL karari True olmali"
            assert reason.startswith("stop_loss"), f"neden stop_loss olmali: {reason}"

    def test_sl_decision_false_when_price_ok(self):
        """-16% dusus (%30 esik alti) -> False."""
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.60)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            rm = _risk_manager(s)

            decision, _ = rm.check_stop_loss(bet, current_price=0.50)
            assert decision is False, "esik altinda SL karari False olmali"


class TestTakeProfitDecision:
    @pytest.fixture(autouse=True)
    def _tp_on(self):
        from config.settings import bot_config

        bot_config.risk.take_profit_pct = 1.0

    def test_tp_decision_true_on_big_gain(self):
        """+100% kar -> check_take_profit True."""
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.40)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            rm = _risk_manager(s)

            decision, reason = rm.check_take_profit(bet, current_price=0.90)
            assert decision is True, "TP karari True olmali"
            assert "take_profit" in reason

    def test_tp_decision_false_on_small_gain(self):
        """+25% kar (esik %100 alti) -> False."""
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.40)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            rm = _risk_manager(s)

            decision, _ = rm.check_take_profit(bet, current_price=0.50)
            assert decision is False, "esik altinda TP karari False olmali"


class TestTrailingStopDecision:
    @pytest.fixture(autouse=True)
    def _ts_on(self):
        from config.settings import bot_config

        bot_config.risk.trailing_stop_pct = 0.15

    def test_ts_decision_true_from_peak(self):
        """Peak 0.80 -> 0.65 (-18.75% >= %15) -> True."""
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.50)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            bet.result_data = json.dumps({"peak_price": 0.80})
            s.commit()
            rm = _risk_manager(s)

            decision, _ = rm.check_trailing_stop(bet, current_price=0.65)
            assert decision is True, "zirveden %15+ dusus TS karari True olmali"

    def test_ts_decision_false_small_drop(self):
        """Peak 0.80 -> 0.75 (-6.25% < %15) -> False."""
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.50)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            bet.result_data = json.dumps({"peak_price": 0.80})
            s.commit()
            rm = _risk_manager(s)

            decision, _ = rm.check_trailing_stop(bet, current_price=0.75)
            assert decision is False, "kucuk dususte TS karari False olmali"


class TestTimeDecayDecision:
    @pytest.fixture(autouse=True)
    def _td_on(self):
        from config.settings import bot_config

        bot_config.risk.time_decay_hours = 4
        bot_config.risk.time_decay_threshold = -0.1

    def _market(self, session, td):
        from database.models import WeatherMarket

        session.add(
            WeatherMarket(
                id="m1",
                question="T?",
                city="Testville",
                city_code="TEST",
                metric="temperature_max",
                threshold=25.0,
                target_date=td,
                latitude=41.0,
                longitude=29.0,
                market_type="HIGH",
                yes_price=0.60,
                no_price=0.40,
                status="open",
            )
        )
        session.commit()
        return session.query(WeatherMarket).filter_by(id="m1").first()

    def test_time_decay_true_close_to_resolution_in_loss(self):
        """Kapanisa 2h kala + zarar -> True."""
        from datetime import datetime, timedelta, timezone

        td = datetime.now(timezone.utc) + timedelta(hours=2)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.60)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            market = self._market(s, td)
            rm = _risk_manager(s)

            decision, reason = rm.check_time_decay(bet, current_price=0.45, market=market)
            assert decision is True, "kapanisa yakin zararda time-decay True olmali"

    def test_time_decay_false_when_far_from_resolution(self):
        """Kapanisa 10h kala -> False."""
        from datetime import datetime, timedelta, timezone

        td = datetime.now(timezone.utc) + timedelta(hours=10)
        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.60)
            bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="m1").first()
            market = self._market(s, td)
            rm = _risk_manager(s)

            decision, _ = rm.check_time_decay(bet, current_price=0.45, market=market)
            assert decision is False, "kapanisa uzaksa time-decay False olmali"


class TestExitChain:
    def test_exit_position_returns_paper_sell(self):
        """exit_position paper modda sell emri dondurur (bet kapatma scheduler'da)."""
        from datetime import datetime, timedelta, timezone

        from executor.bet_placer import BetPlacer

        with __import__("database.db", fromlist=["get_session"]).get_session() as s:
            _add_portfolio(s)
            _add_bet(s, "m1", entry_price=0.60)
            from database.models import Bet, WeatherMarket

            bet = s.query(Bet).filter_by(market_id="m1").first()
            s.add(
                WeatherMarket(
                    id="m1",
                    question="T?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=25.0,
                    target_date=datetime.now(timezone.utc) + timedelta(hours=2),
                    latitude=41.0,
                    longitude=29.0,
                    market_type="HIGH",
                    yes_price=0.40,
                    no_price=0.60,
                    status="open",
                )
            )
            s.commit()

            bp = BetPlacer()
            result = bp.exit_position(
                market=s.query(WeatherMarket).filter_by(id="m1").first(),
                side=bet.side,
                price=0.40,
                size=bet.amount,
                reason="stop_loss: -33.3%",
            )
            assert result.get("paper") is True, "DRY_RUN modunda paper sell olmali"
            assert result.get("orderID"), "paper sell orderID dondurmeli"


class TestSpreadModeStopLossDisabled:
    """SPREAD modunda stop-loss devre disi: fiyat duserken bet kapanmaz.

    (2026-08-10 kullanici karari): spread longshot'lari resolve'a kadar
    tutulur; kazanc 10-100x, kayip -stake. Backtest stop-loss'suz +$36k.
    """

    def _setup(self, session, strategy):
        from datetime import datetime, timedelta, timezone

        from database.models import Bet, WeatherMarket

        _add_portfolio(session)
        # shares gerekli: run_risk_management exit_shares=bet.shares okur
        _add_bet(session, "m1", entry_price=0.20, amount=2.0)
        bet = session.query(Bet).filter_by(market_id="m1").first()
        bet.shares = 10.0
        bet.stake = 2.0
        session.commit()
        session.add(
            WeatherMarket(
                id="m1",
                question="T?",
                city="Testville",
                city_code="TEST",
                metric="temperature_max",
                threshold=30.0,
                target_date=datetime.now(timezone.utc) + timedelta(hours=4),
                latitude=41.0,
                longitude=29.0,
                market_type="HIGH",
                yes_price=0.10,  # %50 dustu -> stop-loss tetiklenmeli (edge)
                no_price=0.90,
                status="open",
            )
        )
        session.commit()

    def test_spread_mode_stop_loss_not_triggered(self, monkeypatch):
        """spread modunda fiyat %50 dusse bile bet kapanmaz."""
        from config.settings import bot_config
        from database.db import get_session
        from database.models import Bet
        from jobs.scheduler import run_risk_management

        monkeypatch.setattr(bot_config.strategy, "betting_strategy", "spread")
        with get_session() as s:
            self._setup(s, "spread")
            run_risk_management(session=s)
            bet = s.query(Bet).filter_by(market_id="m1").first()
            assert bet.status in (
                "placed",
                "partial_fill",
                "filled",
            ), f"spread modunda stop-loss kapatmamali: status={bet.status}"

    def test_edge_mode_stop_loss_triggers(self, monkeypatch):
        """edge modunda fiyat %50 duserse bet kapanir (eski davranis korunur)."""
        from config.settings import bot_config
        from database.db import get_session
        from database.models import Bet
        from jobs.scheduler import run_risk_management

        monkeypatch.setattr(bot_config.strategy, "betting_strategy", "edge")
        with get_session() as s:
            self._setup(s, "edge")
            run_risk_management(session=s)
            bet = s.query(Bet).filter_by(market_id="m1").first()
            assert bet.status == "closed_early", f"edge modunda stop-loss kapatmali: status={bet.status}"
