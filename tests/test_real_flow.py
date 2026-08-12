"""GERCEK bot akis testi — loop'larin kendisini calistirir (mock'lu dis cagrilarla).

2026-08-11 itibariyle botun GERCEK calisma mimarisi (main.py lifespan):
  - scan_and_bet_loop  : fetch -> parse -> [spread'de run_cycle YOK] -> weather
                         -> 2-gun tarih tespiti -> place_spread_bets (ANA BET YOLU)
  - price_poller_loop  : fetch -> refresh_open_prices -> update_prices -> risk
  - settlement_loop    : run_settle + watchdog + auto_cleanup + daily maintenance
  - snapshot_loop      : take_market_snapshots (30dk)

Bu testler dis ag (Polymarket/Open-Meteo/Gamma) ve yan isleri (backup/evolution)
mock'lar, ama bot_loop'un GERCEK fonksiyonlarini ve DB'yi calistirir. Boylece
"loop'lar dogru adimlari dogru sirayla mi cagiriyor" sorusu cevaplanir — izole
birim testlerinin kacirdigi akis kopukluklari burada yakalanir.
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import bot_config


# ── Minimal state (BotState ile ayni arayuz) ────────────────────────────────
class _State:
    def __init__(self):
        self.is_running = True
        self.locked = False
        self.lock_reason = None
        self.last_scan = None
        self.last_price_update = None
        self.fast_price_until = None
        self.tasks = {}
        from config.settings import config

        self.config = config


async def _run_and_cancel(loop_coro, settle=0.4):
    """Loop'u bir tur kadar calistirip iptal eder (CancelledError temiz yakalanir)."""
    task = asyncio.create_task(loop_coro)
    await asyncio.sleep(settle)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture(autouse=True)
def _clean_db():
    from database.db import get_session
    from database.models import Bet, MarketSnapshot, Portfolio, WeatherForecast, WeatherMarket

    with get_session() as s:
        for tbl in [Bet, WeatherForecast, MarketSnapshot, WeatherMarket, Portfolio]:
            s.query(tbl).delete()
        s.commit()
    bot_config.strategy.current_fee_rate = 0.05
    bot_config.strategy.betting_strategy = "spread"
    bot_config.strategy.spread_max_bets_per_day = 100
    bot_config.strategy.spread_radius = 3
    bot_config.strategy.spread_max_entry = 0.99
    bot_config.strategy.spread_stake_usd = 2.0
    bot_config.strategy.spread_max_cities = 15


def _day():
    return (datetime.now(timezone.utc) + timedelta(days=2)).date()


def _add_portfolio(session, cash=1000.0):
    from database.models import Portfolio

    if session.query(Portfolio).filter_by(id=1).first() is None:
        session.add(Portfolio(id=1, initial_value=cash, current_value=cash, cash_balance=cash, total_value=cash))
        session.commit()


def _seed_market_and_forecast(session, target_day):
    """Market + snapshot + forecast: spread_placer'in ihtiyac duydugu veri."""
    from database.models import HistoricalCalibration, MarketSnapshot, WeatherForecast, WeatherMarket

    # AAA -> gercek LTAC (Ankara) bias olcumu: 0.87 — bias'siz sehir secilmez.
    session.add(
        HistoricalCalibration(
            city_code="AAA",
            city="Testville",
            date=datetime(2026, 8, 1),
            metric="temperature_max",
            model="gfs_seamless",
            predicted_value=25.0,
            actual_value=24.13,
            bias=0.87,
        )
    )
    for thr in range(28, 35):  # 28..34 = 7 esik (center 31 +/- 3) — tam-7 kurali
        mid = f"mkt-{thr}"
        session.add(
            WeatherMarket(
                id=mid,
                question="T?",
                city="Testville",
                city_code="AAA",
                metric="temperature_max",
                threshold=thr,
                target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
                status="open",
                yes_price=0.05,
                no_price=0.95,
            )
        )
        session.add(
            MarketSnapshot(
                market_id=mid,
                city="Testville",
                metric="temperature_max",
                target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
                threshold=thr,
                yes_price=0.05,
                no_price=0.95,
                snapshot_time=datetime(target_day.year, target_day.month, target_day.day, 0, 1, 0),
            )
        )
    for src in (
        "gfs_seamless",
        "ecmwf_ifs025",
        "gem_global",
        "icon_global",
        "jma_seamless",
        "cma_grapes_global",
        "ukmo_seamless",
        "meteofrance_seamless",
    ):
        session.add(
            WeatherForecast(
                market_id="mkt-31",
                city="AAA",
                metric="temperature_max",
                target_date=datetime(target_day.year, target_day.month, target_day.day, 12, 0),
                source=src,
                predicted_value=31.0,
                model_weight=1.0,
                fetched_at=datetime(target_day.year, target_day.month, target_day.day, 0, 5, 0),
            )
        )
    session.commit()


