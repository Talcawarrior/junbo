"""Junbo entry point — CLI and bot launcher.

Thin wrapper that imports the FastAPI app, BotState, and bot loops
from their dedicated modules:

  api.py      — FastAPI routes, BotState, app definition
  bot_loop.py — scan_and_bet_loop, settlement_loop
"""

import argparse
import asyncio
import os
import platform
import signal
import subprocess
import time
from contextlib import asynccontextmanager


from config.logging_config import setup_logging
from config.settings import config
from database.db import ensure_initial_portfolio, get_db_session, init_db
from database.models import Analysis, Bet, Portfolio

setup_logging()

# Import app, state, and loop functions from split modules.
from api import app, scan_and_bet_loop, settlement_loop, state  # noqa: E402

logger = __import__("logging").getLogger(__name__)


# ── Port conflict prevention ───────────────────────────────────────────────
def _kill_port_owner(port: int, host: str = "127.0.0.1") -> bool:
    """Kill any process listening on *port* so we can bind to it.

    Works on Windows (netstat + taskkill), Linux (lsof + kill),
    and macOS (lsof + kill).  Returns True if a process was killed.
    """
    is_windows = platform.system() == "Windows"

    try:
        if is_windows:
            # netstat -ano | findstr LISTENING | findstr :PORT
            raw = subprocess.check_output(
                ["netstat", "-ano"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            pids = set()
            for line in raw.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        try:
                            pids.add(int(parts[-1]))
                        except ValueError:
                            pass
            # Exclude our own PID
            my_pid = os.getpid()
            pids.discard(my_pid)
            if not pids:
                return False
            for pid in pids:
                logger.warning("PORT CONFLICT: killing PID %d that owns port %d", pid, port)
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                )
            # Wait until port is free
            for _ in range(10):
                time.sleep(0.5)
                check = subprocess.check_output(
                    ["netstat", "-ano"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                if not any(f":{port}" in line and "LISTENING" in line for line in check.splitlines()):
                    return True
            logger.error("Port %d still occupied after killing processes", port)
            return False
        else:
            # Linux / macOS — lsof -ti :PORT
            raw = subprocess.check_output(
                ["lsof", "-ti", f":{port}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            pids = set()
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    try:
                        pids.add(int(line))
                    except ValueError:
                        pass
            my_pid = os.getpid()
            pids.discard(my_pid)
            if not pids:
                return False
            for pid in pids:
                logger.warning("PORT CONFLICT: killing PID %d that owns port %d", pid, port)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            time.sleep(1)
            return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        # netstat/lsof not available or returned error — assume no conflict
        return False


def _ensure_port_free(port: int, host: str = "127.0.0.1") -> None:
    """Ensure *port* is free before starting uvicorn. Kill stale processes."""
    if _kill_port_owner(port, host):
        logger.info("Port %d cleared — stale process removed", port)


def run_cli():
    """CLI entry point: run, reset, fetch, parse, weather, analyze."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    args = parser.parse_args()

    # Bot başlamadan önce DB backup al
    try:
        from db_backup import create_backup
        create_backup("startup")
    except Exception:
        pass

    init_db()
    ensure_initial_portfolio()
    from jobs.scheduler import (
        run_analyze,
        run_fetch_markets,
        run_fetch_weather,
        run_parse_markets,
        run_place_bets,
        run_report,
        run_settle,
    )

    cmds = {
        "fetch": run_fetch_markets,
        "parse": run_parse_markets,
        "weather": run_fetch_weather,
        "analyze": run_analyze,
        "bet": run_place_bets,
        "settle": run_settle,
        "report": run_report,
    }
    if args.command == "bot":
        # ── Start bot: API + Dashboard + Background loops ────────────────────
        _base = os.path.dirname(os.path.abspath(__file__))
        _out = os.path.join(_base, "out")
        _dash = os.path.join(_base, "dashboard", "out")
        _dashboard_out = _out if os.path.isdir(_out) else _dash
        if os.path.isdir(_dashboard_out):
            from fastapi.staticfiles import StaticFiles

            app.mount(
                "/_next",
                StaticFiles(directory=os.path.join(_dashboard_out, "_next")),
                name="next-static",
            )
            app.mount("/", StaticFiles(directory=_dashboard_out, html=True), name="dashboard")

        # Start background loops on FastAPI startup (lifespan handler)
        @asynccontextmanager
        async def bot_lifespan(app):
            logger.info("LIFESPAN STARTUP - Starting bot loops")
            init_db()
            state.initialize_modules()

            # Ensure initial portfolio row exists in DB
            try:
                ensure_initial_portfolio()
            except Exception as e:
                logger.warning("Portfolio init warning: %s", e)

            state.is_running = True
            state.locked = False
            state.tasks["scan_and_bet"] = asyncio.create_task(scan_and_bet_loop(state))
            state.tasks["settlement"] = asyncio.create_task(settlement_loop(state))
            logger.info("Bot loops started (scan_and_bet + settlement)")
            yield
            # Shutdown
            logger.info("LIFESPAN SHUTDOWN - Stopping bot loops")
            for t in list(state.tasks.values()):
                if not t.done():
                    t.cancel()
            state.tasks.clear()
            state.is_running = False

        app.router.lifespan_context = bot_lifespan

        import uvicorn  # noqa: I001

        _ensure_port_free(config.PORT, config.HOST)
        uvicorn.run(app, host=config.HOST, port=config.PORT)
    elif args.command == "run":
        # ── Mount Next.js static dashboard (must be LAST — catch-all) ──────
        _base = os.path.dirname(os.path.abspath(__file__))
        _out = os.path.join(_base, "out")
        _dash = os.path.join(_base, "dashboard", "out")
        _dashboard_out = _out if os.path.isdir(_out) else _dash
        if os.path.isdir(_dashboard_out):
            from fastapi.staticfiles import StaticFiles

            app.mount(
                "/_next",
                StaticFiles(directory=os.path.join(_dashboard_out, "_next")),
                name="next-static",
            )
            app.mount("/", StaticFiles(directory=_dashboard_out, html=True), name="dashboard")

        import uvicorn  # noqa: I001

        _ensure_port_free(config.PORT, config.HOST)
        uvicorn.run(app, host=config.HOST, port=config.PORT)
    elif args.command == "reset":
        # Silmeden ÖNCE backup al
        try:
            from db_backup import create_backup
            create_backup("pre_reset_cli")
        except Exception:
            pass
        # Bets ve portfolio'yu parquet'a arşivle
        try:
            from database.db_cleanup import archive_bets_and_portfolio
            archive_bets_and_portfolio()
        except Exception:
            pass
        db = get_db_session()
        db.query(Bet).update({"status": "cancelled"})
        db.query(Analysis).delete()
        pf = db.query(Portfolio).filter(Portfolio.id == 1).first()
        pf.cash_balance = config.INITIAL_PORTFOLIO
        db.commit()
        db.close()
    elif args.command in cmds:
        print(cmds[args.command]())


if __name__ == "__main__":
    run_cli()
