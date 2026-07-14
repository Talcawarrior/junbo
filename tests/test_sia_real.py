"""Real SIA tests: per-model Brier differentiation, weight divergence,
insufficient-data freeze, fair_value persistence, and YES-probability Brier.

Each test uses its own temp DB with a fresh engine.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_TODAY = datetime.now(timezone.utc)
_YESTERDAY = _TODAY - timedelta(days=1)
_TWO_DAYS_AGO = _TODAY - timedelta(days=2)


def _fresh_db():
    """Override config + force-reimport database.db so engine points to a temp DB.

    Restores original config values in cleanup to avoid polluting other tests.
    Returns (db_path, cleanup_fn).
    """
    import config.settings as cfg_mod

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    # Save original config state for cleanup
    _orig_db_path = cfg_mod.config.DB_PATH
    _orig_weights = dict(cfg_mod.config.MODEL_WEIGHTS)

    cfg_mod.config.DB_PATH = _db_path
    cfg_mod.config.MODEL_WEIGHTS = {
        "model_a": 0.50,
        "model_b": 0.30,
        "model_c": 0.20,
    }

    # Discard cached database.db module so it re-imports with new DB_PATH
    sys.modules.pop("database.db", None)
    sys.modules.pop("database.models", None)

    from database.db import init_db

    init_db()

    def cleanup():
        # Restore global config state
        cfg_mod.config.DB_PATH = _orig_db_path
        cfg_mod.config.MODEL_WEIGHTS = _orig_weights
        # Dispose engine so temp file can be deleted
        import database.db as ddb

        ddb.engine.dispose()
        try:
            os.unlink(_db_path)
        except PermissionError:
            pass  # safe to ignore on Windows
        sys.modules.pop("database.db", None)
        sys.modules.pop("database.models", None)

    return _db_path, cleanup


def _ensure_portfolio(session):
    from database.models import Portfolio

    p = session.query(Portfolio).filter(Portfolio.id == 1).first()
    if not p:
        p = Portfolio(id=1, cash_balance=1000.0, total_value=1000.0)
        session.add(p)
        session.commit()


def _build_market(session, market_id="test-mkt-001", raw_outcome="YES"):
    from database.models import WeatherMarket

    m = WeatherMarket(
        id=market_id,
        question="Will it be hot?",
        city="Miami",
        city_code="KMIA",
        threshold=30.0,
        target_date=_TODAY,
        yes_price=0.6,
        no_price=0.4,
        volume=1000,
        liquidity=500,
        status="settled_win" if raw_outcome == "YES" else "settled_loss",
        first_seen=_TWO_DAYS_AGO,
        last_updated=_YESTERDAY,
        raw_data=json.dumps(
            {
                "source": "polymarket",
                "outcome": raw_outcome,
                "outcomePrices": [1.0, 0.0],
                "settled_at": _YESTERDAY.isoformat(),
            }
        ),
    )
    session.add(m)
    session.commit()
    return m


def _build_analysis(session, market_id, model_probs: dict, est_prob=0.7):
    from database.models import Analysis

    a = Analysis(
        market_id=market_id,
        estimated_probability=est_prob,
        market_implied_prob=0.5,
        edge=est_prob - 0.5,
        avg_forecast_value=32.0,
        std_forecast_value=2.0,
        num_sources=len(model_probs),
        recommended_side="YES" if est_prob >= 0.5 else "NO",
        should_bet=True,
        reason="test",
        model_predictions=json.dumps(
            {
                "model_temps": {mn: 32.0 for mn in model_probs},
                "model_probs": model_probs,
            }
        ),
    )
    session.add(a)
    session.commit()
    return a


def _build_bet(
    session, market_id, analysis_id, side="YES", status="won", fair_value=0.7
):
    from database.models import Bet

    b = Bet(
        market_id=market_id,
        analysis_id=analysis_id,
        city="Miami",
        city_code="KMIA",
        side=side,
        amount=50.0,
        price=0.6,
        entry_price=0.6,
        shares=83.33,
        fair_value=fair_value,
        status=status,
        settled_at=_YESTERDAY,
    )
    session.add(b)
    session.commit()
    return b


def _setup_cycle(session, n=20, market_outcome="YES", bet_side="YES", prefix="mkt"):
    """Create settled bets. model_a gets good probs, model_b bad ones."""
    analysis_id = None
    for i in range(n):
        mkt_id = f"{prefix}-{i:03d}"
        _build_market(session, mkt_id, raw_outcome=market_outcome)

        good_prob = 0.90 if market_outcome == "YES" else 0.10
        bad_prob = 0.55 if market_outcome == "YES" else 0.45

        model_probs = {
            "model_a": good_prob,
            "model_b": bad_prob,
            "model_c": 0.70 if market_outcome == "YES" else 0.30,
        }
        a = _build_analysis(session, mkt_id, model_probs, est_prob=good_prob)
        analysis_id = a.id
        won = (bet_side == "YES" and market_outcome == "YES") or (
            bet_side == "NO" and market_outcome == "NO"
        )
        _build_bet(
            session,
            mkt_id,
            a.id,
            side=bet_side,
            status="won" if won else "lost",
            fair_value=good_prob,
        )
    return analysis_id


class TestSIAReal:
    """5 tests verifying SIA actually differentiates model performance."""

    def _make_sia(self):
        """Create SIALoop with mocked weights (no disk interference).

        save_weights and save_strategy_params are patched at the
        *module level* so optimize_weights cannot touch real files.
        The caller must NOT exit the patch context before calling
        optimize_weights.
        """
        from database.db import get_db_session_factory
        from engine.strategy import SIALoop

        sia = SIALoop(db_session_factory=get_db_session_factory())
        return sia

    def test_models_get_different_briers(self):
        """model_a (accurate) < model_b (inaccurate) Brier, diff > 0.05."""
        _db_path, cleanup = _fresh_db()
        try:
            from database.db import get_session

            with get_session() as session:
                _ensure_portfolio(session)
                _setup_cycle(
                    session,
                    n=20,
                    market_outcome="YES",
                    bet_side="YES",
                    prefix="brier-diff",
                )

            sia = self._make_sia()
            perf = sia.analyze_model_performance(days=365)

            assert "model_a" in perf, "model_a missing"
            assert "model_b" in perf, "model_b missing"
            assert "model_c" in perf, "model_c missing"

            brier_a = perf["model_a"]["brier_score"]
            brier_b = perf["model_b"]["brier_score"]

            assert brier_a < brier_b, (
                f"model_a Brier ({brier_a}) should be < model_b Brier ({brier_b})"
            )
            assert brier_b - brier_a > 0.05, (
                f"Brier diff too small: {brier_b} - {brier_a} = {brier_b - brier_a}"
            )
            assert perf["model_a"]["num_predictions"] >= 20
            assert perf["model_b"]["num_predictions"] >= 20
        finally:
            cleanup()

    def test_weights_diverge(self):
        """Good model gets higher weight than bad; sum=1.0."""
        _db_path, cleanup = _fresh_db()
        try:
            from database.db import get_session

            with get_session() as session:
                _ensure_portfolio(session)
                _setup_cycle(
                    session,
                    n=20,
                    market_outcome="YES",
                    bet_side="YES",
                    prefix="weights-div",
                )

            sia = self._make_sia()
            perf = sia.analyze_model_performance(days=365)
            with (
                patch("engine.strategy.save_weights"),
                patch("engine.strategy.save_strategy_params"),
            ):
                new_weights = sia.optimize_weights(perf)

            weight_a = new_weights.get("model_a", 0.0)
            weight_b = new_weights.get("model_b", 0.0)
            weight_c = new_weights.get("model_c", 0.0)

            assert weight_a > weight_b, (
                f"model_a weight ({weight_a}) should > model_b weight ({weight_b})"
            )
            total = weight_a + weight_b + weight_c
            assert abs(total - 1.0) < 0.001, f"Weights sum to {total}, expected ~1.0"
        finally:
            cleanup()

    def test_insufficient_data_freezes_weight(self):
        """Model with <10 predictions keeps its original weight."""
        _db_path, cleanup = _fresh_db()
        try:
            from config.settings import config

            original = dict(config.MODEL_WEIGHTS)

            from database.db import get_session

            with get_session() as session:
                _ensure_portfolio(session)
                _build_market(session, "mkt-frozen-001", raw_outcome="YES")
                a = _build_analysis(
                    session,
                    "mkt-frozen-001",
                    {
                        "model_a": 0.90,
                        "model_b": 0.55,
                        "model_c": 0.70,
                    },
                )
                _build_bet(session, "mkt-frozen-001", a.id, side="YES", status="won")

            sia = self._make_sia()
            perf = sia.analyze_model_performance(days=365)

            for model_name in original:
                assert perf[model_name]["frozen"], (
                    f"{model_name} should be frozen with <10 predictions"
                )
                assert perf[model_name]["num_predictions"] == 1

            with (
                patch("engine.strategy.save_weights"),
                patch("engine.strategy.save_strategy_params"),
            ):
                new_weights = sia.optimize_weights(perf)
            for model_name in original:
                assert abs(new_weights[model_name] - original[model_name]) < 0.001, (
                    f"{model_name} weight changed from {original[model_name]} "
                    f"to {new_weights[model_name]} despite frozen"
                )
        finally:
            cleanup()

    def test_fair_value_persisted(self):
        """Bet.fair_value from place_bet == analysis estimated_probability."""
        _db_path, cleanup = _fresh_db()
        try:
            from database.db import get_session
            from database.models import Bet

            with get_session() as session:
                _ensure_portfolio(session)
                _build_market(session, "mkt-fv-001", raw_outcome="YES")
                a = _build_analysis(
                    session,
                    "mkt-fv-001",
                    {
                        "model_a": 0.85,
                    },
                    est_prob=0.85,
                )
                b = _build_bet(
                    session,
                    "mkt-fv-001",
                    a.id,
                    side="YES",
                    status="won",
                    fair_value=0.85,
                )

                saved = session.query(Bet).filter(Bet.id == b.id).first()
                assert saved is not None
                fv = float(saved.fair_value or 0.0)
                assert fv > 0.5, f"fair_value={fv}, expected > 0.5"
                assert abs(fv - 0.85) < 0.01, f"fair_value={fv}, expected ~0.85"
        finally:
            cleanup()

    def test_brier_uses_yes_probability(self):
        """Brier is P(YES) vs market resolution outcome, not bet.status."""
        _db_path, cleanup = _fresh_db()
        try:
            from database.db import get_session

            with get_session() as session:
                _ensure_portfolio(session)
                _build_market(session, "mkt-brier-001", raw_outcome="YES")
                a = _build_analysis(
                    session,
                    "mkt-brier-001",
                    {
                        "model_a": 0.90,
                        "model_b": 0.55,
                        "model_c": 0.70,
                    },
                    est_prob=0.90,
                )
                _build_bet(
                    session,
                    "mkt-brier-001",
                    a.id,
                    side="NO",
                    status="lost",
                    fair_value=0.90,
                )
                for i in range(9):
                    mkt_id = f"mkt-brier-extra-{i:03d}"
                    _build_market(session, mkt_id, raw_outcome="YES")
                    a2 = _build_analysis(
                        session,
                        mkt_id,
                        {
                            "model_a": 0.90,
                            "model_b": 0.55,
                            "model_c": 0.70,
                        },
                        est_prob=0.90,
                    )
                    _build_bet(session, mkt_id, a2.id, side="NO", status="lost")

            sia = self._make_sia()
            sia.model_weights = {"model_a": 0.5, "model_b": 0.3, "model_c": 0.2}
            perf = sia.analyze_model_performance(days=365)

            # model_a: P(YES)=0.9 vs outcome YES=1.0 → Brier = (0.9-1.0)² = 0.01
            brier_a = perf["model_a"]["brier_score"]
            expected_a = 0.01
            assert abs(brier_a - expected_a) < 0.005, (
                f"model_a Brier={brier_a}, expected ~{expected_a} (P(YES)=0.9 vs outcome=YES=1.0)"
            )

            # model_b: P(YES)=0.55 vs outcome YES=1.0 → Brier = (0.55-1.0)² = 0.2025
            brier_b = perf["model_b"]["brier_score"]
            expected_b = 0.2025
            assert abs(brier_b - expected_b) < 0.01, (
                f"model_b Brier={brier_b}, expected ~{expected_b}"
            )
        finally:
            cleanup()