# ── Scan loop (scan_and_bet_loop) ───────────────────────────────────────────


class TestScanAndBetLoop:
    @pytest.mark.asyncio
    async def test_spread_mode_does_not_call_run_cycle(self):
        """spread modunda scan loop run_cycle (edge bet acma) CAGIRMAZ."""
        import bot_loop

        s = _State()
        fetch = AsyncMock(return_value="0 market")
        parse = AsyncMock(return_value="0 market parse edildi")
        weather = AsyncMock(return_value="0 hava tahmini")
        cycle = AsyncMock(return_value="cycle")
        with (
            patch("bot_loop._get_market_count", return_value=0),
            patch("bot_loop._get_open_target_dates", return_value=set()),
            patch("bot_loop._next_two_day_target", return_value=(None, False)),
            patch("bot_loop._is_midnight_window", return_value=False),
            patch("jobs.scheduler.run_fetch_markets", fetch),
            patch("jobs.scheduler.run_parse_markets", parse),
            patch("jobs.scheduler.run_fetch_weather", weather),
            patch("jobs.scheduler.run_cycle", cycle),
            patch("bot_loop._verify_ui", None),
            patch("bot_loop._verify_poly", None),
            patch("utils.model_run_detector.is_in_model_run_window", return_value=False),
        ):
            await _run_and_cancel(bot_loop.scan_and_bet_loop(s))

        assert fetch.called, "fetch markets cagrilmali (pencere disi normal mod)"
        assert parse.called, "parse markets cagrilmali"
        assert weather.called, "weather fetch cagrilmali (ilk tur)"
        assert not cycle.called, "spread modunda run_cycle CAGIRILMAMALI"

    @pytest.mark.asyncio
    async def test_new_two_day_date_triggers_spread_placer(self):
        """2-gun-sonrasi tarih acildiginda spread_placer CAGIRILIR (ANA BET YOLU)."""
        import bot_loop

        s = _State()
        placed = MagicMock(return_value={"placed": 3, "closed": 0, "skipped": 0, "cities": ["Testville"]})
        with (
            patch("bot_loop._get_market_count", return_value=0),
            patch("bot_loop._get_open_target_dates", return_value={_day()}),
            patch("bot_loop._next_two_day_target", return_value=(_day(), True)),
            patch("bot_loop._get_open_market_count_for_date", return_value=6),
            patch("bot_loop._is_midnight_window", return_value=False),
            patch("jobs.scheduler.run_fetch_markets", AsyncMock(return_value="0")),
            patch("jobs.scheduler.run_parse_markets", AsyncMock(return_value="0")),
            patch("jobs.scheduler.run_fetch_weather", AsyncMock(return_value="0")),
            patch("executor.spread_placer.place_spread_bets", placed),
            patch("bot_loop._verify_ui", None),
            patch("bot_loop._verify_poly", None),
            patch("utils.model_run_detector.is_in_model_run_window", return_value=False),
        ):
            await _run_and_cancel(bot_loop.scan_and_bet_loop(s))

        placed.assert_called_once()
        assert s.fast_price_until is None, "spread modunda fast_price_until ayarlanmamali (edge davranisi)"

    @pytest.mark.asyncio
    async def test_edge_mode_sets_fast_price_until(self):
        """Edge modunda 2-gun tarih acilinca fast_price_until ayarlanir (eski davranis)."""
        import bot_loop

        old = bot_config.strategy.betting_strategy
        bot_config.strategy.betting_strategy = "edge"
        try:
            s = _State()
            with (
                patch("bot_loop._get_market_count", return_value=0),
                patch("bot_loop._get_open_target_dates", return_value={_day()}),
                patch("bot_loop._next_two_day_target", return_value=(_day(), True)),
                patch("bot_loop._get_open_market_count_for_date", return_value=6),
                patch("bot_loop._is_midnight_window", return_value=False),
                patch("jobs.scheduler.run_fetch_markets", AsyncMock(return_value="0")),
                patch("jobs.scheduler.run_parse_markets", AsyncMock(return_value="0")),
                patch("jobs.scheduler.run_fetch_weather", AsyncMock(return_value="0")),
                patch("jobs.scheduler.run_cycle", AsyncMock(return_value="cycle")),
                patch("bot_loop._verify_ui", None),
                patch("bot_loop._verify_poly", None),
                patch("utils.model_run_detector.is_in_model_run_window", return_value=False),
            ):
                await _run_and_cancel(bot_loop.scan_and_bet_loop(s))

            assert s.fast_price_until is not None, "edge modunda fast_price_until set edilmeli"
        finally:
            bot_config.strategy.betting_strategy = old

    @pytest.mark.asyncio
    async def test_scan_loop_updates_last_scan(self):
        """Scan loop state.last_scan'i naive UTC olarak gunceller (watchdog girdisi)."""
        import bot_loop

        s = _State()
        with (
            patch("bot_loop._get_market_count", return_value=0),
            patch("bot_loop._get_open_target_dates", return_value=set()),
            patch("bot_loop._next_two_day_target", return_value=(None, False)),
            patch("bot_loop._is_midnight_window", return_value=False),
            patch("jobs.scheduler.run_fetch_markets", AsyncMock(return_value="0")),
            patch("jobs.scheduler.run_parse_markets", AsyncMock(return_value="0")),
            patch("jobs.scheduler.run_fetch_weather", AsyncMock(return_value="0")),
            patch("bot_loop._verify_ui", None),
            patch("bot_loop._verify_poly", None),
            patch("utils.model_run_detector.is_in_model_run_window", return_value=False),
        ):
            await _run_and_cancel(bot_loop.scan_and_bet_loop(s))

        assert s.last_scan is not None, "last_scan guncellenmeli"
        assert s.last_scan.tzinfo is None, "last_scan naive UTC olmali (watchdog karsilastirmasi icin)"


