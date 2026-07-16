"""FastAPI application for Junbo - Polymarket weather betting bot.

Provides REST API endpoints for status, markets, signals, history, cleanup,
and WebSocket push. The bot runs fetch -> parse -> forecast -> analyze ->
place -> settle cycles at configurable intervals.
"""

# pylint: disable=E1102,E1111  # SQLAlchemy func.* false positives

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from asi_engine.calibration_engine import CalibrationEngine
from asi_engine.data_backfiller import DataBackfiller

# ASI Engine imports (for ASI-Evolve dashboard endpoints)
from asi_engine.orchestrator import JunboOrchestrator
from config.logging_config import setup_logging

# Package Imports
from config.settings import bot_config, config

from database.db import (
    ensure_initial_portfolio,
    get_db_session,
    get_db_session_factory,
    init_db,
)
from database.models import OPEN_BET_STATUSES, Analysis, Bet, Portfolio, WeatherMarket
from engine.calculator import WeatherEngine
from engine.strategy import BettingEngine, RiskManager, SIALoop
from executor.settler import SettlementEngine
from scrapers.polymarket import PolymarketScraper
from utils.formulas import max_exposure_cap, portfolio_current_value, roi_pct, win_rate_pct, pnl_ratio
from utils.price_sanity import safe_ev
from utils.weights_store import load_weights

setup_logging()
logger = logging.getLogger(__name__)


# ── API Key Authentication ──────────────────────────────────────────────────
# Protects sensitive POST endpoints (reset, asi/*, start, stop, cleanup).
# JUNBO_API_KEY MUST be set. If not set, a random key is generated at startup
# and printed to console. Destructive endpoints are NEVER open.

import secrets

API_KEY = os.getenv("JUNBO_API_KEY", "")
if not API_KEY:
    API_KEY = secrets.token_urlsafe(32)
    print(f"\n{'='*60}")
    print(f"WARNING: JUNBO_API_KEY not set. Generated random key:")
    print(f"  {API_KEY}")
    print(f"Add to .env: JUNBO_API_KEY={API_KEY}")
    print(f"{'='*60}\n")


async def verify_api_key(x_api_key: str = Header(default="")):
    """FastAPI dependency: verify X-API-Key header for protected endpoints.

    NEVER bypassed. If API_KEY is empty (shouldn't happen), still rejects.
    """
    if x_api_key != API_KEY:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Global State tracking for FastAPI Web App ───────────────────────────────
class BotState:
    """Global bot state tracking running status, modules, and tasks."""

    def __init__(self):
        self.is_running = False
        self.locked = False
        self.lock_reason = None
        self.last_scan = None
        self.total_signals = 0
        self.total_bets = 0
        self.websocket_clients: list[WebSocket] = []
        self.tasks = {}
        self.start_stop_lock = asyncio.Lock()

        # Config reference
        self.config = config
        self.db_session_factory = None
        self.data_fetcher = None
        self.weather_engine = None
        self.risk_manager = None
        self.betting_engine = None
        self.settlement_engine = None
        self.sia_loop = None
        self.sia_last_run = None  # datetime of last SIA optimization
        self.sia_interval_hours = bot_config.sia_interval // 3600

        # ASI-Evolve engines
        self.orchestrator = None
        self.backfiller = None
        self.calibration_engine = None

    def initialize_modules(self):
        """Initialize all modular components."""
        self.db_session_factory = get_db_session_factory()
        self.data_fetcher = PolymarketScraper()
        self.weather_engine = WeatherEngine(self.db_session_factory, self.config)
        self.risk_manager = RiskManager(None, self.config)
        self.betting_engine = BettingEngine(None, self.risk_manager, self.weather_engine)
        self.settlement_engine = SettlementEngine()
        self.sia_loop = SIALoop(self.db_session_factory, self.config)

        # ASI-Evolve engines
        self.orchestrator = JunboOrchestrator()
        self.backfiller = DataBackfiller()
        self.calibration_engine = CalibrationEngine()


