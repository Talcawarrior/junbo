"""Faz 3: Analysis, Kelly, Risk, EV tests."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

# Point to a temp DB for fresh tables
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
from config.settings import config as _cfg  # noqa: E402

_cfg.DB_PATH = _db_path

from database.db import init_db  # noqa: E402

init_db()

from config.settings import bot_config, config  # noqa: E402


def test_fee_drag():
    """Test 1: FEE_DRAG must be 0.02."""
    assert config.FEE_DRAG == 0.02, f"FEE_DRAG={config.FEE_DRAG}, expected 0.02"
    assert bot_config.strategy.fee_drag == 0.02, (
        f"strategy.fee_drag={bot_config.strategy.fee_drag}, expected 0.02"
    )
    print("✅ Test 1: FEE_DRAG = 0.02")


def test_ev_with_fee():
    """Test 2: EV = edge - FEE_DRAG in analyze_signal."""
    from engine.strategy import BettingEngine

    be = BettingEngine()
    signal = be.analyze_signal(
        {
            "yes_price": 0.60,
            "city_code": "KLGA",
            "strike_temp": 30,
            "market_type": "HIGH",
        },
        model_prob=0.75,
        side="YES",
    )
    assert signal is not None
    # edge = 0.75 - 0.60 = 0.15, ev = 0.15 - 0.02 = 0.13
    assert abs(signal["edge"] - 0.15) < 0.001, f"edge={signal['edge']}"
    assert abs(signal["ev"] - 0.13) < 0.001, f"ev={signal['ev']}"
    print(f"✅ Test 2: EV={signal['ev']:.4f} (edge={signal['edge']:.4f} - FEE_DRAG)")


def test_kelly_bankroll():
    """Test 3: Calculator reads bankroll from DB."""
    # Set portfolio to $2000
    from database.db import get_session
    from database.models import Analysis, Portfolio, WeatherForecast, WeatherMarket
    from engine.calculator import Calculator

    with get_session() as session:
        pf = session.query(Portfolio).filter(Portfolio.id == 1).first()
        if not pf:
            pf = Portfolio(
                id=1,
                cash_balance=2000.0,
                current_value=2000.0,
                total_value=2000.0,
                initial_value=2000.0,
            )
            session.add(pf)
        else:
            pf.total_value = 2000.0
            pf.cash_balance = 2000.0
        session.commit()

    # Create a market + forecasts (METRIC_MAP already works per Faz 2)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target = now + timedelta(days=2)
    with get_session() as session:
        m = WeatherMarket(
            id="test-faz3-bankroll",
            question="Test bankroll?",
            city="New York",
            city_code="KLGA",
            metric="temperature_max",
            threshold=30.0,
            target_date=target,
            yes_price=0.60,
            no_price=0.40,
            volume=1000,
            status="open",
            latitude=40.71,
            longitude=-74.0,
        )
        session.add(m)
        for src, val in [("gfs_seamless", 32.0), ("ecmwf_ifs025", 31.5)]:
            session.add(
                WeatherForecast(
                    market_id="test-faz3-bankroll",
                    city="New York",
                    lat=40.71,
                    lon=-74.0,
                    target_date=target,
                    metric="temperature_2m_max",
                    source=src,
                    predicted_value=val,
                    model_weight=0.5,
                    fetched_at=now,
                )
            )
        session.commit()

    calc = Calculator()
    orig_min_edge = bot_config.strategy.min_edge
    bot_config.strategy.min_edge = 0.005
    analysis_instance = calc.analyze_market("test-faz3-bankroll")
    bot_config.strategy.min_edge = orig_min_edge

    assert analysis_instance is not None, "Analysis is NULL"

    # Access attributes within session to avoid DetachedInstanceError
    with get_session() as session:
        analysis = (
            session.query(Analysis)
            .filter(Analysis.market_id == "test-faz3-bankroll")
            .first()
        )
        assert analysis is not None, "Analysis not found in DB!"
        rec_amount = analysis.recommended_amount
        assert rec_amount > 0, f"recommended_amount is {rec_amount}!"
        print(
            f"✅ Test 3: recommended_amount=${rec_amount:.2f} (bankroll=$2000, max_bet=$50)"
        )


def test_sia_status():
    """Test 4: SIALoop uses 'won'/'lost' not 'settled'."""
    import inspect

    from engine.strategy import SIALoop

    src = inspect.getsource(SIALoop.analyze_model_performance)
    assert '"won"' in src, "Missing 'won' in status filter"
    assert '"lost"' in src, "Missing 'lost' in status filter"
    assert '"settled"' not in src.replace('"won", "lost"', ""), (
        "'settled' should not be in status filter"
    )
    print("✅ Test 4: SIALoop uses 'won'/'lost' statuses")


def test_sia_brier_input():
    """Test 5: SIALoop uses per-model probability (model_probs), not expected_value."""
    import inspect

    from engine.strategy import SIALoop

    src = inspect.getsource(SIALoop.analyze_model_performance)
    assert "model_probs" in src, "Missing model_probs in Brier calculation"
    # Verify Brier uses _resolve_market_outcome (market resolution), not bet.status
    assert "_resolve_market_outcome" in src, (
        "Brier should use market resolution outcome, not bet.status"
    )
    # Ensure Bet.fair_value is NOT the Brier prediction input
    assert "bet.fair_value" not in src, (
        "Brier should not use bet.fair_value; uses per-model probs from analysis"
    )
    print("✅ Test 5: SIALoop uses per-model probability for Brier score")


def test_ladder_pending():
    """Test 6: Ladder orders start as PENDING."""
    from engine.strategy import BettingEngine

    be = BettingEngine()
    signal = {"market_price": 0.35, "edge": 0.06}
    ladder = be.create_ladder_orders(signal, 30.0)
    assert len(ladder) == 3, f"Expected 3 levels, got {len(ladder)}"
    for lvl in ladder:
        assert lvl["status"] == "pending", (
            f"Level {lvl['level']} status is '{lvl['status']}', expected 'pending'"
        )
        assert "filled_at" in lvl, f"Level {lvl['level']} missing 'filled_at'"
    print(
        f"✅ Test 6: Ladder pending OK — {ladder[0]['price']}, {ladder[1]['price']}, {ladder[2]['price']}"
    )


def test_exposure_query():
    """Test 7: RiskManager.get_total_exposure uses Bet.amount."""
    import inspect

    from engine.strategy import RiskManager

    src = inspect.getsource(RiskManager.get_total_exposure)
    assert "Bet.amount" in src, "Missing Bet.amount in exposure query"
    print("✅ Test 7: RiskManager uses Bet.amount for exposure")


def test_risk_manager_init():
    """Test 8: RiskManager initializes without error."""
    from engine.strategy import RiskManager

    rm = RiskManager()
    assert rm.portfolio_value > 0
    print(f"✅ Test 8: RiskManager initialized, portfolio=${rm.portfolio_value}")


def test_betting_engine_ev_full():
    """Test 9: Full EV pipeline with fee."""
    from engine.strategy import BettingEngine

    orig_min_edge = bot_config.strategy.min_edge
    bot_config.strategy.min_edge = 0.15
    try:
        be = BettingEngine()

        # Test with edge above min_edge (0.15 from config)
        s1 = be.analyze_signal(
            {"yes_price": 0.70, "city_code": "KLGA"},
            model_prob=0.86,
            side="YES",
        )
        # edge=0.16, ev=0.14 → eligible (ev>0, edge>=min_edge=0.15)
        assert s1 is not None, "Should be eligible"
        assert s1["ev"] > 0, f"EV={s1['ev']}, expected positive"

        # Test with edge below min_edge (0.15 from config)
        s2 = be.analyze_signal(
            {"yes_price": 0.70, "city_code": "KLGA"},
            model_prob=0.80,
            side="YES",
        )
        # edge=0.10 < min_edge=0.15 → not eligible
        assert s2 is None, "Should NOT be eligible (edge < 0.15)"
        print(
            f"✅ Test 9: EV pipeline OK — eligible edge={s1['edge']}->ev={s1['ev']}, rejected edge=0.10->ev=0.08"
        )
    finally:
        bot_config.strategy.min_edge = orig_min_edge


if __name__ == "__main__":
    test_fee_drag()
    test_ev_with_fee()
    test_kelly_bankroll()
    test_sia_status()
    test_sia_brier_input()
    test_ladder_pending()
    test_exposure_query()
    test_risk_manager_init()
    test_betting_engine_ev_full()
    print("\n" + "=" * 50)
    print("ALL FAZ 3 TESTS PASSED ✅")
    print("=" * 50)