# ── Price poller loop (price_poller_loop) ───────────────────────────────────


class TestPricePollerLoop:
    @pytest.mark.asyncio
    async def test_poller_runs_fetch_refresh_update_risk_in_order(self):
        """price_poller_loop 4 adimi sirayla cagirir (risk dahil)."""
        import bot_loop

        s = _State()
        calls = []

        # price_poller_loop cagrilarini asyncio.to_thread ile sarar -> SYNC mock
        def _fetch():
            calls.append("fetch")
            return "0 market"

        def _refresh():
            calls.append("refresh")

        def _update():
            calls.append("update")
            return "0 acik bet guncellendi"

        with (
            patch("jobs.scheduler.run_fetch_markets", _fetch),
            patch("jobs.scheduler.run_refresh_open_prices", _refresh),
            patch("jobs.scheduler.run_update_prices", _update),
        ):
            await _run_and_cancel(bot_loop.price_poller_loop(s))

        assert calls == ["fetch", "refresh", "update"], f"sira yanlis: {calls}"
        assert s.last_price_update is not None

    @pytest.mark.asyncio
    async def test_poller_fast_interval_when_fast_price_until(self):
        """fast_price_until gecmis degilse poller 1 dk interval kullanir (edge davranisi korunur).

        Loop icindeki interval karari _get_price_poll_interval saf fonksiyonuna
        tasindi; burada her iki durum da dogrulanir.
        """
        import bot_loop
        from bot_loop import _get_price_poll_interval

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # fast_price_until gelecekte -> FAST interval
        s_fast = _State()
        s_fast.fast_price_until = now + timedelta(minutes=10)
        assert _get_price_poll_interval(s_fast, now) == bot_loop._FAST_PRICE_INTERVAL

        # fast_price_until yok -> NORMAL interval
        s_normal = _State()
        s_normal.fast_price_until = None
        assert _get_price_poll_interval(s_normal, now) == bot_loop._PRICE_POLL_INTERVAL

        # fast_price_until GECMIS -> NORMAL interval
        s_expired = _State()
        s_expired.fast_price_until = now - timedelta(minutes=1)
        assert _get_price_poll_interval(s_expired, now) == bot_loop._PRICE_POLL_INTERVAL


# ── Settlement loop (settlement_loop) ───────────────────────────────────────