state = BotState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Lifespan context manager for startup and shutdown."""
    logger.info("Junbo Weather Prediction Bot starting...")

    # Startup'ta DB backup al (API ile başlatılsa bile)
    try:
        from db_backup import create_backup
        create_backup("startup")
    except Exception as e:
        logger.warning("Startup backup warning: %s", e)

    init_db()
    state.initialize_modules()

    # Ensure initial portfolio row exists in DB
    try:
        ensure_initial_portfolio()
    except Exception as e:
        logger.warning("Portfolio init warning: %s", e)

    logger.info("Database and all modules ready.")
    logger.info("Junbo Weather Prediction Bot v1.0")
    yield

    # Shutdown
    logger.info("Bot shutting down...")
    if state.tasks:
        for task in list(state.tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*state.tasks.values(), return_exceptions=True)
        state.tasks.clear()


app = FastAPI(title="âš¡ Junbo - Self-Evolving Predictor", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8093", "http://127.0.0.1:8093"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


async def broadcast_message(message: dict):
    """Broadcast message to all connected WebSocket clients."""
    if not state.websocket_clients:
        return
    disconnected = []
    for client in state.websocket_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        if client in state.websocket_clients:
            state.websocket_clients.remove(client)


@app.get("/")
async def root():
    """Serve Next.js Dashboard"""
    _base = os.path.dirname(os.path.abspath(__file__))
    _out = os.path.join(_base, "out")
    _dash = os.path.join(_base, "dashboard", "out")
    dashboard_out = _out if os.path.isdir(_out) else _dash
    index_path = os.path.join(dashboard_out, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Dashboard yukleniyor... Build gerekli: cd dashboard && npx next build</h1>")


@app.get("/api/status")
def get_status():
    """Get bot status and metrics with strict accounting."""
    from sqlalchemy import func

    from database.models import Analysis, Bet

    db = get_db_session()
    try:
        db.query(Portfolio).filter(Portfolio.id == 1).first()

        # 1. Realized PnL (Closed bets)
        from datetime import datetime, timezone

        _ts = datetime.now(timezone.utc).replace(tzinfo=None)
        _today_start = _ts.replace(hour=0, minute=0, second=0, microsecond=0)
        # All closed bet statuses (settled + bot-closed early exits)
        _closed_statuses = ("won", "lost", "settled", "closed_early")
        from sqlalchemy import or_

        daily_pnl = (
            db.query(func.coalesce(func.sum(Bet.pnl), 0.0))
            .filter(
                Bet.status.in_(_closed_statuses),
                or_(Bet.settled_at >= _today_start, Bet.closed_at >= _today_start),
            )
            .scalar()
        ) or 0.0

        realized_pnl_db = (
            db.query(func.coalesce(func.sum(Bet.pnl), 0.0)).filter(Bet.status.in_(_closed_statuses)).scalar()
        ) or 0.0

        # 2. Unrealized PnL (Open bets)
        open_statuses = OPEN_BET_STATUSES
        unrealized_pnl_db = (
            db.query(func.coalesce(func.sum(Bet.unrealized_pnl), 0.0)).filter(Bet.status.in_(open_statuses)).scalar()
        ) or 0.0

        # 3. Counts â€” win/loss based on PnL (includes closed_early)
        _all_closed = db.query(Bet.pnl).filter(Bet.status.in_(_closed_statuses)).all()
        win_count = sum(1 for b in _all_closed if (b.pnl or 0) > 0)
        loss_count = sum(1 for b in _all_closed if (b.pnl or 0) <= 0)
        total_bets_db = db.query(Bet).filter(Bet.status.in_(open_statuses)).count()
        total_signals_db = db.query(Analysis).filter(Analysis.should_bet.is_(True)).count()

        # Open positions with details for frontend
        open_bets = db.query(Bet).filter(Bet.status.in_(open_statuses)).all()
        open_positions = []
        for bet in open_bets:
            wm = db.query(WeatherMarket).filter(WeatherMarket.id == bet.market_id).first()
            open_positions.append(
                {
                    "id": str(bet.id),
                    "city": bet.city,
                    "side": bet.side,
                    "entry_price": float(bet.entry_price or 0),
                    "current_price": float(bet.current_price or bet.entry_price or 0),
                    "unrealized_pnl": float(bet.unrealized_pnl or 0),
                    "edge": float(bet.expected_value or 0) * 100,
                    "shares": float(bet.shares or 0),
                    "amount": float(bet.amount or 0),
                    "opened_at": bet.placed_at.isoformat() if bet.placed_at else None,
                    "market_id": bet.market_id,
                    "market_type": wm.market_type if wm else None,
                    "threshold": float(wm.threshold) if wm and wm.threshold else None,
                    "question": wm.question if wm else None,
                    "metric": wm.metric if wm else None,
                }
            )

        exposure_db = (
            db.query(func.coalesce(func.sum(Bet.amount), 0.0)).filter(Bet.status.in_(open_statuses)).scalar()
        ) or 0.0

        initial_capital = state.config.INITIAL_PORTFOLIO
        total_pnl = realized_pnl_db + unrealized_pnl_db

        # Total amount staked in settled bets (sum of all bet amounts
        # regardless of win/loss). ROI = PnL / total_stake, NOT PnL / initial.
        total_stake_settled = (
            db.query(func.coalesce(func.sum(Bet.amount), 0.0)).filter(Bet.status.in_(_closed_statuses)).scalar()
        ) or 0.0

        # Realized PnL from bets closed BEFORE today (for daily exposure cap)
        realized_before_today = (
            db.query(func.coalesce(func.sum(Bet.pnl), 0.0))
            .filter(
                Bet.status.in_(_closed_statuses),
                or_(
                    Bet.settled_at < _today_start,
                    Bet.closed_at < _today_start,
                ),
            )
            .scalar()
        ) or 0.0

        # ROI for CLOSED bets: realized PNL / total stake (bet amounts)
        total_roi = roi_pct(realized_pnl_db, total_stake_settled)
        # Daily ROI: daily realized PNL / total stake settled today
        total_stake_today = (
            db.query(func.coalesce(func.sum(Bet.amount), 0.0))
            .filter(
                Bet.status.in_(_closed_statuses),
                or_(Bet.settled_at >= _today_start, Bet.closed_at >= _today_start),
            )
            .scalar()
        ) or 0.0
        daily_roi = roi_pct(daily_pnl, total_stake_today)

        # --- Sharpe Ratio & Max Drawdown ---
        import math

        sharpe_ratio = 0.0
        max_drawdown_pct = 0.0
        closed_bets = (
            db.query(Bet.pnl, Bet.settled_at)
            .filter(Bet.status.in_(_closed_statuses))
            .order_by(Bet.settled_at.asc())
            .all()
        )
        if len(closed_bets) > 1:
            pnls = [float(b.pnl or 0.0) for b in closed_bets]
            mean_pnl = sum(pnls) / len(pnls)
            std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls))
            sharpe_ratio = round(mean_pnl / std_pnl, 4) if std_pnl > 0 else 0.0

            # Max Drawdown: simulate portfolio from initial capital
            port_val = float(initial_capital)
            peak = port_val
            for b in closed_bets:
                port_val += float(b.pnl or 0.0)
                if port_val > peak:
                    peak = port_val
                dd = (peak - port_val) / peak * 100 if peak > 0 else 0
                if dd > max_drawdown_pct:
                    max_drawdown_pct = round(dd, 2)

        # Scan loop sağlık kontrolü
        scan_health = "unknown"
        minutes_since_scan = None
        if state.last_scan:
            elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - state.last_scan).total_seconds()
            minutes_since_scan = round(elapsed / 60)
            if elapsed < 900:
                scan_health = "healthy"
            elif elapsed < 1800:
                scan_health = "warning"
            else:
                scan_health = "dead"

        return {
            "is_running": state.is_running,
            "locked": state.locked,
            "scan_health": scan_health,
            "last_scan": state.last_scan.isoformat() if state.last_scan else None,
            "minutes_since_last_scan": minutes_since_scan,
            "portfolio": {
                "initial": initial_capital,
                "current": portfolio_current_value(initial_capital, realized_pnl_db, unrealized_pnl_db),
                "daily_pnl": daily_pnl,
                "daily_roi": daily_roi,
                "unrealized_pnl": float(unrealized_pnl_db),
                "realized_pnl": float(realized_pnl_db),
                "total_pnl": total_pnl,
                "total_roi": total_roi,
                "exposure": float(exposure_db),
                "max_exposure": round(
                    max_exposure_cap(
                        initial_capital,
                        realized_before_today,
                        state.config.TOTAL_EXPOSURE_PCT,
                    ),
                    2,
                ),
            },
            "stats": {
                "total_signals": total_signals_db,
                "total_bets": total_bets_db,
                "win_count": win_count,
                "loss_count": loss_count,
                "total_closed": win_count + loss_count,
                "last_scan": state.last_scan.isoformat() if state.last_scan else None,
            },
            "limits": {
                "max_bet_pct": state.config.MAX_BET_PCT * 100,
                "max_exposure_pct": state.config.TOTAL_EXPOSURE_PCT * 100,
                "daily_stop_loss_pct": state.config.DAILY_LOSS_LIMIT * 100,
                "city_cap": state.config.CITY_CAP,
            },
            "metrics": {
                "sharpe_ratio": sharpe_ratio,
                "max_drawdown_pct": max_drawdown_pct,
            },
            "open_positions": open_positions,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        db.close()


# --- ASI-Evolve Dashboard Endpoints ---


@app.get("/api/asi/weights")
def get_asi_weights():
    """Retrieve current evolved weights with model performance metrics."""
    from database.models import ModelPerformance
    from sqlalchemy import func

    weights = load_weights()
    if not weights:
        weights = config.MODEL_WEIGHTS

    # Get latest performance metrics for each model
    db = get_db_session()
    try:
        # Subquery to get latest record per model
        latest_perf = (
            db.query(
                ModelPerformance.model_name,
                func.max(ModelPerformance.recorded_at).label("max_date"),
            )
            .group_by(ModelPerformance.model_name)
            .subquery()
        )

        perf_records = (
            db.query(ModelPerformance)
            .join(
                latest_perf,
                (ModelPerformance.model_name == latest_perf.c.model_name)
                & (ModelPerformance.recorded_at == latest_perf.c.max_date),
            )
            .all()
        )

        perf_map = {p.model_name: p for p in perf_records}
    finally:
        db.close()

    # Return enriched weights with performance data
    result = {}
    for model, weight in weights.items():
        perf = perf_map.get(model)
        result[model] = {
            "weight": weight,
            "brier_score": perf.brier_score if perf else None,
            "accuracy": perf.accuracy if perf else None,
            "num_predictions": perf.num_predictions if perf else 0,
            "last_updated": perf.recorded_at.isoformat() if perf and perf.recorded_at else None,
        }
    return result


@app.get("/api/asi/cognition")
def get_asi_cognition():
    """Retrieve ASI Cognition Base insights."""
    if not state.orchestrator:
        state.orchestrator = JunboOrchestrator()
    return state.orchestrator.cognition_base.get_all_insights()


@app.post("/api/asi/evolve")
async def run_asi_evolve(_key: str = Depends(verify_api_key)):
    """Run an autonomous evolution pipeline round (5 rounds)."""
    if not state.orchestrator:
        state.orchestrator = JunboOrchestrator()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, state.orchestrator.run_evolution_pipeline, 5)
    return result


@app.post("/api/asi/backfill")
async def run_asi_backfill(days: int = 90, _key: str = Depends(verify_api_key)):
    """Trigger a deep historical weather backfill from Open-Meteo APIs."""
    if not state.backfiller:
        state.backfiller = DataBackfiller()
    loop = asyncio.get_running_loop()
    records_inserted = await loop.run_in_executor(None, state.backfiller.run_deep_backfill, days, 12)
    if state.calibration_engine:
        state.calibration_engine.calculate_biases()
    return {"status": "success", "inserted_records": records_inserted}


@app.get("/api/asi/calibration")
def get_asi_calibration():
    """Retrieve the pre-calculated bias calibration maps for each city."""
    if not state.calibration_engine:
        state.calibration_engine = CalibrationEngine()
    return state.calibration_engine.bias_map


@app.post("/api/asi/calibration/recalculate")
def run_asi_calibration_recalculate(_key: str = Depends(verify_api_key)):
    """Manually recalculate model biases from the historical calibrations table."""
    if not state.calibration_engine:
        state.calibration_engine = CalibrationEngine()
    biases = state.calibration_engine.calculate_biases()
    return {"status": "success", "cities_calibrated": len(biases)}


# --- Standard Endpoints ---


@app.get("/api/markets")
def get_markets():
    """Get all future active weather markets AND missed signals (rejected bets)."""
    from datetime import timedelta

    from database.models import Analysis, Bet, WeatherForecast, WeatherMarket
    from engine.calculator import Calculator

    db = get_db_session()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # 1. Fetch missed signals (should_bet=True but no active bet)
        # These are the "164 - 8 = 156" signals
        missed_signals = (
            db.query(Analysis, WeatherMarket)
            .join(WeatherMarket, Analysis.market_id == WeatherMarket.id)
            .filter(Analysis.should_bet.is_(True))
            .filter(
                ~Analysis.market_id.in_(db.query(Bet.market_id).filter(Bet.status.in_(OPEN_BET_STATUSES)))
            )
            .order_by(Analysis.analyzed_at.desc())
            .all()
        )

        market_list = []
        for analysis, m in missed_signals:
            market_list.append(
                {
                    "id": m.id,
                    "city": m.city,
                    "city_code": "SIGNAL",
                    "date": m.target_date.isoformat() if m.target_date else None,
                    "outcome_type": m.metric or "YES",
                    "strike_temp": float(m.threshold) if m.threshold else 0,
                    "current_yes_bid": float(m.yes_price) if m.yes_price else 0,
                    "current_no_bid": float(m.no_price) if m.no_price else 0,
                    "model_prob": float(analysis.estimated_probability),
                    "edge": float(analysis.edge),
                    "ev": safe_ev(analysis.estimated_probability, m.yes_price or 0.5),
                    "status": "REJECTED (Risk Cap)",
                }
            )

        # 2. Fetch other open markets (today + 7 days)
        upper = now + timedelta(days=7)
        markets = (
            db.query(WeatherMarket)
            .filter(
                WeatherMarket.target_date >= now,
                WeatherMarket.target_date <= upper,
                WeatherMarket.status == "open",
                ~WeatherMarket.id.in_([m["id"] for m in market_list]),
            )
            .limit(100)
            .all()
        )

        calc = Calculator()
        for m in markets:
            if m.yes_price is None or m.threshold is None:
                continue
            current_price = float(m.yes_price)
            model_prob = current_price
            forecasts = (
                db.query(WeatherForecast)
                .filter(WeatherForecast.market_id == m.id)
                .order_by(WeatherForecast.fetched_at.desc())
                .limit(8)
                .all()
            )
            if forecasts:
                latest_vals = [f.predicted_value for f in forecasts]
                days_ahead = max((m.target_date - now).days, 1)
                model_prob = calc.estimate_probability(latest_vals, float(m.threshold), days_ahead)

            market_list.append(
                {
                    "id": m.id,
                    "city": m.city,
                    "city_code": "",
                    "date": m.target_date.isoformat() if m.target_date else None,
                    "outcome_type": m.metric or "YES",
                    "strike_temp": float(m.threshold),
                    "current_yes_bid": current_price,
                    "current_no_bid": m.no_price or (1 - current_price),
                    "model_prob": model_prob,
                    "edge": model_prob - current_price,
                    "ev": safe_ev(model_prob, current_price),
                    "status": "OPEN",
                }
            )

        return {"markets": market_list, "count": len(market_list)}
    except Exception as e:
        logger.error("Markets API error: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e), "markets": []})
    finally:
        db.close()


@app.get("/api/bets")
def get_bets(status: str = "", limit: int = 100, offset: int = 0):
    """Get all bets with optional status filter and pagination.

    Query params:
      status  (str, optional)  -- comma-separated list of statuses to filter by.
                                Omitting returns ALL statuses.
      limit   (int, default 100)
      offset  (int, default 0)
    """
    db = get_db_session()
    try:
        q = db.query(Bet)
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            q = q.filter(Bet.status.in_(statuses))
        total = q.count()
        rows = q.order_by(Bet.placed_at.desc()).offset(offset).limit(limit).all()
        bets = []
        for b in rows:
            bets.append(
                {
                    "id": b.id,
                    "market_id": b.market_id,
                    "city": b.city or "",
                    "side": b.side or b.outcome or "YES",
                    "amount": float(b.amount or 0),
                    "entry_price": float(b.entry_price or b.price or 0),
                    "current_price": float(b.current_price or b.entry_price or b.price or 0),
                    "status": b.status,
                    "realized_pnl": float(b.realized_pnl or 0),
                    "unrealized_pnl": float(b.unrealized_pnl or 0),
                    "placed_at": b.placed_at.isoformat() if b.placed_at else None,
                    "settled_at": b.settled_at.isoformat() if b.settled_at else None,
                }
            )
        return {"bets": bets, "count": len(bets), "total": total}
    except Exception as e:
        logger.error("Bets API error: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e), "bets": [], "count": 0, "total": 0})
    finally:
        db.close()


# Keep other endpoints (signals, history, cleanup, start, stop,
# reset, ws, loops, run_cli) exactly as they were.


def _safe_parse_ladder(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, list) else []
    except Exception:
        return []


@app.get("/api/signals")
def get_signals():
    """Get all currently active (open) bets with live edge/price data."""
    db = get_db_session()
    try:
        active_bets = (
            db.query(Bet).filter(Bet.status.in_(OPEN_BET_STATUSES)).order_by(Bet.placed_at.desc().nullslast()).all()
        )

        # Pre-fetch all WeatherMarket data in one query
        market_ids = {bet.market_id for bet in active_bets if bet.market_id}
        markets_by_id = {}
        if market_ids:
            for m in db.query(WeatherMarket).filter(WeatherMarket.id.in_(market_ids)).all():
                markets_by_id[m.id] = m

        # Pre-fetch latest Analysis per market_id in batch
        latest_analysis_by_market = {}
        if market_ids:
            from sqlalchemy import func as sa_func
            latest_subq = (
                db.query(
                    Analysis.market_id,
                    sa_func.max(Analysis.analyzed_at).label("max_ts"),
                )
                .filter(Analysis.market_id.in_(market_ids))
                .group_by(Analysis.market_id)
                .subquery()
            )
            for a in (
                db.query(Analysis)
                .join(latest_subq, (Analysis.market_id == latest_subq.c.market_id) & (Analysis.analyzed_at == latest_subq.c.max_ts))
                .all()
            ):
                latest_analysis_by_market[a.market_id] = a

        # Pre-fetch origin analyses for entry_edge (by analysis_id)
        analysis_ids_needed = [bet.analysis_id for bet in active_bets if bet.analysis_id]
        origin_analyses_by_id = {}
        if analysis_ids_needed:
            for a in db.query(Analysis).filter(Analysis.id.in_(analysis_ids_needed)).all():
                origin_analyses_by_id[a.id] = a

        signals = []
        for bet in active_bets:
            market = markets_by_id.get(bet.market_id)
            res_date = market.target_date if market else None
            entry = bet.entry_price if bet.entry_price is not None else bet.price
            current = bet.current_price if bet.current_price is not None else bet.entry_price
            fair_value = None
            live_edge = None
            entry_edge = None
            move_pct = None
            try:
                latest = latest_analysis_by_market.get(bet.market_id)
                if latest:
                    fair_value = float(latest.estimated_probability)
                    if current is not None:
                        live_edge = fair_value - current
                if bet.analysis_id:
                    origin = origin_analyses_by_id.get(bet.analysis_id)
                    if origin:
                        entry_edge = float(origin.edge)
                if entry and current and entry > 0:
                    move_pct = (current - entry) / entry
            except Exception:
                pass
            signals.append(
                {
                    "id": bet.id,
                    "market_id": bet.market_id,
                    "city": bet.city or (market.city if market else "Unknown"),
                    "outcome": bet.side or bet.outcome or "YES",
                    "entry_price": entry,
                    "current_price": current,
                    "stake_amount": bet.amount or bet.stake_amount,
                    "unrealized_pnl": float(bet.unrealized_pnl or 0.0),
                    "fair_value": fair_value,
                    "edge": live_edge,
                    "entry_edge": entry_edge,
                    "live_edge": live_edge,
                    "move_pct": move_pct,
                    "ladder_orders": _safe_parse_ladder(bet.ladder_data),
                    "placed_at": bet.placed_at.isoformat() if bet.placed_at else None,
                    "resolution_date": res_date.isoformat() if res_date else None,
                    "status": bet.status,
                    "market_type": market.market_type if market else None,
                    "threshold": float(market.threshold) if market and market.threshold else None,
                    "question": market.question if market else None,
                    "metric": market.metric if market else None,
                }
            )
        return {"signals": signals, "count": len(signals)}
    finally:
        db.close()


@app.get("/api/history")
def get_history():
    """Get settled bet history with win/loss stats."""
    db = get_db_session()
    try:
        from sqlalchemy import case, func

        # True settlement stats: won+lost+settled+closed_early
        # closed_early bets are real exits â€” their PnL is realized cash.
        # Excluding them from stats gives a misleadingly small picture.
        real_settled_statuses = ["settled", "won", "lost", "closed_early"]

        stats_q = (
            db.query(
                func.count(Bet.id),
                func.sum(case((Bet.pnl > 0, 1), else_=0)),
                func.sum(case((Bet.pnl <= 0, 1), else_=0)),
                func.coalesce(func.sum(Bet.amount), 0.0),
                func.coalesce(func.sum(Bet.pnl), 0.0),
                func.coalesce(func.sum(case((Bet.pnl > 0, Bet.pnl), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((Bet.pnl <= 0, func.abs(Bet.pnl)), else_=0.0)), 0.0),
            )
            .filter(Bet.status.in_(real_settled_statuses))
            .one()
        )
        total_won = stats_q[1] or 0
        total_lost = stats_q[2] or 0
        total_stake_all = float(stats_q[3] or 0)
        total_pnl_all = float(stats_q[4] or 0)
        total_win_pnl = float(stats_q[5] or 0)
        total_loss_pnl = float(stats_q[6] or 0)

        # closed_early count and PnL (separate category)
        ce_q = (
            db.query(
                func.count(Bet.id),
                func.coalesce(func.sum(Bet.amount), 0.0),
                func.coalesce(func.sum(Bet.pnl), 0.0),
            )
            .filter(Bet.status == "closed_early")
            .one()
        )
        total_closed_early = ce_q[0] or 0
        ce_pnl = float(ce_q[2] or 0)

        # Average edge from settled bets (via Analysis join)
        avg_edge_q = (
            db.query(func.coalesce(func.avg(Analysis.edge), 0.0))
            .join(Bet, Bet.analysis_id == Analysis.id)
            .filter(Bet.status.in_(real_settled_statuses), Analysis.edge.isnot(None))
            .scalar()
        )
        avg_edge = float(avg_edge_q or 0.0)

        # History list: all settled + closed_early (most recent 300)
        all_closed_statuses = ["settled", "won", "lost", "closed_early"]
        # Use coalesce(settled_at, closed_at) for correct ordering
        close_date = func.coalesce(Bet.settled_at, Bet.closed_at)
        settled_bets = (
            db.query(Bet)
            .filter(Bet.status.in_(all_closed_statuses))
            .order_by(close_date.desc(), Bet.placed_at.desc())
            .limit(300)
            .all()
        )

        # Batch-load analyses for edge data (N+1 fix)
        bet_analysis_ids = [b.analysis_id for b in settled_bets if b.analysis_id]
        bet_analyses = {}
        if bet_analysis_ids:
            for a in db.query(Analysis).filter(Analysis.id.in_(bet_analysis_ids)).all():
                bet_analyses[a.id] = a

        history = []
        for bet in settled_bets:
            pnl = bet.pnl or bet.realized_pnl or 0.0
            stake = bet.amount or 0.0
            roi = roi_pct(pnl, stake)
            # Get actual edge from Analysis (net edge after slippage+fee)
            analysis = bet_analyses.get(bet.analysis_id) if bet.analysis_id else None
            edge_pct = round((analysis.edge or 0) * 100, 2) if analysis and analysis.edge else None
            # Determine exit type code from status + close_reason
            if bet.status == "closed_early" and bet.close_reason:
                cr = bet.close_reason.lower()
                if cr.startswith("take_profit"):
                    exit_type = "TP"
                elif cr.startswith("stop_loss"):
                    exit_type = "SL"
                elif cr.startswith("trailing_stop"):
                    exit_type = "TS"
                elif cr.startswith("time_decay"):
                    exit_type = "TD"
                else:
                    exit_type = "OT"
            else:
                exit_type = "ST"  # settlement (Polymarket resolved)

            history.append(
                {
                    "id": bet.id,
                    "city": bet.city,
                    "outcome": bet.side or "YES",
                    "entry_price": bet.price,
                    "stake_amount": stake,
                    "realized_pnl": pnl,
                    "roi": round(roi, 2),
                    "edge": edge_pct,
                    "result": "WIN" if pnl > 0 else "LOSS",
                    "placed_at": bet.placed_at.isoformat() if bet.placed_at else None,
                    "settled_at": (bet.settled_at.isoformat() if bet.settled_at else None),
                    "closed_at": (bet.closed_at.isoformat() if bet.closed_at else None),
                    "exit_type": exit_type,
                }
            )
        win_rate = win_rate_pct(total_won, total_won + total_lost)
        overall_roi = roi_pct(total_pnl_all, total_stake_all)
        profit_factor = round(total_win_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else 0.0
        return {
            "history": history,
            "stats": {
                "total_won": total_won,
                "total_lost": total_lost,
                "total_closed_early": total_closed_early,
                "closed_early_pnl": round(ce_pnl, 2),
                "win_rate": round(win_rate, 2),
                "overall_roi": round(overall_roi, 2),
                "total_stake": round(total_stake_all, 2),
                "total_pnl": round(total_pnl_all, 2),
                "total_win_pnl": round(total_win_pnl, 2),
                "total_loss_pnl": round(total_loss_pnl, 2),
                "profit_factor": profit_factor,
                "avg_edge": round(avg_edge * 100, 2) if avg_edge else 0.0,
            },
        }
    finally:
        db.close()


@app.get("/api/equity-curve")
def get_equity_curve():
    """Daily equity curve from ALL settled + closed_early bets (no limit).

    Returns [{date, pnl, count}] for every day that had at least one closure.
    The frontend starts from INITIAL_PORTFOLIO and accumulates daily PnL.
    """
    from sqlalchemy import func

    db = get_db_session()
    try:
        initial = state.config.INITIAL_PORTFOLIO
        closed_statuses = ("won", "lost", "settled", "closed_early")
        close_col = func.coalesce(Bet.settled_at, Bet.closed_at)
        # SQLite: use date() function to extract calendar date from datetime string
        day_expr = func.date(close_col)

        # Group PnL by calendar date (UTC)
        rows = (
            db.query(
                day_expr.label("day"),
                func.sum(Bet.pnl).label("daily_pnl"),
                func.count(Bet.id).label("cnt"),
            )
            .filter(Bet.status.in_(closed_statuses))
            .group_by(day_expr)
            .order_by(day_expr.asc())
            .all()
        )

        points = []
        running = initial
        for row in rows:
            # row.day is "YYYY-MM-DD" string from SQLite date()
            day_str = row.day  # "2026-06-24"
            pnl = float(row.daily_pnl or 0)
            running += pnl
            # Format: "24 Haz"
            from datetime import datetime as dt

            d = dt.strptime(day_str, "%Y-%m-%d")
            label = f"{d.day} {d.strftime('%b')}"
            points.append(
                {
                    "date": label,
                    "value": round(running, 2),
                    "pnl": round(pnl, 2),
                    "count": row.cnt,
                }
            )

        # Add today with current unrealized value
        unrealized = (
            db.query(func.coalesce(func.sum(Bet.unrealized_pnl), 0.0))
            .filter(Bet.status.in_(OPEN_BET_STATUSES))
            .scalar()
        ) or 0.0
        realized_now = running - initial  # all realized PnL accumulated
        today_val = initial + realized_now + float(unrealized)
        from datetime import datetime, timezone as tz

        today = datetime.now(tz.utc).replace(tzinfo=None)
        label = f"{today.day} {today.strftime('%b')}"
        if points and points[-1]["date"] == label:
            points[-1]["value"] = round(today_val, 2)
        else:
            points.append(
                {
                    "date": label,
                    "value": round(today_val, 2),
                    "pnl": 0,
                    "count": 0,
                }
            )

        return {"initial": initial, "points": points}
    except Exception as e:
        logger.error("Equity curve error: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e), "initial": config.INITIAL_PORTFOLIO, "points": []})
    finally:
        db.close()


@app.get("/api/slippage")
def get_slippage():
    """Return recent slippage data from analyses joined with market info."""
    db = get_db_session()
    try:
        rows = (
            db.query(
                Analysis,
                WeatherMarket.city,
                Bet.side,
                Bet.entry_price,
                Bet.pnl,
                Bet.status,
            )
            .outerjoin(WeatherMarket, WeatherMarket.id == Analysis.market_id)
            .outerjoin(Bet, Bet.analysis_id == Analysis.id)
            .filter(Analysis.slippage_pct.isnot(None))
            .order_by(Analysis.analyzed_at.desc())
            .limit(50)
            .all()
        )

        entries = []
        for analysis, city, _bet_side, entry_price, bet_pnl, _bet_status in rows:
            # Use Analysis fields for expected values, Bet fields for actuals
            expected_price = round(float(analysis.market_implied_prob or 0), 4)
            side = analysis.recommended_side or "â€”"
            # entry_price: 0 if no bet placed (frontend expects number)
            entry_price_val = round(float(entry_price), 4) if entry_price is not None else 0.0
            # result: PENDING if no bet, WIN/LOSS if bet settled
            if bet_pnl is not None:
                result = "WIN" if bet_pnl > 0 else "LOSS"
            else:
                result = "PENDING"
            entries.append(
                {
                    "id": str(analysis.id),
                    "city": city or "â€”",
                    "side": side,
                    "expected_price": expected_price,
                    "entry_price": entry_price_val,
                    "slippage_pct": round(float(analysis.slippage_pct), 6),  # as decimal (0.005)
                    "result": result,
                    "analyzed_at": (analysis.analyzed_at.isoformat() if analysis.analyzed_at else None),
                }
            )
        return {"slippage": entries}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        db.close()


@app.post("/api/cleanup")
def cleanup_old_data(_key: str = Depends(verify_api_key)):
    """Cancel stale open bets and refund their stakes (ladder-aware)."""
    db = get_db_session()
    try:
        _ts = datetime.now(timezone.utc).replace(tzinfo=None)
        _today_start = _ts.replace(hour=0, minute=0, second=0, microsecond=0)
        stale_analyses = (
            db.query(Analysis)
            .filter(Analysis.should_bet.is_(True), Analysis.analyzed_at < _today_start)
            .delete(synchronize_session=False)
        )
        stale_bets = db.query(Bet).filter(Bet.status.in_(OPEN_BET_STATUSES), Bet.placed_at < _today_start).all()
        cancelled = 0
        for bet in stale_bets:
            bet.status = "cancelled"
            bet.settled_at = datetime.now(timezone.utc).replace(tzinfo=None)

            # Calculate the actual debited amount â€” for ladder bets only
            # filled rungs were debited; for flat bets the full amount.
            from utils.accounting import credit_sale

            ladder = _safe_parse_ladder(bet.ladder_data)
            if ladder:
                filled_amount = sum(float(rung.get("amount", 0)) for rung in ladder if rung.get("status") == "filled")
                refund_amount = filled_amount if filled_amount > 0 else float(bet.amount or 0)
            else:
                refund_amount = float(bet.amount or 0)

            credit_sale(db, refund_amount, f"cleanup_refund:bet_{bet.id}")
            cancelled += 1
        db.commit()
        return {"deleted_analyses": stale_analyses, "cancelled_bets": cancelled}
    finally:
        db.close()


@app.post("/api/start")
async def start_bot(_key: str = Depends(verify_api_key)):
    """Start the scan-and-bet and settlement background loops."""
    async with state.start_stop_lock:
        if state.is_running:
            return {"status": "already_running"}
        state.is_running = True
        state.locked = False
        state.tasks["scan_and_bet"] = asyncio.create_task(scan_and_bet_loop(state))
        state.tasks["settlement"] = asyncio.create_task(settlement_loop(state))
        return {"status": "started"}


@app.post("/api/stop")
async def stop_bot(_key: str = Depends(verify_api_key)):
    """Stop all background loops and cancel pending tasks."""
    async with state.start_stop_lock:
        state.is_running = False
        tasks_to_await = []
        for t in list(state.tasks.values()):
            if not t.done():
                t.cancel()
                tasks_to_await.append(t)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        state.tasks.clear()
        return {"status": "stopped"}


@app.post("/api/reset")
async def reset_bot(_key: str = Depends(verify_api_key)):
    """Reset the bot state and clear in-flight DB rows WITHOUT auto-restart."""
    # Silmeden ÖNCE backup al — asla veri kaybı olmasın
    try:
        from db_backup import create_backup
        create_backup("pre_reset")
    except Exception as e:
        logger.warning("Pre-reset backup failed: %s", e)

    # Bets ve portfolio'yu parquet'a arşivle (reset sonrası kurtarma için)
    try:
        from database.db_cleanup import archive_bets_and_portfolio
        archive_bets_and_portfolio()
    except Exception as e:
        logger.warning("Pre-reset archive failed: %s", e)

    async with state.start_stop_lock:
        state.is_running = False
        tasks_to_await = []
        for t in list(state.tasks.values()):
            if not t.done():
                t.cancel()
                tasks_to_await.append(t)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        state.tasks.clear()

        db = get_db_session()
        try:
            # Clear all operational data
            db.query(Bet).delete()
            db.query(Analysis).delete()

            # Reset portfolio to exactly 1000
            pf = db.query(Portfolio).filter(Portfolio.id == 1).first()
            if not pf:
                pf = Portfolio(id=1)
                db.add(pf)

            pf.cash_balance = config.INITIAL_PORTFOLIO
            pf.initial_value = config.INITIAL_PORTFOLIO
            pf.current_value = config.INITIAL_PORTFOLIO
            pf.total_value = config.INITIAL_PORTFOLIO
            pf.total_realized_pnl = 0.0
            pf.daily_pnl = 0.0
            pf.total_won = 0
            pf.total_lost = 0

            db.commit()

            # Reset in-memory state
            state.total_signals = 0
            state.total_bets = 0
            state.last_scan = None

            return {
                "status": "reset",
                "message": "Sistem sifirlandi. Lutfen manuel olarak baslatin.",
                "portfolio": {
                    "current": config.INITIAL_PORTFOLIO,
                    "exposure": 0.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                },
            }
        except Exception as e:
            db.rollback()
            logger.error(f"Reset error: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})
        finally:
            db.close()


@app.get("/api/health-check")
def get_health_check():
    """Comprehensive health check for bot performance evaluation."""
    from datetime import timedelta

    db = get_db_session()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        h48 = now - timedelta(hours=48)

        # 1. Activity in last 24h
        bets_opened_24h = (
            db.query(Bet)
            .filter(
                Bet.placed_at >= h48,
                Bet.status.in_(OPEN_BET_STATUSES),
            )
            .count()
        )

        # 2. PASS reasons
        pass_analyses = (
            db.query(Analysis)
            .filter(
                Analysis.analyzed_at >= h48,
                Analysis.should_bet.is_(False),
            )
            .order_by(Analysis.analyzed_at.desc())
            .limit(20)
            .all()
        )

        pass_reasons = []
        for a in pass_analyses:
            pass_reasons.append(
                {
                    "market_id": a.market_id,
                    "edge_pct": round((a.edge or 0) * 100, 2),
                    "reason": a.reason or "Bilinmeyen neden",
                    "time": a.analyzed_at.isoformat() if a.analyzed_at else None,
                }
            )

        # 3. Edge distribution for all closed bets (settled + closed_early)
        settled_bet_statuses = ["won", "lost", "settled", "closed_early"]
        settled_for_edge = db.query(Bet).filter(Bet.status.in_(settled_bet_statuses)).all()

        # Batch-load analyses for all bets (N+1 fix)
        analysis_ids = [b.analysis_id for b in settled_for_edge if b.analysis_id]
        analyses_by_id = {}
        if analysis_ids:
            for a in db.query(Analysis).filter(Analysis.id.in_(analysis_ids)).all():
                analyses_by_id[a.id] = a

        edge_values = []
        for b in settled_for_edge:
            if b.analysis_id:
                analysis = analyses_by_id.get(b.analysis_id)
                if analysis:
                    edge_values.append(
                        {
                            "bet_id": b.id,
                            "raw_edge_pct": (round((analysis.raw_edge or 0) * 100, 2) if analysis.raw_edge else None),
                            "net_edge_pct": (round((analysis.edge or 0) * 100, 2) if analysis.edge else None),
                            "slippage_pct": (
                                round((analysis.slippage_pct or 0) * 100, 2)
                                if hasattr(analysis, "slippage_pct") and analysis.slippage_pct
                                else None
                            ),
                            "market_id": b.market_id,
                            "city": b.city,
                            "stake": b.stake_amount,
                            "status": b.status,
                            "pnl": b.pnl or 0.0,
                        }
                    )

        net_edges = [e["net_edge_pct"] for e in edge_values if e["net_edge_pct"] is not None]
        avg_net_edge = sum(net_edges) / len(net_edges) if net_edges else 0
        min_net_edge = min(net_edges) if net_edges else 0
        max_net_edge = max(net_edges) if net_edges else 0

        # 4. All-Time Closed Bets Summary (settled + closed_early)
        settled_all = (
            db.query(Bet)
            .filter(
                Bet.status.in_(("won", "lost", "settled", "closed_early")),
            )
            .all()
        )

        total_settled = len(settled_all)
        wins_all = sum(1 for b in settled_all if b.pnl and b.pnl > 0)
        losses_all = sum(1 for b in settled_all if b.pnl is not None and b.pnl <= 0)
        win_rate_all = win_rate_pct(wins_all, total_settled)
        total_pnl_all_health = sum(b.pnl or 0.0 for b in settled_all)
        total_stake_all_health = sum(b.amount or 0.0 for b in settled_all)
        roi_all = roi_pct(total_pnl_all_health, total_stake_all_health)

        # 4b. Exit type breakdown for wins/losses (donut charts)
        exit_type_map = {
            "take_profit": "TP",
            "stop_loss": "SL",
            "trailing_stop": "TS",
            "time_decay": "TD",
        }
        wins_by_exit = {"TP": 0, "SL": 0, "TS": 0, "TD": 0, "ST": 0}
        losses_by_exit = {"TP": 0, "SL": 0, "TS": 0, "TD": 0, "ST": 0}
        for b in settled_all:
            is_win = b.pnl and b.pnl > 0
            if b.status == "closed_early" and b.close_reason:
                cr = b.close_reason.lower()
                code = "ST"
                for prefix, c in exit_type_map.items():
                    if cr.startswith(prefix):
                        code = c
                        break
            else:
                code = "ST"
            if is_win:
                wins_by_exit[code] = wins_by_exit.get(code, 0) + 1
            else:
                losses_by_exit[code] = losses_by_exit.get(code, 0) + 1

        # 5. Red Flags â€” son 48 saatlik verilere gÃ¶re
        red_flags = []

        # Son 48 saatteki kayÄ±plarÄ± say
        recent_losses = sum(
            1
            for b in settled_all
            if b.pnl is not None
            and b.pnl <= 0
            and ((b.settled_at and b.settled_at >= h48) or (b.closed_at and b.closed_at >= h48))
        )
        recent_total = sum(
            1 for b in settled_all if ((b.settled_at and b.settled_at >= h48) or (b.closed_at and b.closed_at >= h48))
        )
        recent_wins = sum(
            1
            for b in settled_all
            if b.pnl is not None
            and b.pnl > 0
            and ((b.settled_at and b.settled_at >= h48) or (b.closed_at and b.closed_at >= h48))
        )
        recent_win_rate = win_rate_pct(recent_wins, recent_total)

        if recent_total >= 10 and recent_losses >= 7:
            red_flags.append(
                {
                    "severity": "critical",
                    "message": (
                        f"Son 48 saatte {recent_losses} kayÄ±p "
                        f"(toplam {recent_total} sonuÃ§lanan). "
                        f"Calibration bozuk olabilir."
                    ),
                    "action": "Botu durdur ve kalibrasyonu kontrol et.",
                }
            )

        if state.is_running and bets_opened_24h == 0:
            any_analyses = db.query(Analysis).filter(Analysis.analyzed_at >= h48).count()
            if any_analyses > 0:
                red_flags.append(
                    {
                        "severity": "warning",
                        "message": (
                            f"Son 24 saatte {any_analyses} analiz yapÄ±ldÄ±"
                            f" ama hiÃ§ bet aÃ§Ä±lmadÄ±."
                            f" Edge threshold Ã§ok yÃ¼ksek olabilir."
                        ),
                        "action": "min_edge'i dÃ¼ÅŸÃ¼r veya marketleri kontrol et.",
                    }
                )
            else:
                red_flags.append(
                    {
                        "severity": "info",
                        "message": ("Son 24 saatte hiÃ§ analiz yapÄ±lmadÄ±. Market taramasÄ± Ã§alÄ±ÅŸÄ±yor mu?"),
                        "action": "Market taramasÄ±nÄ± kontrol et.",
                    }
                )

        if net_edges and all(e < 2.5 for e in net_edges):
            red_flags.append(
                {
                    "severity": "critical",
                    "message": (
                        f"TÃ¼m net edge'ler %2.5 altÄ±nda (ortalama: %{avg_net_edge:.1f}). Maliyeti karÅŸÄ±lamÄ±yor."
                    ),
                    "action": ("Botu durdur. min_edge veya kalibrasyon ayarlarÄ±nÄ± gÃ¶zden geÃ§ir."),
                }
            )

        if recent_total >= 5 and recent_win_rate < 50:
            red_flags.append(
                {
                    "severity": "critical",
                    "message": (f"Win rate %{win_rate_all:.1f} (5+ sonuÃ§lanmÄ±ÅŸ bet). Model tahminleri gÃ¼venilmez."),
                    "action": "Kalibrasyon verisini kontrol et, evrim Ã§alÄ±ÅŸtÄ±r.",
                }
            )

        open_total = db.query(Bet).filter(Bet.status.in_(OPEN_BET_STATUSES)).count()
        if bets_opened_24h > 50 or open_total > 50:
            red_flags.append(
                {
                    "severity": "warning",
                    "message": (
                        f"AÅŸÄ±rÄ± bahis: 24s'de {bets_opened_24h} aÃ§Ä±lan, "
                        f"{open_total} aÃ§Ä±k. Risk yÃ¶netimi aÅŸÄ±lÄ±yor."
                    ),
                    "action": "min_edge'i yÃ¼kselt, Kelly fraction'Ä± dÃ¼ÅŸÃ¼r.",
                }
            )

        recent_pnl = sum(
            b.pnl or 0.0
            for b in settled_all
            if ((b.settled_at and b.settled_at >= h48) or (b.closed_at and b.closed_at >= h48))
        )
        if recent_pnl < 0 and recent_total >= 5:
            red_flags.append(
                {
                    "severity": "warning",
                    "message": (f"Son 48 saatte PnL negatif: ${recent_pnl:.2f}. Zarar trendi devam ediyor."),
                    "action": "Botu izlemeye devam et. 3 gÃ¼n sonunda karar ver.",
                }
            )

        # 6. Daily PnL Timeline (last 7 days)
        from sqlalchemy import or_

        daily_pnl = []
        for i in range(7):
            day_start = (now - timedelta(days=i + 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_bets = (
                db.query(Bet)
                .filter(
                    or_(
                        Bet.settled_at >= day_start,
                        Bet.closed_at >= day_start,
                    ),
                    or_(
                        Bet.settled_at < day_end,
                        Bet.closed_at < day_end,
                    ),
                    Bet.status.in_(("won", "lost", "settled", "closed_early")),
                )
                .all()
            )
            day_pnl = sum(b.pnl or 0.0 for b in day_bets)
            day_wins = sum(1 for b in day_bets if b.pnl and b.pnl > 0)
            day_losses = sum(1 for b in day_bets if b.pnl is not None and b.pnl <= 0)
            daily_pnl.append(
                {
                    "date": day_start.strftime("%m/%d"),
                    "pnl": round(day_pnl, 2),
                    "wins": day_wins,
                    "losses": day_losses,
                    "total": day_wins + day_losses,
                }
            )
        daily_pnl.reverse()

        # 7. Overall verdict
        if not red_flags or all(f["severity"] == "info" for f in red_flags):
            verdict = "healthy"
        elif any(f["severity"] == "critical" for f in red_flags):
            verdict = "critical"
        else:
            verdict = "warning"

        return {
            "verdict": verdict,
            "is_running": state.is_running,
            "activity_24h": {
                "bets_opened": bets_opened_24h,
                "pass_reasons": pass_reasons,
                "total_analyses": db.query(Analysis).filter(Analysis.analyzed_at >= h48).count(),
            },
            "edge_distribution": {
                "values": edge_values,
                "avg_net_edge_pct": round(avg_net_edge, 2),
                "min_net_edge_pct": round(min_net_edge, 2),
                "max_net_edge_pct": round(max_net_edge, 2),
                "count": len(edge_values),
            },
            "summary_all": {
                "total_settled": total_settled,
                "wins": wins_all,
                "losses": losses_all,
                "win_rate_pct": round(win_rate_all, 1),
                "total_pnl": round(total_pnl_all_health, 2),
                "total_stake": round(total_stake_all_health, 2),
                "roi_pct": round(roi_all, 2),
                "avg_net_edge_pct": round(avg_net_edge, 2),
                "wins_by_exit": wins_by_exit,
                "losses_by_exit": losses_by_exit,
            },
            "red_flags": red_flags,
            "daily_pnl_timeline": daily_pnl,
        }
    except Exception as e:
        logger.error("Health check error: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e), "verdict": "error"})
    finally:
        db.close()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, api_key: str = ""):
    if api_key != API_KEY:
        await websocket.close(code=4001, reason="Invalid API key")
        return
    await websocket.accept()
    state.websocket_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in state.websocket_clients:
            state.websocket_clients.remove(websocket)


# Re-export loop functions from bot_loop module so existing
# callers (e.g. bot_lifespan in main.py) can import from here.
from bot_loop import scan_and_bet_loop, settlement_loop  # noqa: E402, F401
