"""Latent-bug detection: catch silent failures in dead/unwired code paths.

The e2e, liveliness and integration suites only exercise the *hot* pipeline
(fetch -> parse -> analyze -> bet -> settle).  Leaf modules — backfiller,
calibration engine, data pipelines, etc. — are never imported in tests, so
import errors, missing attributes, and dead references go undetected until
the code is actually called (which may be never, or only when it crashes).

This suite fills that gap:

1. **Import-all** — every .py file in the project tree is imported.  Catches
   syntax errors, broken imports, and AttributeError on first access.
2. **Config-proxy sync** — ``_ConfigProxy._MAP`` keys are checked against
   actual ``BotConfig`` attributes so the proxy cannot silently miss a field.
3. **Dead-code audit** — modules expected to be wired into the bot loop
   (jobs, data_pipeline) are verified to be import-reachable
   from the scheduler / evolution_job entry points.
4. **Calibration live** — the ``_get_calibration`` + ``get_calibrated_temperature``
   chain that ``Calculator`` now calls does not crash, even without real data.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

# Root directory of the Junbo project.
REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 1. Import every .py file in the project ──────────────────────────────


def _iter_python_files(root: Path):
    """Yield all ``.py`` files under *root*, excluding test files and venvs."""
    for dirpath, _dirnames, filenames in os.walk(root):
        # Skip hidden dirs, venvs, node_modules, out, .hypothesis, .github
        rel = Path(dirpath).relative_to(root)
        parts = set(rel.parts)
        if parts & {
            "__pycache__",
            ".hypothesis",
            ".github",
            "node_modules",
            "out",
            "dashboard",
            ".pytest_cache",
        }:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def test_import_all_modules():
    """Every ``.py`` file must be importable without error.

    This catches silent AttributeError / broken ``from X import Y`` statements
    in code paths that are never exercised by integration or e2e tests.
    """
    errors: list[str] = []
    for fpath in _iter_python_files(REPO_ROOT):
        # Compute dotted module path relative to repo root.
        rel = fpath.relative_to(REPO_ROOT)
        dotted = str(rel.with_suffix("")).replace(os.sep, ".")
        # Skip test files, __init__, and scripts with leading underscore.
        if rel.parts and rel.parts[0] in ("tests", "scripts", "backtest_archive"):
            continue
        if dotted.endswith("__init__"):
            continue
        if fpath.name.startswith("_"):
            continue
        # Skip web frontend (Next.js) and config files.
        if dotted.startswith("src") or dotted.startswith("app"):
            continue
        try:
            importlib.import_module(dotted)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{dotted}: {exc}")

    if errors:
        msg = "\n".join(errors[:20])  # show first 20
        if len(errors) > 20:
            msg += f"\n... and {len(errors) - 20} more"
        pytest.fail(msg)


# ── 2. Config-proxy synchronisation ──────────────────────────────────────


def test_config_proxy_map_matches_botconfig():
    """Every key in ``_ConfigProxy._MAP`` must map to a real attribute.

    If an attribute is added to ``BotConfig`` but not to the proxy map,
    ``Config.ATTR`` raises ``AttributeError``.  This test ensures the
    two stay in sync.

    Conversely, if a proxy key maps to something that no longer exists
    on ``BotConfig``, that is flagged too.
    """
    from config.settings import BotConfig, _ConfigProxy

    proxy_map = getattr(_ConfigProxy, "_MAP", {})

    # Root-level fields (section == "root") → direct BotConfig attributes.
    root_aliases = {k for k, (sec, _attr) in proxy_map.items() if sec == "root"}
    strategy_aliases = {k for k, (sec, _attr) in proxy_map.items() if sec == "strategy"}

    for alias in root_aliases:
        _sec, attr = proxy_map[alias]
        assert hasattr(BotConfig, attr), f"Proxy maps '{alias}' → BotConfig.{attr} but BotConfig has no '{attr}'"

    # Strategy fields (section == "strategy") → BotConfig.strategy.*
    for alias in strategy_aliases:
        _sec, attr = proxy_map[alias]
        s = getattr(BotConfig, "strategy", None)
        if s is not None:
            assert hasattr(s, attr), f"Proxy maps '{alias}' → BotConfig.strategy.{attr} but strategy has no '{attr}'"


# ── 3. Dead-code audit: every job/engine must be reachable ───────────────


# (module, reason) tuples that MUST be importable from the running bot.
REQUIRED_REACHABLE_MODULES = [
    ("jobs.scheduler", "run_cycle"),
    ("jobs.evolution_job", "run_evolution_cycle"),
    ("jobs.backup_job", "run_backup_once"),
    ("data_pipeline.backfill_from_live_db", "backfill"),
    ("data_pipeline.unified_datastore", "UnifiedDatastore"),
    ("data_pipeline.weather_ensemble", "backfill_archive_many"),
]


def test_required_modules_reachable():
    """Every job / pipeline module listed in the architecture must
    import without error, even if it is never called by the hot path.

    This catches the ``config.ICAO_COORDS`` class of bug — silent import
    failures in unwired code.
    """
    errors: list[str] = []
    for dotted, _reason in REQUIRED_REACHABLE_MODULES:
        try:
            importlib.import_module(dotted)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{dotted}: {exc}")
    if errors:
        pytest.fail("\n".join(errors))


def test_required_reachable_names():
    """Verify that specific callable names exist inside required modules."""
    errors: list[str] = []
    for dotted, name in REQUIRED_REACHABLE_MODULES:
        try:
            mod = importlib.import_module(dotted)
            if not hasattr(mod, name):
                errors.append(f"{dotted} has no attribute '{name}'")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{dotted}: {exc}")
    if errors:
        pytest.fail("\n".join(errors))


# ── 5. Dead-code detection: public functions with zero callers ────────────
#
# A public (non-underscore) function defined in production code but never
# called from any other non-test file is either:
#   a) A dormant entry point (API route, CLI, asyncio task) — these are
#      allowlisted below.
#   b) Dead code that should either be wired or removed (the module it's
#      in may have no tests at all, so the bug rots silently).
#
# The test below detects (b) automatically; when you *intentionally* add
# a public function that is meant to be called by an external system (or
# is a hot-path entry point), add its name to the ALLOWED_DEAD set.
# -------------------------------------------------------------------------

# Functions that are intentionally called by external systems or
# framework-magic entry points (FastAPI routes, CLI, service manager, …).
ALLOWED_DEAD = {
    # CLI / service entry points
    "main",
    "run_cli",
    "SvcStop",
    "SvcDoRun",
    "bot_lifespan",
    "lifespan",
    # scripts/backtest.py argparse subcommand entry points (wired to the
    # subparsers inside the SAME file, so the cross-file census cannot see
    # the reference; they are reached via args.func(args) dispatch).
    "cmd_gunluk",
    "cmd_orderbook",
    "cmd_metar_peak",
    "cmd_metar_peak_live",
    "cmd_metar_vs_settlement",
    "cmd_walk_forward",
    # 2026-08-18: 24 saat METAR arsiv toplayici — run_metar_peak_bets icinden
    # cagrilir (jobs.metar_peak.py), cross-file census goremez.
    "collect_metar_archive",
    # Finance helpers covered by unit tests but no non-test caller today
    # (api.py computes equity inline; kept as a documented formula helper)
    "portfolio_current_value",
    # FastAPI route decorators (called by uvicorn, not directly by our code)
    "verify_api_key",
    "broadcast_message",
    "root",
    "get_status",
    "get_markets",
    "get_bets",
    "get_signals",
    "get_history",
    "get_equity_curve",
    "get_slippage",
    "cleanup_old_data",
    "start_bot",
    "stop_bot",
    "reset_bot",
    "get_health_check",
    "get_city_bets",
    "websocket_endpoint",
    "initialize_modules",
    "_safe_parse_ladder",
    # Scheduler entry called by asyncio task
    "run_cycle",
    # Backend / DB (used via SQLAlchemy session, not direct call)
    "get_engine",
    # Logging / utilities
    "setup_logging",
    "log",
    "emit",
    "wrapper",
    "decorator",
    # Watchdog / backup (called by separate scripts / cron)
    "watchdog_loop",
    "is_bot_running",
    "ensure_running",
    "service_state",
    "run_backup_force",
    "last_backup_date",
    "list_backups",
    "restore_backup",
    "prune_all",
    "mirror_offsite",
    # Data watchdog helpers (scripts/data_watchdog.py — Task Scheduler entry)
    "db_max_age",
    "ensure_task_enabled",
    "run_script",
    # Backtest script internal helpers (standalone scripts: kendi icinde
    # kullanilir, disardan cagrilmaz — census false-positive)
    "ask_at_or_after",
    "cost_of",
    "peak_break",
    "trough_lock",
    "trough_break",
    "fetch_history",
    "first_ask_below",
    "peak_lock",
    # Strategy methods used via dispatch / polymorphism
    "analyze_signal",
    "execute_signal",
    "calculate_position_size_with_risk",
    "calculate_brier_score",
    "run_optimization_cycle",
    "optimize_strategy_params",
    "analyze_market",
    "get_adjusted_probability",
    # Adaptive sizing (called by Strategy, not directly)
    "is_enabled",
    "get_phase",
    "estimate_edge",
    "get_kelly_fraction",
    "retrain_sizing",
    "maybe_retrain_sizing",
    # Polymarket ingester functions (called by fetch_polymarket events)
    "fetch_markets",
    "fetch_active_markets",
    "fetch_events",
    "fetch_market_detail",
    "fetch_weather_markets",
    # On-chain ingestion (separate pipeline)
    "decode_order_filled",
    "asset_id_to_side",
    "fetch_markets_metadata",
    "get_latest_block",
    "get_logs",
    "load_cursor",
    "save_cursor",
    "scan_order_filled",
    "join_with_markets",
    "get_hyperliquid_live_orderbook",
    "get_hyperliquid_snapshots",
    "get_market_by_slug",
    "get_market_summary",
    "get_live_orderbook_by_slug",
    "iter_all_historical_markets",
    "iter_all_snapshots",
    "list_categories",
    "list_live_markets",
    "list_recent_markets",
    "list_historical_markets",
    "list_backtest_history",
    "get_backtest_status",
    "get_market_snapshots",
    "public_stats",
    # DB cleanup (called by auto_cleanup daily job)
    "archive_old_forecasts",
    "load_archives",
    "get_archive_stats",
    # Weather ensemble
    "brier_score_per_model",
    "fetch_archive_actuals",
    # Data pipeline CLI entry points (called via python -m / __main__)
    "ingest_all",
    "fetch_historical_forecast_ensemble",
    # Slippage helpers
    "estimate_slippage",
    "adjust_edge_for_costs",
    "adjust_kelly_for_slippage",
    "check_orderbook_depth",
    # Drawdown monitor internal
    "current_equity",
    "peak",
    "halt",
    "alpha_multiplier",
    "record_outcome",
    # Formulae
    "normal_cdf",
    "kelly_fraction",
    "kelly_bet_amount",
    # Probability internal
    "estimate_probability",
    "compute_effective_min_edge",
    # Scraper session (called by async context manager)
    "close_session",
    "init_session",
    "fetch_polymarket_events",
    "fetch_for_markets",
    "cache_clear",
    "fetch_one_blocking",
    # Market parser internal
    "parse_and_update",
    "parse_all_unparsed",
    # Preflight (wired into bot_loop._run_daily_maintenance now)
    "run_preflight_check",
    # Unified datastore internal
    "read_markets",
    "read_forecasts",
    "read_actuals",
    "read_trades",
    "read_snapshots",
    "write_snapshots",
    "write_trades",
    "get_split",
    "write_markets",
    "write_forecasts",
    "write_actuals",
    # Backfill from live db
    "backfill",
    "main",
    # Db cleanup
    "auto_cleanup",
    "archive_bets_and_portfolio",
    # Db backup internal
    "create_backup",
    "list_backups",
    "restore_backup",
    "prune_all",
    # Scheduler internal
    "_should_skip_analysis",
    "run_fetch_markets",
    "run_parse_markets",
    "run_fetch_weather",
    "run_analyze",
    "run_place_bets",
    "run_update_prices",
    "run_refresh_open_prices",
    "run_settle",
    "run_report",
    "analyze_single",
    # Accounting internal
    "debit_stake",
    "credit_sale",
    "credit_settlement",
    # Strategy internal (called by other Strategy methods)
    "check_city_cap",
    "increment_city_bet",
    "decrement_city_bet",
    "calculate_kelly_bet_size",
    "get_portfolio_value",
    "get_total_exposure",
    "get_daily_pnl",
    "daily_loss_limit_amount",
    "is_bot_locked",
    # Settler internal
    "settle_all",
    # Bet placer internal
    "place_bet",
    "place_all_pending",
    # Calculator internal
    "kelly_criterion",
    "get_multi_model_forecast",
    # Evolution job internal (called by run_evolution_cycle)
    "should_run_calibration",
    "should_run",
    # Watchdog
    "watchdog_loop",
    "start_bot",
    "stop_bot",
    "restart_bot",
    # Bot loop internal
    "price_poller_loop",
    "scan_and_bet_loop",
    "settlement_loop",
    "snapshot_loop",
    "take_market_snapshots",
    "cleanup_old_snapshots",
    "get_price_history",
    "get_city_price_comparison",
    # Import-time overlay of persisted strategy params (runs at settings import)
    "apply_persisted_strategy_params",
    # Model run detector (public API for latency arbitrage)
    "get_active_model_windows",
    # Entry time analysis (called from API/CLI on demand)
    "get_entry_time_analysis",
    # Market selection helpers (called by scheduler/bet_placer)
    "market_group_key",
    "passes_time_gate",
    "select_highest_yes_candidates",
    # Standalone scripts (called via __main__ / scheduled task)
    "analyze_date",
    "fetch_yes_markets",
    "parse_city",
    "init_snapshot_db",
    "take_snapshots",
    # City-time analysis scripts (standalone, called via __main__)
    "analyze_best_hours",
    "analyze_city_time_patterns",
    "print_analysis",
    "print_top_cities_by_metric",
    "generate_charts",
    "load_data",
    "save_report",
    # Backtest scripts (standalone, called via __main__ / scheduled task)
    "compute_city_stats",
    "compute_hts_bands",
    "compute_summary_stats",
    "find_best_cities",
    "print_result",
    "print_summary_table",
    "run_rolling_backtest",
    "simulate_strategy",
    # New data collection scripts (called via scheduled tasks)
    "init_orderbook_db",
    "fetch_active_markets_from_gamma",
    "extract_yes_token_id",
    "fetch_orderbook",
    "parse_orderbook",
    "save_snapshot",
    "extract_city_from_question",
    "collect_once",
    "init_actuals_db",
    "get_cities",
    "get_last_fetched_date",
    "fetch_archive_actuals",
    "parse_archive_response",
    "upsert_records",
    "copy_with_retry",
    "get_size_mb",
    "backup_once",
    "purge_old_backups",
    "init_backtest_db",
    "get_bot_connection",
    "get_backtest_connection",
    "get_table_columns",
    "ensure_table_exists",
    "get_max_pk",
    "sync_table",
    "sync_once",
    # Backtest/analysis scripts (utility functions, not called by main bot)
    "compute_city_stats",
    "compute_hts_bands",
    "find_best_cities",
    "generate_charts",
    "print_result",
    "print_summary_table",
    "print_top_cities_by_metric",
    "run_rolling_backtest",
    "simulate_strategy",
    # Walk-forward backtest (standalone script)
    "calculate_metrics",
    "get_available_forecast",
    "resolve_outcome_from_bets",
    "run_single_fold",
    "simulate_decision",
    "walk_forward",
    # First-peak / city-time analysis scripts (standalone, run via __main__)
    "classify",
    "find_first_peak",
    "get_city",
    "load_outcomes",
    "load_series",
    "load_settled",
    "load_snapshots",
    "hours_between",
    "mins",
    # Excel export script (standalone, run via __main__)
    "bet_rows",
    # Outcome parsing utility helper (used inside parse_resolved_outcome)
    "market_is_resolved",
    # Empirical CDF probability model (2026-08-12, kod entegrasyonu settlement sonrasi)
    "estimate_probability_empirical",
    "empirical_cdf",
    "load_empirical_errors",
    # METAR live helpers (2026-08-14 — metar_loop ana akista, live/health araclari yedek)
    "fetch_metar_live",
    "metar_live_check",
    # Backtest analiz script'leri (standalone, kullanici istegiyle olusturuldu)
    "ask_at_utc",
    "ask_before",
    "ask_until",
    "core_bet",
    "last_ask_before_close",
    "single_bet",
    # METAR-peak + erken-giris backtest helper'lari (2026-08-16)
    "price_at",
    "price_before",
    "thr_to_market",
}


def test_no_dead_public_functions():
    """Every public (non-underscore) function must be called from at least
    one non-test file besides its own definition file.

    Exceptions are allowlisted in ``ALLOWED_DEAD`` (entry points, framework
    hooks, API routes, etc.).

    This is a *census* test: when you add a new public function, it forces
    you to either wire it or add it to the allowlist, which prevents
    accidental dead code from accumulating silently.
    """
    import ast
    import re
    from collections import defaultdict

    repo_root = Path(__file__).resolve().parent.parent
    skip_dirs = {"tests", "venv", "__pycache__", "migrations", "node_modules"}

    all_files = [f for f in repo_root.rglob("*.py") if not any(d in f.parts for d in skip_dirs)]

    # 1. Collect all top-level public function definitions
    defined_in: dict[str, set[Path]] = defaultdict(set)
    defined_details: dict[str, list[tuple[Path, int]]] = defaultdict(list)

    for f in all_files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                if not name.startswith("_"):
                    defined_in[name].add(f)
                    defined_details[name].append((f, node.lineno))

    # 2. For each, search for word-boundaried references in other files
    dead: list[str] = []
    for func_name in sorted(defined_in):
        if func_name in ALLOWED_DEAD:
            continue
        def_files = defined_in[func_name]
        other_files = [f for f in all_files if f not in def_files]

        pattern = re.compile(r"\b" + re.escape(func_name) + r"\b")
        found = False
        for f in other_files:
            try:
                if pattern.search(f.read_text(encoding="utf-8")):
                    found = True
                    break
            except Exception:
                continue

        if not found:
            locations = [f"{f.relative_to(repo_root)}:{ln}" for f, ln in defined_details[func_name]]
            dead.append(f"  {func_name} ({', '.join(locations)})")

    if dead:
        pytest.fail(
            f"Found {len(dead)} public function(s) never referenced outside\n"
            f"their definition file. Either wire them or add to ALLOWED_DEAD:\n" + "\n".join(dead)
        )
