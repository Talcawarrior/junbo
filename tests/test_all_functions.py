"""Comprehensive tests for all critical functions.

Covers: accounting (debit/credit), bet_placer, scheduler, API endpoints.
All tests use in-memory DB — production DB'ye dokunulmaz.
"""

import pytest
from datetime import datetime, timedelta, timezone


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, WeatherMarket, WeatherForecast, Portfolio, Analysis, MarketSnapshot

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, WeatherMarket, Portfolio, Analysis, MarketSnapshot]:
            try:
                s.query(tbl).delete()
            except Exception:
                pass
        s.commit()


def _add_portfolio(session, cash=1000.0):
    from database.models import Portfolio

    session.add(Portfolio(id=1, cash_balance=cash, total_value=cash, initial_value=1000.0))
    session.commit()


def _add_market(session, mid="m1", yes_price=0.50, city="Testville"):
    from database.models import WeatherMarket

    session.add(
        WeatherMarket(
            id=mid,
            question="Test?",
            city=city,
            city_code="TEST",
            metric="temperature_max",
            threshold=25.0,
            target_date=(datetime.now(timezone.utc) + timedelta(days=2)).replace(
                hour=23, minute=59, second=59, microsecond=0
            ),
            yes_price=yes_price,
            no_price=1.0 - yes_price,
            status="open",
            latitude=41.0,
            longitude=29.0,
        )
    )
    session.commit()


# ══════════════════════════════════════════════════════════════════════════
# 1. ACCOUNTING (debit_stake, credit_sale, credit_settlement)
# ══════════════════════════════════════════════════════════════════════════


class TestDebitStake:
    def test_debit_reduces_cash(self):
        from database.db import get_session
        from utils.accounting import debit_stake

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            result = debit_stake(s, 100.0, "test:open")
            assert result == 900.0
            from database.models import Portfolio

            pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
            assert pf.cash_balance == 900.0

    def test_debit_exact_balance(self):
        from database.db import get_session
        from utils.accounting import debit_stake

        with get_session() as s:
            _add_portfolio(s, 100.0)
            result = debit_stake(s, 100.0, "test:exact")
            assert result == 0.0

    def test_debit_exceeds_balance_raises(self):
        from database.db import get_session
        from utils.accounting import debit_stake

        with get_session() as s:
            _add_portfolio(s, 50.0)
            with pytest.raises(ValueError, match="Insufficient cash"):
                debit_stake(s, 100.0, "test:over")

    def test_debit_zero_amount(self):
        from database.db import get_session
        from utils.accounting import debit_stake

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            result = debit_stake(s, 0.0, "test:zero")
            assert result == 1000.0


class TestCreditSale:
    def test_credit_increases_cash(self):
        from database.db import get_session
        from utils.accounting import credit_sale

        with get_session() as s:
            _add_portfolio(s, 100.0)
            result = credit_sale(s, 50.0, "test:sell")
            assert result == 150.0

    def test_credit_zero(self):
        from database.db import get_session
        from utils.accounting import credit_sale

        with get_session() as s:
            _add_portfolio(s, 100.0)
            result = credit_sale(s, 0.0, "test:zero")
            assert result == 100.0


class TestCreditSettlement:
    def test_credit_settlement_increases_cash(self):
        from database.db import get_session
        from utils.accounting import credit_settlement

        with get_session() as s:
            _add_portfolio(s, 100.0)
            result = credit_settlement(s, 200.0, 0.0, "test:settle")
            assert result == 300.0

    def test_credit_settlement_with_fee(self):
        from database.db import get_session
        from utils.accounting import credit_settlement

        with get_session() as s:
            _add_portfolio(s, 100.0)
            result = credit_settlement(s, 200.0, 10.0, "test:fee")
            assert result == 290.0


# ══════════════════════════════════════════════════════════════════════════
# 2. BET_PLACER
# ══════════════════════════════════════════════════════════════════════════