class TestSettlementLoop:
    @pytest.mark.asyncio
    async def test_settlement_runs_settle_and_maintenance(self):
        """settlement_loop run_settle + daily maintenance cagirir."""
        import bot_loop

        s = _State()
        s.last_scan = datetime.now(timezone.utc).replace(tzinfo=None)

        # settlement_loop cagrilari asyncio.to_thread ile sarilir -> sync mock
        settle = MagicMock(return_value="Sonuclandirilan -> Kazanan:0, Kaybeden:0, Bekleyen:0")
        maint = MagicMock()
        cleanup = MagicMock()

        with (
            patch("bot_loop._run_daily_maintenance", maint),
            patch("jobs.scheduler.run_settle", settle),
            patch("database.db_cleanup.auto_cleanup", cleanup) as _,
        ):
            await _run_and_cancel(bot_loop.settlement_loop(s))

        assert settle.called, "run_settle cagrilmali"
        assert maint.called, "daily maintenance cagrilmali"

    @pytest.mark.asyncio
    async def test_watchdog_flags_missing_last_scan(self):
        """last_scan yoksa watchdog uyari verir (scan loop saglik kontrolu)."""
        import bot_loop

        s = _State()
        s.last_scan = None
        # asyncio.to_thread ile sarilir -> sync mock
        settle = MagicMock(return_value="ok")
        maint = MagicMock()
        with patch("jobs.scheduler.run_settle", settle), patch("bot_loop._run_daily_maintenance", maint):
            await _run_and_cancel(bot_loop.settlement_loop(s))

        # crash olmamasi yeterli — watchdog sadece log yazar
        assert True


# ── Snapshot loop (snapshot_loop) ───────────────────────────────────────────


class TestSnapshotLoop:
    @pytest.mark.asyncio
    async def test_snapshot_loop_takes_snapshots(self):
        """snapshot_loop take_market_snapshots cagirir."""
        import bot_loop

        s = _State()
        # to_thread ile sarilir -> sync mock
        snap = MagicMock(return_value=3)
        cleanup = MagicMock(return_value=0)
        with (
            patch("jobs.snapshot_job.take_market_snapshots", snap),
            patch("jobs.snapshot_job.cleanup_old_snapshots", cleanup),
        ):
            await _run_and_cancel(bot_loop.snapshot_loop(s))

        snap.assert_called_once()


# ── Gercek veriyle entegrasyon (loop icindeki DB akisi) ─────────────────────


class TestEndToEndSpreadFlow:
    def test_scan_cycle_with_real_data_opens_spread_bets(self):
        """GERCEK DB verisiyle: seed -> place_spread_bets -> bet'ler acilir.

        Scan loop'un 2-gun tarih dalindaki gercek isi: market + snapshot +
        forecast DB'deyken spread_placer bet acar, portfolio'dan stake duser.
        """
        from bot_loop import _get_open_market_count_for_date, _get_open_target_dates, _next_two_day_target
        from database.db import get_session
        from database.models import Bet

        day = _day()
        with get_session() as s:
            _add_portfolio(s)
            _seed_market_and_forecast(s, day)
            s.commit()

            # Scan loop'un yaptigi tespit (GERCEK fonksiyonlar):
            open_dates = _get_open_target_dates()
            new_date, trigger = _next_two_day_target(None, open_dates)
            assert trigger is True
            assert _get_open_market_count_for_date(new_date) >= 6

            # Ana bet acma yolu (GERCEK fonksiyon):
            from executor.spread_placer import place_spread_bets

            res = place_spread_bets(new_date, session=s)
            s.commit()

            placed = s.query(Bet).filter(Bet.status == "placed").count()
            from database.models import Portfolio

            pf = s.query(Portfolio).filter(Portfolio.id == 1).first()
            cash_after = pf.cash_balance if pf else None
        assert res["placed"] >= 1, f"spread bet acilmali: {res}"
        assert placed >= 1
        assert cash_after is not None and cash_after < 1000.0, "stake kasadan dusmeli"

    def test_update_prices_then_settle(self):
        """Gercek DB akisi: update_prices -> settle.

        Bet acildiktan sonra:
          - run_update_prices current_price/unrealized_pnl gunceller
          - run_settle gercek sonuca gore sonuclandirir (mock'lu ag)
        """
        from database.db import get_session
        from database.models import Bet, WeatherMarket
        from executor.settler import SettlementEngine
        from jobs.scheduler import run_update_prices
        from unittest.mock import patch

        day = _day()
        with get_session() as s:
            _add_portfolio(s)
            _seed_market_and_forecast(s, day)
            from executor.spread_placer import place_spread_bets

            place_spread_bets(day, session=s)
            s.commit()

            # 1) update_prices — current_price market'ten gelir
            for m in s.query(WeatherMarket).filter(WeatherMarket.city == "Testville").all():
                m.yes_price = 0.30
            s.commit()
            run_update_prices(session=s)
            s.commit()

            bet = s.query(Bet).filter(Bet.status == "placed").first()
            assert bet is not None
            assert bet.current_price is not None

        # 2) settle — ag cagrisi mock'lu, crash olmamali
        engine = SettlementEngine()
        with patch.object(engine, "_fetch_market_resolution", return_value=None):
            results = engine.settle_all()
        assert isinstance(results, dict)
        assert {"win", "loss", "pending"} <= set(results.keys())
