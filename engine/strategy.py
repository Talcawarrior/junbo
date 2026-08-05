"""Sinyal analizi, Kelly kasa yönetimi ve risk kontrolü."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func

from config.settings import bot_config, config
from database.models import (
    OPEN_BET_STATUSES,
    Bet,
    Portfolio,
)
from utils.formulas import conservative_portfolio_value, max_exposure_cap
from utils.kelly import kelly_bet_amount

logger = logging.getLogger("STRATEGY_ENGINE")


class SimpleSignal:
    """Lightweight signal object for inter-module compatibility."""

    market_id: str = ""
    city: str = ""
    city_code: str = ""
    outcome: str = "YES"
    entry_price: float = 0.5
    fair_value: float = 0.5
    edge: float = 0.0
    probability: float = 0.5
    bet_size: float = 0.0
    side: str = "YES"

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class RiskManager:
    """Risk management with Kelly sizing and circuit breakers."""

    def __init__(self, db_session=None, cfg=None):
        self.db = db_session
        self.config = cfg or config
        self.portfolio_value = getattr(self.config, "INITIAL_PORTFOLIO", 1000.0)
        self.daily_pnl = 0.0
        # Track drawdown via a high-water-mark monitor.
        from utils.drawdown_monitor import DrawdownMonitor

        self.drawdown = DrawdownMonitor(peak=self.portfolio_value)
        # Load current portfolio value from DB so exposure cap uses
        # the actual portfolio, not just INITIAL_PORTFOLIO.
        self._load_portfolio_from_db()
        self.open_bets_count = 0
        self.city_bet_counts: dict[str, int] = {}
        self._last_pnl_date: datetime | None = None
        self._load_from_db()

    def _load_portfolio_from_db(self):
        """Load current portfolio total_value from DB."""
        if not self.db:
            return
        try:
            from database.models import Portfolio

            p = self.db.query(Portfolio).filter(Portfolio.id == 1).first()
            if p and p.total_value:
                self.portfolio_value = float(p.total_value)
        except Exception as e:
            logger.warning("portfolio load fallback: %s", e)

    def update_portfolio(self, value: float):
        """Update portfolio value."""
        self.portfolio_value = value

    def update_daily_pnl(self, pnl: float):
        """Update daily PnL and check circuit breaker."""
        now = datetime.now(timezone.utc)
        if self._last_pnl_date is None or self._last_pnl_date.date() != now.date():
            if self._last_pnl_date is not None:
                logger.info("Daily PnL reset for new day (was $%.2f)", self.daily_pnl)
            self.daily_pnl = 0.0
            self._last_pnl_date = now
        self.daily_pnl += pnl
        if self.daily_loss_limit_amount > 0 and self.daily_pnl <= -self.daily_loss_limit_amount:
            logger.warning("DAILY STOP-LOSS TRIGGERED! PnL: $%.2f", self.daily_pnl)
            return False
        return True

    def check_city_cap(self, city_code: str) -> bool:
        """Check city cap limit."""
        current_count = self.city_bet_counts.get(city_code, 0)
        return current_count < self.config.CITY_CAP

    def increment_city_bet(self, city_code: str):
        """Increment city bet count."""
        self.city_bet_counts[city_code] = self.city_bet_counts.get(city_code, 0) + 1

    def decrement_city_bet(self, city_code: str):
        """Decrement city bet count."""
        if city_code in self.city_bet_counts:
            self.city_bet_counts[city_code] = max(0, self.city_bet_counts[city_code] - 1)

    def calculate_kelly_bet_size(self, model_prob: float, market_price: float) -> float:
        """Calculate Kelly bet sizing.

        Thin wrapper over utils.kelly.kelly_bet_amount so the math
        lives in one place. Bankroll comes from self.portfolio_value,
        which the portfolio-sync hook refreshes after every settlement
        cycle (PR #9).
        """
        return kelly_bet_amount(
            self.portfolio_value,
            model_prob,
            market_price,
            fraction=self.config.KELLY_FRACTION,
            min_bet=self.config.MIN_BET_SIZE,
            max_bet_pct=self.config.MAX_BET_PCT,
        )

    def check_exposure_cap(self, current_exposure: float, additional_bet: float) -> bool:
        """Check total exposure cap limit.

        Portfolio = initial_capital + realized_pnl (unrealized katilmaz).
        Limit = portfolio * 25%. Her gun PnL sermayeye eklenir.
        """
        conservative_value = self._conservative_portfolio_value()
        max_exposure = max_exposure_cap(
            self.config.INITIAL_PORTFOLIO,
            conservative_value - self.config.INITIAL_PORTFOLIO,
            self.config.TOTAL_EXPOSURE_PCT,
        )
        if (current_exposure + additional_bet) > max_exposure:
            logger.warning(
                "Exposure cap: $%.2f + $%.2f = $%.2f > $%.2f (25%% of $%.2f conservative)",
                current_exposure,
                additional_bet,
                current_exposure + additional_bet,
                max_exposure,
                conservative_value,
            )
            return False
        return True

    def _conservative_portfolio_value(self) -> float:
        """Portfolio = dünkü kapanış sermayesi (bugünkü realize edilmemiş).

        Bugünden önce kapanan bahislerin PnL'i hesaba katılır.
        Bugün realizado olan kârlar bugünkü exposure cap'ini şişirmez.
        Yarınki başlangıç = bugünkü kapanış.

        Bu sayede:
        - Daily starting capital = önceki günün kapanış sermayesi
        - Max exposure = %25 × dünkü kapanış
        - Feedback loop önlenir (unrealized PnL dahil edilmez)

        Formula from: utils/formulas.py → conservative_portfolio_value()
        """
        if not self.db:
            return self.portfolio_value
        try:
            from datetime import datetime, timezone

            from sqlalchemy import or_

            from database.models import Bet

            initial = self.config.INITIAL_PORTFOLIO
            # Sadece BUGÜNDEN ÖNCE kapanan bahislerin PnL'i
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            realized = float(
                self.db.query(func.coalesce(func.sum(Bet.pnl), 0.0))
                .filter(
                    Bet.status.in_(("won", "lost", "settled", "closed_early")),
                    or_(
                        Bet.settled_at < today_start,
                        Bet.closed_at < today_start,
                    ),
                )
                .scalar()
                or 0.0
            )
            # Use central formula
            return conservative_portfolio_value(initial, realized)
        except Exception as e:
            logger.warning("conservative_portfolio fallback: %s", e)
            return self.portfolio_value

    @property
    def daily_loss_limit_amount(self) -> float:
        """Günlük zarar limiti = dünkü kapanış sermayesi × DAILY_LOSS_LIMIT."""
        return self._conservative_portfolio_value() * self.config.DAILY_LOSS_LIMIT

    def is_bot_locked(self) -> bool:
        """Check if bot is locked."""
        return self.daily_loss_limit_amount > 0 and self.daily_pnl <= -self.daily_loss_limit_amount

    def get_daily_pnl(self) -> float:
        """Get daily PnL."""
        return self.daily_pnl

    def get_total_exposure(self) -> float:
        """Get total exposure (sum of `amount` for all open/active/placed bets)."""
        if self.db:
            try:
                # Include all open-style statuses so freshly-placed bets are
                # counted in exposure. "placed" is what BetPlacer writes
                # immediately after writing the Bet row. Use `Bet.amount`
                # (the column BetPlacer actually writes) rather than the
                # legacy `stake_amount` which stays at 0.
                total = (
                    self.db.query(func.coalesce(func.sum(Bet.amount), 0.0))
                    .filter(Bet.status.in_(OPEN_BET_STATUSES))
                    .scalar()
                )
                return float(total or 0.0)
            except Exception:
                pass
        exposure = sum(self.city_bet_counts.values()) * 20.0
        return exposure

    def get_portfolio_value(self) -> float:
        """Get portfolio value."""
        return self.portfolio_value

    def _load_from_db(self):
        """Load state from DB."""
        if not self.db:
            return
        try:
            portfolio = self.db.query(Portfolio).filter(Portfolio.id == 1).first()
            if portfolio:
                self.portfolio_value = portfolio.total_value or portfolio.initial_value or self.portfolio_value
                self.daily_pnl = portfolio.daily_pnl or 0.0

            active = self.db.query(Bet).filter(Bet.status.in_(OPEN_BET_STATUSES)).all()
            self.city_bet_counts = {}
            self.open_bets_count = len(active)
            for bet in active:
                cc = bet.city_code or "unknown"
                self.city_bet_counts[cc] = self.city_bet_counts.get(cc, 0) + 1
        except Exception as e:
            logger.warning("Risk load from DB warning: %s", e)

    # ──────────────────────────────────────────────
    # Active Risk Management — Position-Level Methods
    # ──────────────────────────────────────────────
    # These methods evaluate individual positions for early exit (stop-loss,
    # take-profit, time decay, trailing stop) and portfolio rebalancing.
    #
    # risk_config comes from bot_config.risk (RiskConfig dataclass in settings.py)

    def _get_risk_config(self):
        """Return risk config with fallback defaults."""
        try:
            from config.settings import bot_config

            return bot_config.risk
        except Exception:
            from config.settings import RiskConfig

            return RiskConfig()

    def check_stop_loss(self, bet, current_price: float, market=None) -> tuple:  # pylint: disable=unused-argument
        """Stop-loss: pozisyon %stop_loss_pct'den fazla zarardaysa kapat."""
        from utils.formulas import pnl_ratio

        cfg = self._get_risk_config()
        raw = bet.entry_price if bet.entry_price is not None else bet.price
        entry = float(raw) if raw is not None else 0.0
        if entry <= 0:
            return False, ""
        ratio = pnl_ratio(current_price, entry)
        if ratio <= -cfg.stop_loss_pct:
            return True, f"stop_loss: {ratio:.1%}"
        return False, ""

    def check_take_profit(self, bet, current_price: float, market=None) -> tuple:  # pylint: disable=unused-argument
        """Take-profit: pozisyon %take_profit_pct'den fazla kardaysa veya fiyat 0.98'e ulaştıysa kapat.

        Partial take-profit: düşük girişli ("lottery ticket") bahislerde,
        ~%100 kârda sadece ana parayı kurtaracak kadar satılır, kalan pozisyon
        trailing stop ile "free ride" devam eder. (Bizim RiskConfig flat'tır;
        spec'teki tier sistemi YOK — sadece entry fiyatı + kâr ile tetiklenir.)
        Bu fonksiyon SADECE karar verir; pozisyon küçültme + muhasebe
        scheduler._partial_close_early içinde yapılır (çift mutasyon yok).
        """
        from utils.formulas import pnl_ratio

        cfg = self._get_risk_config()
        raw = bet.entry_price if bet.entry_price is not None else bet.price
        entry = float(raw) if raw is not None else 0.0
        if entry <= 0:
            return False, ""

        # Partial TP: zaten yapıldıysa tekrar tetikleme (trailing stop'a bırak)
        if bool(getattr(bet, "partial_tp_done", False)):
            return False, ""

        # Partial TP: düşük giriş (<=0.35) ve ~%100 kâr
        if entry <= 0.35 and current_price > 0:
            profit_pct = (current_price - entry) / entry
            if profit_pct >= 1.0:
                fraction_to_sell = entry / current_price
                if 0 < fraction_to_sell < 1:
                    # Karar yeterli; scheduler pozisyonu küçültür.
                    return (
                        True,
                        f"partial_take_profit: sold {fraction_to_sell:.1%} @ {current_price:.2f}",
                    )

        # Normal (tam) take-profit
        ratio = pnl_ratio(current_price, entry)
        if ratio >= cfg.take_profit_pct:
            return True, f"take_profit: {ratio:.1%}"
        return False, ""

    def check_time_decay(self, bet, current_price: float, market) -> tuple:
        """Time decay: settlement'a <time_decay_hours kala ve zarardaysa kapat."""
        from utils.formulas import pnl_ratio

        cfg = self._get_risk_config()
        if not market or not hasattr(market, "target_date"):
            return False, ""
        try:
            resolution = market.target_date
            if not resolution:
                return False, ""
            # Naive datetime'leri timezone-aware yap
            if resolution.tzinfo is None:
                resolution = resolution.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            hours_left = (resolution - now).total_seconds() / 3600
            if hours_left <= 0:
                return False, ""  # Zaten geçmiş, settlement halleder
            if hours_left <= cfg.time_decay_hours:
                raw = bet.entry_price if bet.entry_price is not None else bet.price
                entry = float(raw) if raw is not None else 0.0
                if entry > 0:
                    ratio = pnl_ratio(current_price, entry)
                    if ratio <= cfg.time_decay_threshold:
                        return (
                            True,
                            f"time_decay: {hours_left:.1f}h left, {ratio:.1%}",
                        )
        except Exception:
            pass
        return False, ""

    def check_trailing_stop(self, bet, current_price: float) -> tuple:
        """Trailing stop: en yüksek fiyattan %trailing_stop_pct düşüşte kapat.

        Sadece pozisyon kâra geçmişse (peak > entry) tetiklenir.
        Peak <= entry ise pozisyon hiç kâra geçmemiş, TS koruma sağlamaz.
        """
        from utils.formulas import drop_ratio

        cfg = self._get_risk_config()
        raw = bet.entry_price if bet.entry_price is not None else bet.price
        entry = float(raw) if raw is not None else 0.0
        if entry <= 0:
            return False, ""

        # Peak price'ı result_data'dan oku veya ilk defa set et
        peak = entry
        if bet.result_data:
            try:
                data = json.loads(bet.result_data) if isinstance(bet.result_data, str) else {}
                peak = float(data.get("peak_price", entry))
            except Exception:
                peak = entry

        # Yeni tepe noktası var mı?
        if current_price > peak:
            peak = current_price
            # Güncellenmiş peak değerini kaydet
            try:
                data = json.loads(bet.result_data) if isinstance(bet.result_data, str) else {}
                if not isinstance(data, dict):
                    data = {}
                data["peak_price"] = peak
                bet.result_data = json.dumps(data)
            except Exception:
                pass

        # Sadece pozisyon kâra geçmişse (peak > entry) TS uygula
        # Peak <= entry ise pozisyon hiç kâra geçmemiş, TS tetiklenmesin
        if peak <= entry:
            return False, ""

        # Tepeden düşüş kontrolü
        if peak > 0:
            ratio = drop_ratio(peak, current_price)
            if ratio >= cfg.trailing_stop_pct:
                return (
                    True,
                    f"trailing_stop: dropped {ratio:.1%} from peak {peak:.3f}",
                )

        return False, ""

    def check_early_exit(self, bet, current_price: float, market=None) -> tuple:
        """Tüm erken çıkış kontrollerini sırayla çalıştır.

        Returns: (should_exit: bool, reason: str)
        """
        # Minimum hold: bet aynı scan döngüsünde açıldıysa kapatma.
        # Bu, resolve edilmiş market'lere anında bet açılıp kapanmasını engeller.
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        placed = getattr(bet, "placed_at", None)
        if placed:
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            hold_seconds = (now - placed).total_seconds()
            if hold_seconds < 21600:  # 6 saat minimum hold (piyasa gurultusu rotasyonlari engelle)
                return False, "Hold (minimum hold period: 6h)"

        # 1. Stop-loss (range betlerde devre disi)
        order_id = getattr(bet, "order_id", "") or ""
        is_range = isinstance(order_id, str) and order_id.startswith("range_")
        if not is_range:
            exit_bool, reason = self.check_stop_loss(bet, current_price, market)
            if exit_bool:
                return True, reason

        # 2. Take-profit
        exit_bool, reason = self.check_take_profit(bet, current_price, market)
        if exit_bool:
            return True, reason

        # 3. Trailing stop (range betlerde devre disi)
        if not is_range:
            exit_bool, reason = self.check_trailing_stop(bet, current_price)
        if exit_bool:
            return True, reason

        # 4. Time decay (sadece non-range betlerde, market objesi varsa)
        if not is_range and market is not None:
            exit_bool, reason = self.check_time_decay(bet, current_price, market)
            if exit_bool:
                return True, reason

        return False, "Hold"

    def check_rebalance(self, new_signal, active_bets: list) -> object:
        """Yeni yüksek-edge fırsatı için eski pozisyonu kapatmaya değer mi?

        Returns: Kapatılacak Bet nesnesi veya None
        """
        cfg = self._get_risk_config()
        new_edge = getattr(new_signal, "edge", 0.0) or (isinstance(new_signal, dict) and new_signal.get("edge", 0.0))

        for bet in active_bets:
            # Bet edge'ini fair_value - entry_price'dan hesapla
            bet_edge = float(getattr(bet, "expected_value", 0) or 0)
            bet_pnl = float(getattr(bet, "unrealized_pnl", 0) or 0)
            bet_stake = float(getattr(bet, "stake", bet.amount or 1))
            bet_return_pct = bet_pnl / bet_stake if bet_stake > 0 else 0

            # Yeni edge eski edge'in min_rebalance_edge_ratio katı mı?
            if bet_edge > 0 and new_edge > bet_edge * cfg.min_rebalance_edge_ratio:
                # Eski pozisyon zararda mı?
                if bet_return_pct <= cfg.rebalance_min_loss:
                    return bet

        return None

    def check_model_reversal(self, bet, analysis) -> tuple:
        """Model olasılığı ters yönde önemli ölçüde değiştiyse erken çık.

        Returns: (should_exit: bool, reason: str)
        """
        if not analysis:
            return False, ""
        try:
            # Bet'in açıldığı andaki model prob'u fair_value'da saklı
            entry_prob = float(getattr(bet, "fair_value", 0.5) or 0.5)
            current_prob = float(getattr(analysis, "estimated_probability", 0.5) or 0.5)

            if entry_prob <= 0 or current_prob <= 0:
                return False, ""

            prob_change = current_prob - entry_prob
            bet_pnl = float(getattr(bet, "unrealized_pnl", 0) or 0)
            bet_stake = float(getattr(bet, "stake", bet.amount or 1))
            return_pct = bet_pnl / bet_stake if bet_stake > 0 else 0

            # Model prob'u %20+ ters yönde değiştiyse ve zarardaysak çık
            if prob_change <= -0.20 and return_pct <= -0.10:
                return (
                    True,
                    f"model_reversal: prob {entry_prob:.0%}->{current_prob:.0%} ({prob_change:.0%})",
                )

            # Model prob'u %30+ ters yönde değiştiyse (karda da olsak çık)
            if prob_change <= -0.30:
                return (
                    True,
                    f"model_reversal: prob {entry_prob:.0%}->{current_prob:.0%} ({prob_change:.0%})",
                )

        except Exception:
            pass
        return False, ""

    def calculate_position_size_with_risk(self, signal, portfolio_value: float) -> float:
        """Kelly + risk limitleri ile pozisyon boyutu hesapla.

        Akış:
        1. Kelly criterion ile ideal boyut
        2. max_bet_pct (%3) sınırı
        3. Smart pool (%40 dokunulmaz)
        4. Exposure cap (%25)
        5. City cap kontrolü
        """
        model_prob = getattr(
            signal,
            "probability",
            getattr(
                signal,
                "model_prob",
                (signal.get("model_prob") if isinstance(signal, dict) else 0.5),
            ),
        )
        market_price = getattr(
            signal,
            "entry_price",
            (signal.get("entry_price") if isinstance(signal, dict) else 0.5),
        )

        if model_prob is None or model_prob <= 0 or market_price is None or market_price <= 0:
            return 0.0

        # 1. Kelly — use passed portfolio_value, not self.portfolio_value
        kelly_size = kelly_bet_amount(
            portfolio_value,
            model_prob,
            market_price,
            fraction=self.config.KELLY_FRACTION,
            min_bet=self.config.MIN_BET_SIZE,
            max_bet_pct=self.config.MAX_BET_PCT,
        )

        # 2. Exposure cap (conservative: initial + realized_before_today)
        conservative_value = self._conservative_portfolio_value()
        current_exposure = self.get_total_exposure()
        max_exposure = conservative_value * self.config.TOTAL_EXPOSURE_PCT
        remaining_cap = max(0, max_exposure - current_exposure)
        kelly_size = min(kelly_size, remaining_cap)

        # Only enforce MIN_BET_SIZE if Kelly actually recommends betting
        if kelly_size <= 0:
            return 0.0
        return max(kelly_size, self.config.MIN_BET_SIZE)


class BettingEngine:
    """Signal analysis, single-fill betting, and position management."""

    def __init__(self, db_session=None, risk_manager=None, weather_engine=None):
        self.db = db_session
        self.risk_manager = risk_manager
        self.weather_engine = weather_engine
        self.config = config

    def analyze_signal(self, market_data: dict, model_prob: float, side: str = "YES") -> dict | None:
        """Analyze signal, calculate edge and EV."""
        yes_price = market_data.get("yes_price", 0.5)
        if side.upper() == "NO":
            market_price = 1.0 - yes_price
            edge = (1.0 - model_prob) - market_price
        else:
            market_price = yes_price
            edge = model_prob - market_price

        # Tek kaynak: bot_config.strategy.current_fee_rate (dinamik fee)
        fee_rate = bot_config.strategy.current_fee_rate
        ev = edge - fee_rate * market_price * (1 - market_price)
        # min_edge check calculator.py'de effective_min_edge ile yapılıyor.
        # Burada sadece EV pozitif mi diye bakıyoruz — çifte kontrol kaldırıldı.
        is_eligible = ev > 0

        if not is_eligible:
            return None

        return {
            "city_code": market_data.get("city_code", ""),
            "strike_temp": market_data.get("strike_temp", 0),
            "market_type": market_data.get("market_type", "HIGH"),
            "model_prob": model_prob,
            "market_price": market_price,
            "edge": round(edge, 4),
            "ev": round(ev, 4),
            "is_eligible": True,
            "side": side,
        }

    def calculate_position_size(self, signal: dict, portfolio_value: float, risk_manager) -> float:
        """Calculate position size using fractional Kelly and exposure caps.

        Checks the drawdown monitor to de-risk (alpha < 1.0) or halt
        entirely when the bankroll has fallen significantly from its peak.
        """
        # ── Drawdown gate: halt if critical, scale if yellow ─────────
        dd = getattr(risk_manager, "drawdown", None)
        if dd is not None:
            if dd.halt():
                return 0.0
            alpha = dd.alpha_multiplier()
        else:
            alpha = 1.0

        market_price = signal["market_price"]
        kelly_size = risk_manager.calculate_kelly_bet_size(signal.get("model_prob", 0.5), market_price)

        current_exposure = 0.0
        if risk_manager and hasattr(risk_manager, "get_total_exposure"):
            current_exposure = risk_manager.get_total_exposure()

        if not risk_manager.check_exposure_cap(current_exposure, kelly_size):
            # Use conservative portfolio value (initial + realized only)
            conservative_value = risk_manager._conservative_portfolio_value()
            max_allowed = (conservative_value * self.config.TOTAL_EXPOSURE_PCT) - current_exposure
            kelly_size = min(kelly_size, max_allowed)

        # Apply drawdown de-risk multiplier
        kelly_size *= alpha

        # Only enforce MIN_BET_SIZE if Kelly actually recommends betting
        if kelly_size <= 0:
            return 0.0
        return max(kelly_size, self.config.MIN_BET_SIZE)

    async def analyze_market(self, market_data, portfolio_value, forecast=None):
        """Wrapper for analyzing a specific market."""
        if not market_data:
            return None

        if isinstance(market_data, dict):
            city = market_data.get("city", "Unknown")
            city_code = market_data.get("city_code", "")
            strike_temp = market_data.get("strike_temp", 25.0)
            market_type = market_data.get("market_type", "HIGH")
            yes_price = market_data.get("yes_price", 0.5)
        else:
            city = getattr(market_data, "city", "Unknown")
            city_code = getattr(market_data, "city_code", "")
            strike_temp = getattr(market_data, "strike_temp", 25.0)
            market_type = getattr(market_data, "market_type", "HIGH")
            yes_price = getattr(market_data, "yes_price", 0.5) or getattr(market_data, "current_yes_bid", 0.5)

        model_prob = 0.55
        side = "YES"  # YES-only: asla NO bahis açma
        if forecast:
            from utils.probability import estimate_probability as _ep

            try:
                mean = forecast.get("weighted_mean", 0) if isinstance(forecast, dict) else 0
                std = forecast.get("weighted_std", 0) if isinstance(forecast, dict) else 0
                model_prob = _ep(
                    mean=float(mean),
                    std=float(std),
                    threshold=float(strike_temp),
                    days_ahead=0,
                    market_type=str(market_type),
                )
                side = "YES"  # YES-only: model_prob ne olursa olsun sadece YES
            except Exception:
                model_prob = 0.55

        signal_dict = self.analyze_signal(
            {
                "city_code": city_code,
                "city": city,
                "strike_temp": strike_temp,
                "market_type": market_type,
                "yes_price": yes_price,
                "market_price": yes_price,
            },
            model_prob,
            side=side,
        )

        if not signal_dict:
            return None

        bet_size = 10.0
        if self.risk_manager and hasattr(self.risk_manager, "calculate_position_size"):
            try:
                bet_size = self.calculate_position_size(signal_dict, portfolio_value, self.risk_manager)
            except Exception as e:
                logger.warning("Position size calculation failed: %s", e)
                bet_size = min(bet_size, bot_config.strategy.max_bet_amount)
        signal_dict["bet_size"] = bet_size

        sig = SimpleSignal(
            market_id=(
                getattr(market_data, "market_id", "")
                if not isinstance(market_data, dict)
                else market_data.get("market_id", "")
            ),
            city=city,
            city_code=city_code,
            outcome="YES" if model_prob >= 0.5 else "NO",
            entry_price=yes_price,
            fair_value=model_prob,
            edge=signal_dict.get("edge", 0),
            probability=model_prob,
            bet_size=bet_size,
            side=side,
        )

        return sig

    async def execute_signal(self, signal, market_data):
        """Wrapper for placing a simulated/paper bet."""
        city = getattr(signal, "city", "Unknown")
        bet_size = getattr(signal, "bet_size", 10.0)
        logger.info("Placing bet for %s size $%.2f", city, bet_size)

        try:
            if isinstance(market_data, dict):
                market_id = market_data.get("market_id") or market_data.get("event_id") or ""
                city_code = market_data.get("city_code", "")
                yes_price = market_data.get("yes_price", 0.5)
            else:
                market_id = getattr(market_data, "market_id", getattr(market_data, "event_id", ""))
                city_code = getattr(market_data, "city_code", "")
                yes_price = getattr(market_data, "yes_price", 0.5) or getattr(market_data, "current_yes_bid", 0.5)

            # Idempotency: return existing open bet if one exists for this market
            if self.db:
                existing = (
                    self.db.query(Bet)
                    .filter(Bet.market_id == str(market_id), Bet.status.in_(OPEN_BET_STATUSES))
                    .first()
                )
                if existing is not None:
                    logger.info("Duplicate bet refused for %s — returning existing bet #%s", market_id, existing.id)
                    return existing

            # YES-only guard: asla NO bahis acma
            if getattr(signal, "side", "YES").upper() == "NO" or getattr(signal, "outcome", "YES").upper() == "NO":
                logger.info("NO bet refused for %s — YES-only mode", market_id)
                return None

            # YES-only price gate: [0.10, 0.95)
            min_price = float(getattr(bot_config.strategy, "min_entry_price", 0.10))
            max_price = float(getattr(bot_config.strategy, "max_entry_price", 0.95))
            if not (min_price <= float(yes_price) < max_price):
                logger.info(
                    "Price gate: %s yes_price=%.3f outside [%.2f, %.2f)", market_id, yes_price, min_price, max_price
                )
                return None

            # 24-hour settlement window: only bet on markets settling within 24h
            try:
                from database.models import WeatherMarket as _WM

                _market_row = self.db.query(_WM).filter(_WM.id == str(market_id)).first() if self.db else None
                if _market_row and _market_row.target_date:
                    _res = _market_row.target_date
                    if getattr(_res, "tzinfo", None) is None:
                        from datetime import timezone as _tz

                        _res = _res.replace(tzinfo=_tz.utc)
                    _hours_left = (_res - datetime.now(timezone.utc)).total_seconds() / 3600.0
                    if _hours_left > 24 or _hours_left <= 0:
                        logger.info("24h guard: %s has %.1fh left — bet refused", market_id, _hours_left)
                        return None
            except Exception:
                pass

            bet = Bet(
                market_id=str(market_id),
                city_code=city_code,
                city=city,
                outcome=getattr(signal, "outcome", "YES"),
                stake_amount=bet_size,
                entry_price=getattr(signal, "entry_price", yes_price),
                current_price=getattr(signal, "entry_price", yes_price),
                fair_value=getattr(signal, "fair_value", 0.5),
                expected_value=getattr(signal, "edge", 0.0),
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                strike_temp=getattr(signal, "strike_temp", 25.0)
                or (
                    market_data.get("strike_temp")
                    if isinstance(market_data, dict)
                    else getattr(market_data, "strike_temp", 25.0)
                ),
                side=getattr(signal, "side", "YES"),
                status="active",
                placed_at=datetime.now(timezone.utc),
            )
            if self.db:
                self.db.add(bet)
                self.db.flush()  # assign ID without committing (caller manages transaction)
                self.db.refresh(bet)
                if self.risk_manager and hasattr(self.risk_manager, "increment_city_bet"):
                    self.risk_manager.increment_city_bet(city_code)
            return bet
        except Exception as e:
            logger.error("Bet DB insert failed; aborting placement (no fallback bet): %s", e)
            if self.db:
                self.db.rollback()
            return None
