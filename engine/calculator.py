"""Matematiksel olasilik, Kelly kriteri hesaplayicisi ve WeatherEngine konsensus birlesimi."""

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
from utils.model_blacklist import get_blacklisted_models

logger = logging.getLogger("ENGINE_CALCULATOR")

# Global rate-limit flag: ilk 429'te 5dk boyunca tum Open-Meteo isteklerini durdur
import time as _time  # noqa: E402

_RATE_LIMITED_UNTIL = 0.0  # monotonic timestamp


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
        """Tahmin degerlerinden, market tipine gore YES olasiligini hesapla.

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
                logger.warning(f"Market bulunamadi: {market_id}")
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

            # Her kaynaktan en son tahmini al + agirliklari topla
            latest_by_source = {}
            source_weights = {}
            for f in forecasts:
                if f.source not in latest_by_source:
                    latest_by_source[f.source] = f.predicted_value
                    source_weights[f.source] = f.model_weight or 0.0

            # ── Model blacklist filter ────────────────────────────────────
            # Remove unreliable model-city pairs identified by backtest.
            blacklisted = get_blacklisted_models(market.city_code or "", market.metric or "temperature_max")
            if blacklisted:
                removed = []
                for bl_model in blacklisted:
                    if bl_model in latest_by_source:
                        removed.append(bl_model)
                        del latest_by_source[bl_model]
                        source_weights.pop(bl_model, None)
                if removed:
                    logger.info(
                        "Blacklist [%s]: removed %s (%d models remain)",
                        market.city_code,
                        ", ".join(removed),
                        len(latest_by_source),
                    )

            forecast_values = list(latest_by_source.values())

            if len(forecast_values) < bot_config.strategy.min_sources:
                logger.info(
                    f"Market {market_id}: Yetersiz kaynak ({len(forecast_values)}/{bot_config.strategy.min_sources})"
                )

            # days_ahead: use calendar days (>=0) and treat "today" as 1 day
            # so that (target_date=23:59:59, now=04:21) -> 0 still means "today".
            days_ahead = (market.target_date - datetime.now(timezone.utc).replace(tzinfo=None)).days

            # ── Lead-time based dynamic weighting ───────────────────────
            # Research shows ECMWF HRES outperforms GFS at all lead times,
            # but the advantage is most pronounced at short lead times.
            # 0-48h:  ECMWF 1.3x boost, GFS 0.7x penalty
            # 48-120h: ECMWF 1.1x boost, GFS 0.9x penalty
            # 120h+:  No adjustment (both degrade similarly)
            ECMWF_MODELS = {"ecmwf_ifs025", "ecmwf_ifs04"}
            GFS_MODELS = {"gfs_seamless"}
            if days_ahead <= 2:
                # Short lead time: ECMWF boost, GFS penalty
                for s in source_weights:
                    if s in ECMWF_MODELS:
                        source_weights[s] *= 1.3
                    elif s in GFS_MODELS:
                        source_weights[s] *= 0.7
            elif days_ahead <= 5:
                # Medium lead time: mild ECMWF boost
                for s in source_weights:
                    if s in ECMWF_MODELS:
                        source_weights[s] *= 1.1
                    elif s in GFS_MODELS:
                        source_weights[s] *= 0.9

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

            days_ahead_for_check = max(days_ahead, 1)

            # Olasilik hesapla — weighted mean/std ile (market_type-aware)
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

            # Per-model probabilities
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

            market_implied = market.yes_price if market.yes_price is not None else 0.5
            raw_edge = estimated_prob - market_implied

            if raw_edge > 0:
                # YES tarafi
                kelly_frac = self.kelly_criterion(estimated_prob, market_implied, bot_config.strategy.kelly_fraction)
                recommended_side = "YES"
            else:
                # YES-only: model_prob <= market_implied ise bahis acilmaz
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

            # Bet miktari — gercek portfoyden oku (using net_edge now)
            raw_kelly_amount = min(kelly_frac * bankroll, max_bet_cap(bankroll, Config.MAX_BET_PCT))
            # Reduce Kelly size by estimated slippage cost
            recommended_amount = adjust_kelly_for_slippage(raw_kelly_amount, entry_price_for_cost)

            # Bet acilmali mi?
            # NOTE: Polymarket'te public-search'ten gelen marketlerin
            # `liquidity` alani genelde 0 (price bize zaten gercek bilgi veriyor),
            # bu yuzden likidite kontrolunu kaldiriyoruz — gercek piyasa sinyali
            # `volume` veya `volume24hr` alanlarindan biridir; bunlar da yoksa
            # `current_price` zaten likiditeyi yansitir.
            # Yine de kullanici isterse `bot_config.strategy.min_liquidity`
            # degerini 0 yaparak bunu bypass edebilir.
            liquidity_ok = (
                market.liquidity or 0
            ) >= bot_config.strategy.min_liquidity or bot_config.strategy.min_liquidity <= 0
            effective_min_edge = self._compute_effective_min_edge(market, std_val)

            # 8-hour pre-settlement guard
            settlement_hours_left = None
            try:
                if market.target_date:
                    _res = market.target_date
                    if getattr(_res, "tzinfo", None) is None:
                        _res = _res.replace(tzinfo=timezone.utc)
                    settlement_hours_left = (_res - datetime.now(timezone.utc)).total_seconds() / 3600.0
            except Exception:
                pass
            settlement_ok = settlement_hours_left is None or settlement_hours_left > 8

            should_bet = (
                recommended_side == "YES"  # YES-only: asla NO
                and net_edge >= effective_min_edge  # post-cost edge gate
                and len(forecast_values) >= bot_config.strategy.min_sources
                and 0 <= days_ahead <= bot_config.strategy.max_days_ahead
                and liquidity_ok
                and settlement_ok
                and recommended_amount > 1.0
            )

            reason_parts = []
            if recommended_side != "YES":
                reason_parts.append("YES-only mode: NO side rejected")
            if net_edge < effective_min_edge:
                reason_parts.append(
                    f"Net edge dusuk: {net_edge:.2%} (raw={raw_edge:.2%}, slip={slippage_est.slippage_pct:.2%})"
                )
            if len(forecast_values) < bot_config.strategy.min_sources:
                reason_parts.append(f"Az kaynak: {len(forecast_values)}")
            if days_ahead > bot_config.strategy.max_days_ahead:
                reason_parts.append(f"Cok uzak: {days_ahead} gun")
            if (market.liquidity or 0) < bot_config.strategy.min_liquidity:
                reason_parts.append(f"Dusuk likidite: ${market.liquidity}")
            if not settlement_ok:
                reason_parts.append(f"Settlement'a {settlement_hours_left:.1f}s kaldi (8s min)")

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

    # _compute_effective_min_edge Calculator sinifinda (satir 364) tanimli.

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
        # `_time` and `_RATE_LIMITED_UNTIL` are module-level globals; the
        # 429 pause must mutate the global so it persists across calls.
        global _RATE_LIMITED_UNTIL
        if not city_code or (latitude == 0 and longitude == 0):
            return None
        if target_date is None:
            target_date = datetime.now(timezone.utc).replace(tzinfo=None)

        global _RATE_LIMITED_UNTIL, _time
        # Global rate-limit kontrolu
        if _time.monotonic() < _RATE_LIMITED_UNTIL:
            logger.debug("Rate-limited, skipping API call for %s", city_code)
            return None

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
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 429:
                            # Global rate-limit: tum dongu boyunca API'yi engelle
                            _RATE_LIMITED_UNTIL = _time.monotonic() + 300  # 5dk
                            logger.warning("Ensemble 429 — all API calls paused for 5min")
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

                # Dedup: skip (market, source) rows we already have for this
                # date/metric so repeated hourly fetches don't append duplicates.
                existing_keys = {
                    (f.market_id, f.source)
                    for f in db_session.query(WeatherForecast).filter(
                        WeatherForecast.market_id.in_(market_ids),
                        WeatherForecast.source.in_(list(model_temps.keys())),
                        WeatherForecast.target_date == target_date,
                        WeatherForecast.metric == metric,
                    )
                }
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                for mid in market_ids:
                    for mn, tmp in model_temps.items():
                        if (mid, mn) in existing_keys:
                            continue
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
                                fetched_at=now_utc,
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