class TestOpenBetOnMarket:
    def test_opens_paper_bet(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m1", 0.50)
            bp = BetPlacer()
            market = (
                s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                .filter_by(id="m1")
                .first()
            )
            result = bp.open_bet_on_market(market, s)
            assert result is not None
            assert result.status == "placed"
            assert result.side == "YES"
            assert result.amount > 0

    def test_rejects_high_price(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m2", 0.96)
            bp = BetPlacer()
            market = (
                s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                .filter_by(id="m2")
                .first()
            )
            result = bp.open_bet_on_market(market, s)
            assert result is None

    def test_rejects_low_price(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m3", 0.05)
            bp = BetPlacer()
            market = (
                s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                .filter_by(id="m3")
                .first()
            )
            result = bp.open_bet_on_market(market, s)
            assert result is None

    def test_rejects_duplicate_market(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m4", 0.50)
            s.add(
                Bet(
                    market_id="m4",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    price=0.50,
                    status="placed",
                )
            )
            s.commit()
            bp = BetPlacer()
            market = (
                s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                .filter_by(id="m4")
                .first()
            )
            result = bp.open_bet_on_market(market, s)
            assert result is None

    def test_rejects_expired_market(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from database.models import WeatherMarket

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            # Past target_date
            past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
            s.add(
                WeatherMarket(
                    id="m5",
                    question="Q?",
                    city="Testville",
                    city_code="TEST",
                    metric="temperature_max",
                    threshold=25.0,
                    target_date=past,
                    yes_price=0.50,
                    no_price=0.50,
                    status="open",
                    latitude=41.0,
                    longitude=29.0,
                )
            )
            s.commit()
            bp = BetPlacer()
            market = s.query(WeatherMarket).filter_by(id="m5").first()
            result = bp.open_bet_on_market(market, s)
            assert result is None

    def test_rejects_reentry_after_recent_stop_loss(self):
        """Stop-loss ile kapanan ayni markete hemen tekrar bet ACILMAMALI.

        Bug: bet_placer.once_2026_08_07 ayni market_id 3374736'ya 2 kez
        girdi — bet 144 SL ile kapandi (14:46), 10 sn sonra bet 186 ayni
        markete yeniden acildi (14:46:29) ve yine SL'ye dustu. Kapali
        betler OPEN_BET_STATUSES'de olmadigi icin normal dedup bunlari
        yakalayamaz; re-entry guard'i olmadan kayip dongusu olusur.
        """
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m7", 0.50)
            # Stop-loss 5 dk once kapanmis bir bet var
            s.add(
                Bet(
                    market_id="m7",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    price=0.50,
                    status="closed_early",
                    close_reason="stop_loss: -54.5%",
                    closed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None),
                )
            )
            s.commit()
            bp = BetPlacer()
            WM = __import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket
            market = s.query(WM).filter_by(id="m7").first()
            result = bp.open_bet_on_market(market, s)
            assert result is None, "stop_loss sonrasi ayni markete yeniden bet acilmamali"

    def test_reopens_new_leader_after_stop_loss(self):
        """Stop-loss ile kaybedilen gruba, ayni grupta daha yuksek fiyatli
        FARKLI market (yeni lider) pencere disinda bile acilmali.

        Bug kaynagi (2026-08-07): SL sonrasi ayni markete tekrar girmek
        yerine grubun yeni liderine gecilmeliydi. Bu test `_reopen_after_stop_loss`
        akisinin, stop_loss kapanisina ragmen ayni gruptaki yeni (yuksek
        fiyatli) farkli marketi actigini dogrular.
        """
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            # Ayni gruba iki market: m9 dusuk fiyatli (kaybedecek), m10 yuksek
            _add_market(s, "m9", 0.60)
            _add_market(s, "m10", 0.80)
            # m9'da son 1 saatte stop_loss ile kapanmis bet
            s.add(
                Bet(
                    market_id="m9",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    price=0.60,
                    status="closed_early",
                    close_reason="stop_loss: -54.5%",
                    closed_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None),
                )
            )
            s.commit()
            bp = BetPlacer()
            reopened = bp._reopen_after_stop_loss(s)
            assert reopened == 1, f"yeni lider acilmali, acilan={reopened}"
            existing = (
                s.query(Bet).filter(Bet.market_id == "m10", Bet.status.in_(("active", "placed", "pending"))).first()
            )
            assert existing is not None, "yeni lider m10'a bet acilmali"

    def test_allows_reentry_after_old_non_loss_close(self):
        """Eski (guard penceresi disinda) kapanmalar re-entry'i engellememeli."""
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from database.models import Bet, WeatherMarket

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m8", 0.50)
            # 7 gun once 'rotation' ile kapanmis bet (guard 6 saat, disinda)
            s.add(
                Bet(
                    market_id="m8",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=10.0,
                    price=0.50,
                    status="closed",
                    close_reason="rotation",
                    closed_at=(datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None),
                )
            )
            s.commit()
            bp = BetPlacer()
            market = s.query(WeatherMarket).filter_by(id="m8").first()
            result = bp.open_bet_on_market(market, s)
            assert result is not None

    def test_caps_amount_to_remaining_room(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m6", 0.50)
            # 950 exposure already
            for i in range(95):
                s.add(
                    Bet(
                        market_id=f"ex_{i}",
                        city="Testville",
                        city_code="TEST",
                        side="YES",
                        amount=10.0,
                        price=0.50,
                        status="placed",
                    )
                )
            s.commit()
            bp = BetPlacer()
            market = (
                s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                .filter_by(id="m6")
                .first()
            )
            result = bp.open_bet_on_market(market, s)
            # flat_bet=1000 but only 50 room → capped to 50
            if result is not None:
                assert result.amount <= 50.0, f"Amount {result.amount} should be capped to remaining room"

    def test_dry_run_does_not_call_api(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from config.settings import Config

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m7", 0.50)
            original = Config.DRY_RUN
            Config.DRY_RUN = True
            try:
                bp = BetPlacer()
                market = (
                    s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                    .filter_by(id="m7")
                    .first()
                )
                result = bp.open_bet_on_market(market, s)
                assert result is not None
                assert "paper" in (result.order_id or "").lower()
            finally:
                Config.DRY_RUN = original


class TestCloseBetForRotation:
    def test_rotation_close_updates_status(self):
        from database.db import get_session
        from executor.bet_placer import BetPlacer

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m8", 0.50)
            bp = BetPlacer()
            market = (
                s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                .filter_by(id="m8")
                .first()
            )
            bet = bp.open_bet_on_market(market, s)
            assert bet is not None
            bp.close_bet_for_rotation(bet, 0.55, s)
            assert bet.status == "closed"
            assert bet.close_reason == "rotation"
            assert bet.closed_at is not None
            assert bet.realized_pnl is not None


# ══════════════════════════════════════════════════════════════════════════
# 3. SCHEDULER (run_risk_management, run_update_prices)
# ══════════════════════════════════════════════════════════════════════════


class TestRunRiskManagement:
    def test_no_open_bets_returns_message(self):
        from jobs.scheduler import run_risk_management

        result = run_risk_management()
        assert "no open positions" in result

    def test_updates_unrealized_pnl(self):
        from database.db import get_session
        from jobs.scheduler import run_risk_management
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m9", 0.50)
            s.add(
                Bet(
                    market_id="m9",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=100.0,
                    price=0.50,
                    shares=200.0,
                    entry_price=0.50,
                    entry_fee=0.40,
                    status="placed",
                )
            )
            s.commit()
            result = run_risk_management(session=s)
            assert "no open positions" not in result
            bet = s.query(Bet).filter_by(market_id="m9").first()
            assert bet.unrealized_pnl is not None

    def test_stop_loss_closes_position(self):
        """Fiyat %stop_loss_pct'den fazla duserse bet closed_early + close_reason alir."""
        from database.db import get_session
        from jobs.scheduler import run_risk_management
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m10", yes_price=0.05)  # yes_price 0.50 -> 0.05 = %90 dusus
            s.add(
                Bet(
                    market_id="m10",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=100.0,
                    price=0.50,
                    shares=200.0,
                    entry_price=0.50,
                    entry_fee=0.40,
                    status="placed",
                )
            )
            s.commit()
            run_risk_management(session=s)
            bet = s.query(Bet).filter_by(market_id="m10").first()
            assert bet.status == "closed_early"
            assert bet.close_reason and "stop_loss" in bet.close_reason
            assert bet.closed_at is not None

    def test_stop_loss_holds_when_price_ok(self):
        """Fiyat makul seviyedeyse bet acik kalir."""
        from database.db import get_session
        from jobs.scheduler import run_risk_management
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m11", yes_price=0.48)  # %4 dusus < %20 stop-loss
            s.add(
                Bet(
                    market_id="m11",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=100.0,
                    price=0.50,
                    shares=200.0,
                    entry_price=0.50,
                    entry_fee=0.40,
                    status="placed",
                )
            )
            s.commit()
            run_risk_management(session=s)
            bet = s.query(Bet).filter_by(market_id="m11").first()
            assert bet.status == "placed"


class TestRunUpdatePrices:
    def test_updates_current_price(self):
        from database.db import get_session
        from jobs.scheduler import run_update_prices
        from database.models import Bet

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "m10", 0.60)
            s.add(
                Bet(
                    market_id="m10",
                    city="Testville",
                    city_code="TEST",
                    side="YES",
                    amount=100.0,
                    price=0.50,
                    shares=200.0,
                    entry_price=0.50,
                    entry_fee=0.40,
                    status="placed",
                )
            )
            s.commit()
            run_update_prices(session=s)
            bet = s.query(Bet).filter_by(market_id="m10").first()
            assert bet.current_price == 0.60


# ══════════════════════════════════════════════════════════════════════════
# 4. API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════


class TestApiStatus:
    def test_status_returns_portfolio(self):
        from fastapi.testclient import TestClient
        from api import app
        from database.db import get_session

        with get_session() as s:
            _add_portfolio(s, 1000.0)
        client = TestClient(app)
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert "portfolio" in data
        p = data["portfolio"]
        assert p["initial"] == 1000.0
        assert "realized_pnl" in p
        assert "unrealized_pnl" in p
        assert "total_pnl" in p
        assert "total_entry_fee" in p
        assert "gercek_kayip" in p

    def test_status_includes_stats(self):
        from fastapi.testclient import TestClient
        from api import app
        from database.db import get_session

        with get_session() as s:
            _add_portfolio(s, 1000.0)
        client = TestClient(app)
        r = client.get("/api/status")
        data = r.json()
        assert "stats" in data
        assert "total_signals" in data["stats"]
        assert "total_bets" in data["stats"]


class TestApiHealth:
    def test_health_endpoint(self):
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        r = client.get("/api/health-check")
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# 5. FORMULAS
# ══════════════════════════════════════════════════════════════════════════


class TestFormulas:
    def test_unrealized_pnl_with_fee(self):
        from utils.formulas import unrealized_pnl

        assert abs(unrealized_pnl(200, 0.60, 0.50) - 20.0) < 0.01

    def test_unrealized_pnl_loss(self):
        from utils.formulas import unrealized_pnl

        assert abs(unrealized_pnl(200, 0.40, 0.50) - (-20.0)) < 0.01

    def test_unrealized_pnl_zero(self):
        from utils.formulas import unrealized_pnl

        assert unrealized_pnl(200, 0.50, 0.50) == 0.0

    def test_bet_shares(self):
        from utils.formulas import bet_shares

        assert bet_shares(10.0, 0.50) == 20.0

    def test_portfolio_total_value(self):
        from utils.formulas import portfolio_total_value

        assert portfolio_total_value(500.0, 300.0) == 800.0

    def test_max_exposure_cap(self):
        from utils.formulas import max_exposure_cap

        assert max_exposure_cap(1000.0, 0.0, 0.6) == 600.0
        assert max_exposure_cap(1000.0, -100.0, 0.6) == 540.0

    def test_polymarket_fee(self):
        from utils.formulas import polymarket_fee

        fee = polymarket_fee(100.0, 0.50, 0.05)
        assert fee > 0

    def test_roi_pct(self):
        from utils.formulas import roi_pct

        assert roi_pct(10.0, 100.0) == 10.0
        assert roi_pct(-5.0, 100.0) == -5.0

    def test_settlement_pnl_won(self):
        from utils.formulas import settlement_pnl

        pnl = settlement_pnl(10.0, 0.50, 0.40, True)
        assert pnl > 0  # Won: payout > stake

    def test_settlement_pnl_lost(self):
        from utils.formulas import settlement_pnl

        pnl = settlement_pnl(10.0, 0.50, 0.40, False)
        assert abs(pnl - (-10.4)) < 0.01  # Lost: -(stake + entry_fee)


# ══════════════════════════════════════════════════════════════════════════
# 6. DRY_RUN INTEGRATION
# ══════════════════════════════════════════════════════════════════════════


class TestDryRunIntegration:
    def test_full_dry_run_cycle(self):
        """Open → price update → close rotation — all in DRY_RUN."""
        from database.db import get_session
        from executor.bet_placer import BetPlacer
        from jobs.scheduler import run_update_prices
        from config.settings import Config

        with get_session() as s:
            _add_portfolio(s, 1000.0)
            _add_market(s, "dry1", 0.50)
            original = Config.DRY_RUN
            Config.DRY_RUN = True
            try:
                bp = BetPlacer()
                market = (
                    s.query(__import__("database.models", fromlist=["WeatherMarket"]).WeatherMarket)
                    .filter_by(id="dry1")
                    .first()
                )
                bet = bp.open_bet_on_market(market, s)
                assert bet is not None
                assert bet.status == "placed"

                # Price moves up
                market.yes_price = 0.70
                s.commit()
                run_update_prices(session=s)

                bet = s.query(__import__("database.models", fromlist=["Bet"]).Bet).filter_by(market_id="dry1").first()
                assert bet.current_price == 0.70
                assert bet.unrealized_pnl > 0

                # Close via rotation
                bp.close_bet_for_rotation(bet, 0.70, s)
                assert bet.status == "closed"
                assert bet.realized_pnl is not None
            finally:
                Config.DRY_RUN = original
