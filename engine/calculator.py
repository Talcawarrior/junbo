"""Matematiksel olasılık, Kelly kriteri hesaplayıcısı ve WeatherEngine konsensüs birleşimi."""

import asyncio
import json
import logging
import math
from datetime import datetime, timezone

import aiohttp

from config.settings import Config, bot_config, config
from database.db import get_session_or
from database.models import Analysis, Portfolio, WeatherForecast, WeatherMarket
from utils.price_sanity import is_valid_binary_price
from utils.probability import compute_effective_min_edge
from utils.probability import estimate_probability as _estimate_probability
from utils.formulas import max_bet_cap
from utils.kelly import kelly_fraction as _kelly_fraction
from utils.slippage import (
    adjust_edge_for_costs,
    adjust_kelly_for_slippage,
    estimate_slippage,
)

logger = logging.getLogger("ENGINE_CALCULATOR")


class Calculator:
    """Calculates forecasting probability, Kelly stake sizes, and analyzes markets."""

    def estimate_probability(
        self,
        forecasts: list[float],
        threshold: float,
        days_ahead: int,
        market_type: str = "HIGH",
        range_low: float | None = None,
        range_high: float | None = None,
    ) -> float:
        """Tahmin değerlerinden, market tipine göre YES olasılığını hesapla.

        Delegates to :func:`utils.probability.estimate_probability`.
        """
        if not forecasts:
            return 0.5

        mean = sum(forecasts) / len(forecasts)

        if len(forecasts) > 1:
            variance = sum((x - mean) ** 2 for x in forecasts) / (len(forecasts) - 1)
            std = math.sqrt(variance)
        else:
            std = 2.0  # Default 2C uncertainty for single source

        return _estimate_probability(
            mean=mean,
            std=std,
            threshold=threshold,
            days_ahead=days_ahead,
            market_type=market_type,
            range_low=range_low,
            range_high=range_high,
        )

    # NOTE: Kelly fraction is NOT duplicated here.
    # Use utils.kelly.kelly_fraction() instead of a local copy.
    # This method wrapper exists only to apply the strategy's kelly_fraction.
    def kelly_criterion(self, prob: float, price: float, fraction: float = 0.15) -> float:
        """Wrapper around utils.kelly.kelly_fraction + fraction multiplier."""
        f_star = _kelly_fraction(prob, price)
        return f_star * fraction

    def analyze_market(self, market_id: str, session=None) -> Analysis | None:
        """Bir marketi analiz et. Optional session for batched cycles."""
        with get_session_or(session) as session:
            market = session.query(WeatherMarket).filter_by(id=market_id).first()
            if not market:
                logger.warning(f"Market bulunamadı: {market_id}")
                return None

            if not all([market.city, market.threshold, market.target_date, market.metric]):
                logger.warning(f"Market eksik bilgi: {market_id}")
                return None

            # Price sanity check - skip invalid binary markets
            if not is_valid_binary_price(market.yes_price or 0, market.no_price or 0):
                logger.debug(
                    f"Market {market_id}: invalid prices yes={market.yes_price}, no={market.no_price}, skipping"
                )
                return None

            # Skip already-resolved markets (lookahead bias guard)
            if market.target_date <= datetime.now(timezone.utc).replace(tzinfo=None):
                logger.debug(f"Market {market_id}: target_date {market.target_date} already passed, skipping")
                return None

            # Skip markets with no real liquidity (price too low for paper realism)
            # The min_entry_price threshold is a Karpathy-search-discovered
            # lever that filters out long-shot bets (the source of the
            # asymmetric-payoff bleed where a single low-price loss wipes
            # out dozens of small wins).
            market_price = market.yes_price or 0.5
            min_price = getattr(bot_config.strategy, "min_entry_price", None) or getattr(
                bot_config, "MIN_ENTRY_PRICE", 0.01
            )
            if market_price < min_price:
                logger.debug(f"Market {market_id}: price {market_price:.4f} < min_entry_price {min_price}, skipping")
                return None

            # Karpathy-search-discovered inefficiency gate. Only bet when
            # the market price is mispriced in our favour by at least
            # `inefficiency_min`. We approximate the "naive fair price" by
            # the simple average of the YES/NO prices (0.5 midpoint adjusted
            # by yes_price deviation), and the inefficiency is the residual
            # after we compute our own estimate_probability below.
            #
            # This is a soft gate — we evaluate it AFTER we know our own
            # estimate, then check the implied market inefficiency.
            inefficiency_min = getattr(bot_config.strategy, "inefficiency_min", -1.0)

            # En son tahminleri al — query by market.metric directly.
            forecasts = (
                session.query(WeatherForecast)
                .filter(
                    WeatherForecast.market_id == market_id,
                    WeatherForecast.metric == market.metric,
                )
                .order_by(WeatherForecast.fetched_at.desc())
                .all()
            )

            # Her kaynaktan en son tahmini al + ağırlıkları topla
            latest_by_source = {}
            source_weights = {}
            for f in forecasts:
                if f.source not in latest_by_source:
                    latest_by_source[f.source] = f.predicted_value
                    source_weights[f.source] = f.model_weight or 0.0

            forecast_values = list(latest_by_source.values())

            if len(forecast_values) < bot_config.strategy.min_sources:
                logger.info(
                    f"Market {market_id}: Yetersiz kaynak ({len(forecast_values)}/{bot_config.strategy.min_sources})"
                )

            # Compute weighted std early — needed for both consensus and per-model probs
            total_weight = sum(source_weights.get(s, 0.0) for s in latest_by_source)
            if forecast_values and len(forecast_values) > 1:
                if total_weight > 0:
                    # Weighted average
                    avg = sum(latest_by_source[s] * source_weights.get(s, 0.0) for s in latest_by_source) / total_weight
                    # Weighted std
                    std_val = math.sqrt(
                        sum(source_weights.get(s, 0.0) * (latest_by_source[s] - avg) ** 2 for s in latest_by_source)
                        / total_weight
                    )
                else:
                    # Fallback to simple average if no weights
                    avg = sum(forecast_values) / len(forecast_values)
                    std_val = math.sqrt(sum((x - avg) ** 2 for x in forecast_values) / (len(forecast_values) - 1))
            else:
                avg = forecast_values[0] if forecast_values else 0.5
                std_val = None

            # days_ahead: use calendar days (>=0) and treat "today" as 1 day
            # so that (target_date=23:59:59, now=04:21) -> 0 still means "today".
            days_ahead = (market.target_date - datetime.now(timezone.utc).replace(tzinfo=None)).days
            days_ahead_for_check = max(days_ahead, 1)

            # Olasılık hesapla — weighted mean/std ile (market_type-aware)
            # RANGE markets: pass explicit bucket bounds if stored
            range_low = None
            range_high = None
            if (market.market_type or "").upper() == "RANGE":
                if market.threshold_low is not None and market.threshold_high is not None:
                    range_low = float(market.threshold_low)
                    range_high = float(market.threshold_high)
            total_std = float(std_val) if std_val is not None else 2.0
            estimated_prob = _estimate_probability(
                mean=avg,
                std=total_std,
                threshold=float(market.threshold or 0),
                days_ahead=days_ahead_for_check,
                market_type=(market.market_type or "HIGH"),
                range_low=range_low,
                range_high=range_high,
            )

            # Per-model probabilities for SIA weight optimization
            model_temps = {src: float(val) for src, val in latest_by_source.items() if val is not None}
            total_std = float(std_val) if std_val is not None else 2.0
            model_probs = {}
            for mn, mt in model_temps.items():
                mp = _estimate_probability(
                    mean=mt,
                    std=total_std,
                    threshold=float(market.threshold or 0),
                    days_ahead=days_ahead_for_check,
                    market_type=(market.market_type or "HIGH"),
                )
                model_probs[mn] = mp
            model_predictions_json = json.dumps(
                {
                    "model_temps": model_temps,
                    "model_probs": model_probs,
                }
            )

            market_implied = market.yes_price or 0.5
            raw_edge = estimated_prob - market_implied

            if raw_edge > 0:
                # YES tarafı
                kelly_frac = self.kelly_criterion(estimated_prob, market_implied, bot_config.strategy.kelly_fraction)
                recommended_side = "YES"
            else:
                # NO tarafı
                no_prob = 1 - estimated_prob
                no_implied = market.no_price or (1 - market_implied)
                no_edge = no_prob - no_implied

                if no_edge > 0:
                    kelly_frac = self.kelly_criterion(no_prob, no_implied, bot_config.strategy.kelly_fraction)
                    recommended_side = "NO"
                    raw_edge = no_edge
                else:
                    kelly_frac = 0
                    recommended_side = None

            # ── Slippage + fee adjusted edge ────────────────────────────
            # Net edge = raw edge − slippage − fee_drag.
            # This ensures the should_bet gate uses realistic post-cost
            # edge, not the raw theoretical edge that assumes perfect
            # fills at market price.
            entry_price_for_cost = (
                market_implied if recommended_side == "YES" else (market.no_price or (1 - market_implied))
            )

            # Extract condition_id from market.raw_data for orderbook slippage
            condition_id = None
            try:
                raw = json.loads(market.raw_data) if market.raw_data else {}
                for tok in raw.get("tokens", []):
                    if tok.get("outcome", "").upper() == (recommended_side or "").upper():
                        condition_id = tok.get("condition_id") or tok.get("token_id")
                        break
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

            # Preliminary bet amount for gas cost calculation (using raw edge)
            portfolio = session.query(Portfolio).filter(Portfolio.id == 1).first()
            bankroll = portfolio.total_value if portfolio and portfolio.total_value else 1000.0
            prelim_kelly = min(kelly_frac * bankroll, max_bet_cap(bankroll, Config.MAX_BET_PCT))

            net_edge = (
                adjust_edge_for_costs(raw_edge, entry_price_for_cost, bet_amount_usd=prelim_kelly)
                if recommended_side
                else 0.0
            )
            slippage_est = estimate_slippage(entry_price_for_cost, condition_id=condition_id)

            # Bet miktarı — gerçek portföyden oku (using net_edge now)
            raw_kelly_amount = min(kelly_frac * bankroll, max_bet_cap(bankroll, Config.MAX_BET_PCT))
            # Reduce Kelly size by estimated slippage cost
            recommended_amount = adjust_kelly_for_slippage(raw_kelly_amount, entry_price_for_cost)

            # Bet açılmalı mı?
            # NOTE: Polymarket'te public-search'ten gelen marketlerin
            # `liquidity` alanı genelde 0 (price bize zaten gerçek bilgi veriyor),
            # bu yüzden likidite kontrolünü kaldırıyoruz — gerçek piyasa sinyali
            # `volume` veya `volume24hr` alanlarından biridir; bunlar da yoksa
            # `current_price` zaten likiditeyi yansıtır.
            # Yine de kullanıcı isterse `bot_config.strategy.min_liquidity`
            # değerini 0 yaparak bunu bypass edebilir.
            liquidity_ok = (
                market.liquidity or 0
            ) >= bot_config.strategy.min_liquidity or bot_config.strategy.min_liquidity <= 0
            effective_min_edge = self._compute_effective_min_edge(market, std_val)

            # ── Karpathy-search inefficiency gate ─────────────────────────
            # The "inefficiency" is the residual between our estimated
            # probability and the price-implied naive probability. In the
            # backtest harness this is the same construction (naive ensemble
            # average + independent noise). In the live system we don't
            # observe the inefficiency directly, but a good proxy is the
            # edge itself: an edge of `e` means the market is mispriced by
            # `e` in our favour. The Karpathy search found that requiring
            # `inefficiency_min` of -0.124 (i.e. accept even slightly
            # adverse inefficiency as long as other gates pass) gave the
            # best risk-adjusted return. We translate that to a *minimum
            # absolute edge* requirement on top of effective_min_edge.
            #
            # For a positive inefficiency_min (e.g. +0.067), we require the
            # edge to be at least that large. For negative values, the gate
            # is effectively disabled (we already require min_edge > 0).
            if inefficiency_min > 0:
                inefficiency_ok = abs(raw_edge) >= inefficiency_min
            else:
                inefficiency_ok = True

            should_bet = (
                abs(net_edge) >= effective_min_edge
                and inefficiency_ok
                and len(forecast_values) >= bot_config.strategy.min_sources
                and 0 <= days_ahead <= bot_config.strategy.max_days_ahead
                and liquidity_ok
                and recommended_amount > 1.0
            )

            reason_parts = []
            if abs(net_edge) < effective_min_edge:
                reason_parts.append(
                    f"Net edge düşük: {net_edge:.2%} (raw={raw_edge:.2%}, slip={slippage_est.slippage_pct:.2%})"
                )
            if not inefficiency_ok:
                reason_parts.append(f"İnefficiency düşük: edge {net_edge:.2%} < {inefficiency_min:.2%}")
            if len(forecast_values) < bot_config.strategy.min_sources:
                reason_parts.append(f"Az kaynak: {len(forecast_values)}")
            if days_ahead > bot_config.strategy.max_days_ahead:
                reason_parts.append(f"Çok uzak: {days_ahead} gün")
            if (market.liquidity or 0) < bot_config.strategy.min_liquidity:
                reason_parts.append(f"Düşük likidite: ${market.liquidity}")

            if not reason_parts:
                reason = (
                    f"BET AC! Edge={net_edge:.2%} "
                    f"(raw={raw_edge:.2%}), "
                    f"Side={recommended_side}, "
                    f"slip={slippage_est.model_used}"
                )
            else:
                reason = "PASS: " + ", ".join(reason_parts)

            avg_val = sum(forecast_values) / len(forecast_values) if forecast_values else None

            analysis = Analysis(
                market_id=market_id,
                estimated_probability=estimated_prob,
                market_implied_prob=market_implied,
                edge=net_edge,
                raw_edge=raw_edge,
                slippage_pct=slippage_est.slippage_pct,
                avg_forecast_value=avg_val,
                std_forecast_value=std_val,
                num_sources=len(forecast_values),
                recommended_side=recommended_side,
                recommended_amount=recommended_amount,
                confidence_score=min(len(forecast_values) / 5, 1.0),
                should_bet=should_bet,
                reason=reason,
                model_predictions=model_predictions_json,
                analyzed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(analysis)
            logger.info(
                f"Market {market_id}: prob={estimated_prob:.2%}, "
                f"market={market_implied:.2%}, raw_edge={raw_edge:.2%}, "
                f"net_edge={net_edge:.2%} (slip={slippage_est.slippage_pct:.2%}), "
                f"should_bet={should_bet}, kelly_raw=${raw_kelly_amount:.2f}, kelly_adj=${recommended_amount:.2f}"
            )
            return analysis

    @staticmethod
    def _compute_effective_min_edge(market, std: float | None = None) -> float:
        """Time-to-close-scaled min_edge. Delegates to utils.probability."""
        return compute_effective_min_edge(market, std=std)


# WeatherEngine kept for seamless FastAPI / backward compatibility
OPEN_METEO_MODEL_MAP = {
    "gfs_seamless": "gfs_seamless",
    "ecmwf_ifs04": "ecmwf_ifs025",
    "gem_seamless": "gem_global",
    "icon_seamless": "icon_global",
    "jma_msm": "jma_seamless",
    "cma_grapes_global": "cma_grapes_global",
    "ukmo_seamless": "ukmo_seamless",
    "meteofrance_seamless": "meteofrance_seamless",
}

METRIC_MAP = {
    "temperature_max": "temperature_2m_max",
    "temperature_min": "temperature_2m_min",
    "temperature_2m_max": "temperature_2m_max",
    "temperature_2m_min": "temperature_2m_min",
}


class WeatherEngine:
    """Weather engine consensus calculator (FastAPI / test compatibility wrapper)."""

    def __init__(self, db_session_factory=None, cfg=None):
        self.db_session_factory = db_session_factory
        self.config = cfg or config
        self.model_weights = self.config.get_normalized_weights()
        # Local cache for the current session to avoid redundant fetches (e.g. max/min overlap)
        self._forecast_cache = {}

    @staticmethod
    def _compute_effective_min_edge(market, std: float | None = None) -> float:
        """Return the time-to-close-scaled min_edge. Delegates to utils.probability."""
        return compute_effective_min_edge(market, std=std)

    async def get_multi_model_forecast(
        self,
        city_code: str,
        latitude: float,
        longitude: float,
        target_date: datetime | None = None,
        market_ids: list[str] = None,
        db_session=None,
        metric: str = "temperature_2m_max",
    ) -> dict | None:
        if not city_code or (latitude == 0 and longitude == 0):
            return None
        if target_date is None:
            target_date = datetime.now(timezone.utc).replace(tzinfo=None)

        api_model_names = []
        for internal_name in self.model_weights.keys():
            api_name = OPEN_METEO_MODEL_MAP.get(internal_name, internal_name)
            if api_name not in api_model_names:
                api_model_names.append(api_name)
        models_str = ",".join(api_model_names)

        # Cache check
        target_str = target_date.strftime("%Y-%m-%d")
        cache_key = (round(latitude, 4), round(longitude, 4), target_str)
        if cache_key in self._forecast_cache:
            data = self._forecast_cache[cache_key]
            logger.debug("Ensemble cache hit for %s", cache_key)
        else:
            url = f"{Config.OPEN_METEO_API}/forecast"
            params = {
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "models": models_str,
                "forecast_days": 14,
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 429:
                            logger.warning("Ensemble API: Open-Meteo 429 Rate Limit! Waiting 30s...")
                            await asyncio.sleep(30)
                            return None
                        if resp.status != 200:
                            return None
                        data = await resp.json()
                        self._forecast_cache[cache_key] = data
            except Exception as e:
                logger.error("get_multi_model_forecast fetch error: %s", e)
                return None

        try:
            model_temps = {}
            daily_data = data.get("daily", {})
            times = daily_data.get("time", [])
            if not times:
                return None

            target_idx = None
            for i, t in enumerate(times):
                if t.startswith(target_str):
                    target_idx = i
                    break

            # Timezone robustness fix: Open-Meteo with `timezone=auto` returns
            # daily buckets in *local* time. For cities east of UTC (e.g. Seoul
            # at UTC+9), the local "today" can be one day ahead of UTC "today",
            # so the UTC target_str is not in the response. Similarly, cities
            # west of UTC (e.g. Los Angeles at UTC-8) can return a date that
            # is one day *behind* UTC today for the first bucket.
            #
            # Strategy: if exact match not found, fall back to the bucket whose
            # calendar date is closest to the target_date (within ±1 day). This
            # matches what the Polymarket market question means by "today" —
            # the local calendar day at the city, not UTC.
            if target_idx is None:
                try:
                    target_d = target_date.date()
                    best_idx = None
                    best_delta = None
                    for i, t in enumerate(times):
                        try:
                            d = datetime.strptime(t, "%Y-%m-%d").date()
                        except ValueError:
                            continue
                        delta = abs((d - target_d).days)
                        if best_delta is None or delta < best_delta:
                            best_delta = delta
                            best_idx = i
                    # Only accept the closest match if it is within 1 day,
                    # otherwise the lookup is genuinely stale and we should
                    # return None to avoid silently returning wrong-day data.
                    if best_idx is not None and best_delta is not None and best_delta <= 1:
                        target_idx = best_idx
                        logger.info(
                            "Timezone fallback: target=%s not in API response; using closest bucket %s (delta=%d day)",
                            target_str,
                            times[target_idx],
                            best_delta,
                        )
                except Exception as e:
                    logger.debug("Timezone fallback failed: %s", e)

            if target_idx is None:
                logger.warning(
                    "get_multi_model_forecast: target_date=%s not found in API dates %s",
                    target_str,
                    times[:5],
                )
                return None

            for internal_name in self.model_weights.keys():
                api_name = OPEN_METEO_MODEL_MAP.get(internal_name, internal_name)
                # Use the metric requested to pick the right daily data key
                # although we fetch both max and min.
                api_metric = "temperature_2m_max"
                if "min" in metric.lower():
                    api_metric = "temperature_2m_min"

                key = f"{api_metric}_{api_name}"
                if key in daily_data:
                    temps = daily_data[key]
                    if target_idx < len(temps) and temps[target_idx] is not None:
                        model_temps[internal_name] = temps[target_idx]

            if not model_temps:
                return None

            # Calculate consensus
            total_weight = sum(self.model_weights.get(m, 0.0) for m in model_temps)
            if total_weight == 0:
                return None
            weighted_mean = sum(self.model_weights.get(m, 0.0) * t for m, t in model_temps.items()) / total_weight
            weighted_var = (
                sum(self.model_weights.get(m, 0.0) * (t - weighted_mean) ** 2 for m, t in model_temps.items())
                / total_weight
            )
            weighted_std = max(weighted_var**0.5, 0.5)

            if db_session is not None and market_ids:
                from database.models import WeatherForecast

                for mid in market_ids:
                    for mn, tmp in model_temps.items():
                        db_session.add(
                            WeatherForecast(
                                market_id=mid,
                                city=city_code,
                                lat=latitude,
                                lon=longitude,
                                target_date=target_date,
                                metric=metric,
                                source=mn,
                                predicted_value=float(tmp),
                                model_weight=self.model_weights.get(mn, 0.0),
                                fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
                                raw_data=str({"model": mn, "temp": tmp, "ensemble": True}),
                            )
                        )
                try:
                    db_session.commit()
                    logger.info(
                        "Ensemble persisted for %d markets, coords=(%s, %s)",
                        len(market_ids),
                        latitude,
                        longitude,
                    )
                except Exception as e:
                    db_session.rollback()
                    logger.error("Failed to persist ensemble: %s", e)

            return {
                "weighted_mean": weighted_mean,
                "weighted_std": weighted_std,
                "model_count": len(model_temps),
                "model_temps": model_temps,
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        except Exception as e:
            logger.error("get_multi_model_forecast error: %s", e)
            return None

    def _db_consensus(self, market_id: str) -> dict | None:
        if not market_id or not self.db_session_factory:
            return None
        db = self.db_session_factory()
        try:
            from database.models import WeatherForecast

            fcs = (
                db.query(WeatherForecast)
                .filter(WeatherForecast.market_id == market_id)
                .order_by(WeatherForecast.fetched_at.desc())
                .limit(30)
                .all()
            )
            if not fcs:
                return None
            lat = {}
            for f in fcs:
                if f.source not in lat:
                    lat[f.source] = (
                        f.predicted_value,
                        self.model_weights.get(f.source, 0.0),
                    )
            tw = sum(w for _, w in lat.values())
            if tw <= 0:
                vs = [v for v, _ in lat.values()]
                m = sum(vs) / len(vs)
                s = max((sum((v - m) ** 2 for v in vs) / len(vs)) ** 0.5, 0.5) if len(vs) > 1 else 1.0
                return {"weighted_mean": m, "weighted_std": s}
            wm = sum(v * w for v, w in lat.values()) / tw
            wv = sum(w * (v - wm) ** 2 for v, w in lat.values()) / tw
            return {"weighted_mean": wm, "weighted_std": max(wv**0.5, 0.5)}
        except Exception:
            return None
        finally:
            db.close()

    def calculate_probability_above(self, strike_temp: float, consensus=None, market_id=""):
        """P(YES) for a HIGH market — delegates to shared estimate_probability."""
        if not consensus:
            consensus = self._db_consensus(market_id)
        if not consensus:
            return 0.5
        return _estimate_probability(
            mean=consensus["weighted_mean"],
            std=consensus["weighted_std"],
            threshold=strike_temp,
            days_ahead=0,
            market_type="HIGH",
        )

    def calculate_probability_below(self, strike_temp: float, consensus=None, market_id=""):
        """P(YES) for a LOW market — delegates to shared estimate_probability."""
        if not consensus:
            consensus = self._db_consensus(market_id)
        if not consensus:
            return 0.5
        return _estimate_probability(
            mean=consensus["weighted_mean"],
            std=consensus["weighted_std"],
            threshold=strike_temp,
            days_ahead=0,
            market_type="LOW",
        )

    async def get_forecast(
        self,
        city_code: str,
        latitude: float,
        longitude: float,
        target_date: datetime | None = None,
    ) -> dict | None:
        return await self.get_multi_model_forecast(city_code, latitude, longitude, target_date)

    def update_model_weights(self, new_weights: dict):
        self.model_weights = new_weights
