"""Junbo - Polymarket Weather Prediction Bot - Configuration Dataclasses & Legacy Config."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Compute repo root (parent of config/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env from repo root
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _resolve_path(path_value: str, default_relative: str) -> str:
    """Resolve relative paths to absolute from repo root."""
    raw = path_value or default_relative
    if os.path.isabs(raw):
        return raw
    return os.path.join(BASE_DIR, raw)


@dataclass
class PolymarketConfig:
    """Polymarket specific configurations."""

    api_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    weather_keywords: list = None  # type: ignore[assignment]

    # Fee rates by category (dynamic, fetched from API)
    fee_categories: dict = None  # {"weather": 0.05, ...}

    def __post_init__(self):
        self.weather_keywords = [
            "temperature",
            "heat",
            "cold",
            "snow",
            "rain",
            "hurricane",
            "storm",
            "weather",
            "°F",
            "°C",
            "celsius",
            "fahrenheit",
            "precipitation",
            "highest",
        ]

        # Initialize fee categories if not provided
        if self.fee_categories is None:
            self.fee_categories = {
                "weather": 0.05,    # Weather markets: 5% fee
            }


@dataclass
class MeteoConfig:
    """Weather service API configurations."""

    openmeteo_url: str = "https://api.open-meteo.com/v1/forecast"
    weatherapi_key: str = os.getenv("WEATHERAPI_KEY", "")
    weatherapi_url: str = "https://api.weatherapi.com/v1"


@dataclass
class StrategyConfig:
    """Strategy & bankroll metrics."""

    # Polymarket temperature markets in /public-search almost never
    # produce 5%+ edge because the market price already discounts the
    # public NWS/Open-Meteo consensus.  5% is enough to cover bookmaker
    # vig + a thin profit margin in paper mode.  Can be lowered once a
    # private weather feed (e.g. ECMWF-direct) gives a structural edge.
    min_edge: float = 0.05  # 5% edge minimum (must exceed 2% fee_drag + margin)
    max_bet_amount: float = 3.0  # Maximum $3 per bet (binde 3 of $1,000)
    max_bet_pct: float = 0.003  # Max bet as % of portfolio (single source of truth)
    min_bet_size: float = 1.0  # Minimum bet size in USD
    total_exposure_pct: float = 0.25  # Max total exposure as % of portfolio
    min_liquidity: float = 0.0  # Liquidity check disabled: Polymarket public-search
    # markets don't expose a `liquidity` field reliably
    # (it's always 0). The current_price already reflects
    # real market depth.
    # ── Orderbook depth filter ───────────────────────────────────────
    # Minimum USD depth (at our fill price ±2 ticks) required to place a bet.
    # 0.0 = disabled (current default, relies on entry price filter).
    # Recommended: 50.0 (require $50 of depth near our fill).
    # The depth is checked from the live orderbook via ResolvedMarkets API.
    # If the API call fails, the filter is skipped (graceful degradation).
    min_depth_usd: float = 0.0
    kelly_fraction: float = 0.15  # Quarter/Fractional Kelly (aligned with Junbo 15%)
    # Time-to-close edge escalation. As a market approaches its
    # resolution time, Polymarket prices move fast on the public
    # weather consensus and forecast uncertainty is already low.
    # We demand a stronger edge in the last N hours before close
    # so the bot is less willing to take a late bet at a thin edge.
    # Linear ramp: 1x min_edge at edge_escalation_hours, then
    # ramps to edge_escalation_multiplier * min_edge at 0h.
    edge_escalation_hours: int = 24
    edge_escalation_multiplier: float = 2.0
    min_sources: int = 2  # En az 2 kaynak (openmeteo + weatherapi ile calisiyor)

    # ── Polymarket Dynamic Fee Rate (fetched from API) ──────────────────────
    # Default: 5% (Weather category). Fetch from Polymarket API at startup.
    # If API fails, fallback to this default.
    fee_rate_weather: float = 0.05
    current_fee_rate: float = 0.05  # Updated dynamically from API

    fee_drag: float = 0.02  # Polymarket taker fee %2
    # Bot scope: today + 1 + 2 days ahead (0..2 inclusive).
    # Tightened from 14 to 2 so the bot only trades near-term markets
    # where the public weather ensemble (GFS/ECMWF/ICON/...) is still
    # calibrated. Forecasts degrade past 3 days.
    max_days_ahead: int = 2

    # ── Karpathy-search-discovered levers (asymmetric-payoff fix) ────────
    # These were tuned by `scripts/karpathy_search.py` against 90 days /
    # 15 cities of historical_calibrations data. The defaults below are
    # deliberately permissive (min_entry_price=0.01 = accept anything,
    # inefficiency_min=-1.0 = accept anything) so the unit tests that
    # exercise the calculator with low-price markets still work.
    #
    # In production, the tuned values (min_entry_price≈0.35,
    # inefficiency_min≈-0.124) are loaded from data/strategy_params.json
    # by `apply_persisted_strategy_params()` at import time. That file is
    # written by the Karpathy search script.
    #
    # Background: a naive Kelly bot wins ~94% of its trades but loses
    # money overall because the 6% losing trades are at low prices
    # (long-shot bets) where a single loss wipes out dozens of small
    # wins. Setting MIN_ENTRY_PRICE higher filters out the long shots;
    # INEFFICIENCY_MIN only takes trades where the market price looks
    # mispriced in our favour by at least that much.
    min_entry_price: float = 0.01
    inefficiency_min: float = -1.0  # negative = gate disabled (accept all)

    # ── Slippage model ────────────────────────────────────────────────
    # "flat"   — fixed slippage_pct from strategy_params.json
    # "tiered" — 3-tier by entry price (<0.05: 3%, 0.05-0.10: 1%, >0.10: 0.5%)
    # "orderbook" — live depth-based (future, falls back to tiered)
    slippage_model: str = "orderbook"
    slippage_pct: float = 0.005  # used when slippage_model="flat"
    gas_cost_usd: float = 0.10  # Polygon gas per round-trip

    # ── Flat bet override & Daily loss limit (synced from Config) ─────────
    flat_bet_usd: float = 0.0  # 0 = use Kelly sizing, >0 = fixed $ per bet
    daily_loss_limit: float = 0.05  # 5% daily max loss


@dataclass
class RiskConfig:
    """Active risk management: position-level stop-loss, take-profit, time decay, rebalance."""

    # Position-level limits
    stop_loss_pct: float = 0.30  # %30 kayıpta otomatik kapat
    take_profit_pct: float = 1.0  # %100 karda otomatik kapat
    trailing_stop_pct: float = 0.15  # %15 trailing stop (tepeden düşüşte)

    # Time-based exits
    time_decay_hours: int = 24  # Settlement'a bu kadar saat kala
    time_decay_threshold: float = -0.10  # %10 zarardaysa kapat

    # Rebalancing
    min_rebalance_edge_ratio: float = 2.0  # Yeni edge en az 2x eski edge
    rebalance_min_loss: float = -0.15  # Rebalance için min zarar eşiği

    # Risk management loop interval (seconds)


# ── Large constant dicts (module-level, shared by all) ────────────────────
_ICAO_COORDS = {
    # Turkey (4)
    "LTAC": (39.9891, 32.8236),
    "LTFM": (41.2753, 28.7519),
    "LTBJ": (38.2924, 27.1569),
    "LTAI": (36.8987, 30.8005),
    # USA (15)
    "KDAL": (32.8471, -96.8517),
    "KMIA": (25.7959, -80.2870),
    "KORD": (41.9742, -87.9073),
    "KLGA": (40.7769, -73.8740),
    "KLAX": (33.9416, -118.4085),
    "KLAS": (36.0840, -115.1537),
    "KPHX": (33.4343, -112.0080),
    "KIAH": (29.9844, -95.3414),
    "KATL": (33.6407, -84.4277),
    "KBOS": (42.3656, -71.0096),
    "KSEA": (47.4502, -122.3088),
    "KDEN": (39.8617, -104.6732),
    "KDCA": (38.8521, -77.0377),
    "KSFO": (37.6188, -122.3750),
    "KMCO": (28.4294, -81.3089),
    # Canada / Mexico (5)
    "CYYZ": (43.6777, -79.6308),
    "CYVR": (49.1947, -123.1792),
    "CYUL": (45.4706, -73.7408),
    "MMMX": (19.4363, -99.0721),
    "MMGL": (20.5218, -103.3112),
    # South America (5)
    "SBGR": (-23.4356, -46.4731),
    "SBGL": (-22.8089, -43.2436),
    "SAEZ": (-34.8222, -58.5358),
    "SCEL": (-33.3930, -70.7858),
    "SPJC": (-12.0219, -77.1143),
    # Europe (15)
    "EGLL": (51.4700, -0.4543),
    "LFPG": (49.0099, 2.5479),
    "EDDT": (52.5597, 13.2877),
    "UUEE": (55.9726, 37.4146),
    "EDDF": (50.0379, 8.5622),
    "EHAM": (52.3105, 4.7683),
    "LEMD": (40.4983, -3.5676),
    "LIRF": (41.8003, 12.2389),
    "LEBL": (41.2974, 2.0833),
    "EDDM": (48.3538, 11.7861),
    "LSZH": (47.4581, 8.5480),
    "LOWW": (48.1103, 16.5697),
    "ESSA": (59.6498, 17.9294),
    "LGAV": (37.9364, 23.9472),
    "LPPT": (38.7750, -9.1354),
    # Middle East (3)
    "OMDB": (25.2532, 55.3657),
    "LLBG": (32.0114, 34.8867),
    "OTHH": (25.2731, 51.6081),
    # Asia (12)
    "RJTT": (35.5533, 139.7811),
    "RJOO": (34.7882, 135.4381),
    "ZSPD": (31.1434, 121.8052),
    "ZBAA": (40.0799, 116.6031),
    "RKSS": (37.4602, 126.4407),
    "VHHH": (22.3080, 113.9185),
    "RCTP": (25.0764, 121.2338),
    "WSSS": (1.3592, 103.9894),
    "VTBS": (13.6926, 100.7501),
    "WIII": (-6.1256, 106.6559),
    "VABB": (19.0887, 72.8679),
    "VIDP": (28.5562, 77.1000),
    # Oceania (3)
    "YSSY": (-33.9399, 151.1753),
    "YMML": (-37.6690, 144.8410),
    "NZAA": (-37.0082, 174.7918),
    # Africa (2)
    "HECA": (30.1219, 31.4056),
    "FACT": (-33.9694, 18.5972),
}

_CITY_ICAO_MAP = {
    "ankara": "LTAC",
    "istanbul": "LTFM",
    "izmir": "LTBJ",
    "antalya": "LTAI",
    "dallas": "KDAL",
    "miami": "KMIA",
    "chicago": "KORD",
    "new york": "KLGA",
    "newyork": "KLGA",
    "los angeles": "KLAX",
    "las vegas": "KLAS",
    "phoenix": "KPHX",
    "houston": "KIAH",
    "atlanta": "KATL",
    "boston": "KBOS",
    "seattle": "KSEA",
    "denver": "KDEN",
    "washington": "KDCA",
    "san francisco": "KSFO",
    "orlando": "KMCO",
    "toronto": "CYYZ",
    "vancouver": "CYVR",
    "montreal": "CYUL",
    "mexico city": "MMMX",
    "guadalajara": "MMGL",
    "sao paulo": "SBGR",
    "rio de janeiro": "SBGL",
    "buenos aires": "SAEZ",
    "santiago": "SCEL",
    "lima": "SPJC",
    "london": "EGLL",
    "paris": "LFPG",
    "berlin": "EDDT",
    "moscow": "UUEE",
    "frankfurt": "EDDF",
    "amsterdam": "EHAM",
    "madrid": "LEMD",
    "rome": "LIRF",
    "barcelona": "LEBL",
    "munich": "EDDM",
    "zurich": "LSZH",
    "vienna": "LOWW",
    "stockholm": "ESSA",
    "athens": "LGAV",
    "lisbon": "LPPT",
    "dubai": "OMDB",
    "tel aviv": "LLBG",
    "doha": "OTHH",
    "tokyo": "RJTT",
    "osaka": "RJOO",
    "shanghai": "ZSPD",
    "beijing": "ZBAA",
    "seoul": "RKSS",
    "hong kong": "VHHH",
    "taipei": "RCTP",
    "singapore": "WSSS",
    "bangkok": "VTBS",
    "jakarta": "WIII",
    "mumbai": "VABB",
    "delhi": "VIDP",
    "sydney": "YSSY",
    "melbourne": "YMML",
    "auckland": "NZAA",
    "cairo": "HECA",
    "cape town": "FACT",
}


@dataclass
class BotConfig:
    """Combined configurations — single source of truth for ALL config."""

    # ── Portfolio ──────────────────────────────────────────────────
    initial_portfolio: float = 1000.0
    max_exposure_pct: float = 0.25
    city_cap: int = 4
    weather_fee_rate: float = 0.05

    # ── Intervals ──────────────────────────────────────────────────
    scan_interval: int = 300
    settlement_interval: int = 120
    sia_interval: int = 86400
    # Midnight scan: after 00:00, scan every N seconds for the first
    # MIDNIGHT_SCAN_WINDOW minutes to catch 2-day-ahead markets early
    # (earlier = cheaper prices on Polymarket).
    midnight_scan_interval: int = 60  # seconds between scans after midnight
    midnight_scan_window: int = 60  # minutes after midnight to use fast scan

    # ── API URLs ───────────────────────────────────────────────────
    polymarket_gamma_api: str = "https://gamma-api.polymarket.com"
    polymarket_clob_api: str = "https://clob.polymarket.com"
    open_meteo_api: str = "https://api.open-meteo.com/v1"

    # ── Database ───────────────────────────────────────────────────
    db_path: str = ""  # set from .env in __post_init__
    db_echo: bool = False

    # ── Logging ────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = ""  # set from .env in __post_init__
    log_format: str = "%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s"

    # ── Runtime ────────────────────────────────────────────────────
    dry_run: bool = True
    temp_unit: str = "celsius"
    host: str = "127.0.0.1"
    port: int = 8093

    # ── Model weights ──────────────────────────────────────────────
    model_weights: dict = None  # type: ignore[assignment]

    # ── Constants ──────────────────────────────────────────────────
    icao_coords: dict = None  # type: ignore[assignment]
    city_icao_map: dict = None  # type: ignore[assignment]

    # ── Nested configs ─────────────────────────────────────────────
    polymarket: PolymarketConfig = None  # type: ignore[assignment]
    meteo: MeteoConfig = None  # type: ignore[assignment]
    strategy: StrategyConfig = None  # type: ignore[assignment]
    risk: RiskConfig = None  # type: ignore[assignment]

    def __post_init__(self):
        self.polymarket = self.polymarket or PolymarketConfig()
        self.meteo = self.meteo or MeteoConfig()
        self.strategy = self.strategy or StrategyConfig()
        self.risk = self.risk or RiskConfig()

        # ── Override from .env (single source: .env > dataclass defaults) ──
        self.initial_portfolio = float(os.getenv("INITIAL_PORTFOLIO", str(self.initial_portfolio)))
        self.max_exposure_pct = float(os.getenv("MAX_EXPOSURE_PCT", str(self.max_exposure_pct)))
        self.city_cap = int(os.getenv("CITY_CAP", str(self.city_cap)))
        self.weather_fee_rate = float(os.getenv("WEATHER_FEE_RATE", str(self.weather_fee_rate)))
        self.scan_interval = int(os.getenv("SCAN_INTERVAL", str(self.scan_interval)))
        self.settlement_interval = int(os.getenv("SETTLEMENT_INTERVAL", str(self.settlement_interval)))
        self.sia_interval = int(os.getenv("SIA_INTERVAL", str(self.sia_interval)))
        self.midnight_scan_interval = int(os.getenv("MIDNIGHT_SCAN_INTERVAL", str(self.midnight_scan_interval)))
        self.midnight_scan_window = int(os.getenv("MIDNIGHT_SCAN_WINDOW", str(self.midnight_scan_window)))
        self.host = os.getenv("HOST", self.host)
        self.port = int(os.getenv("PORT", str(self.port)))
        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        self.log_level = os.getenv("LOG_LEVEL", self.log_level)
        self.db_echo = os.getenv("DB_ECHO", "false").lower() == "true"

        # Resolve paths
        self.db_path = _resolve_path(os.getenv("DB_PATH") or "", "data/bot.db")
        self.log_file = _resolve_path(os.getenv("LOG_FILE") or "", "logs/bot.log")

        # ── Constants (large dicts) ───────────────────────────────
        if self.model_weights is None:
            self.model_weights = {
                "gfs_seamless": 0.30,
                "ecmwf_ifs025": 0.25,
                "gem_global": 0.15,
                "icon_global": 0.10,
                "jma_seamless": 0.08,
                "cma_grapes_global": 0.05,
                "ukmo_seamless": 0.04,
                "meteofrance_seamless": 0.03,
            }
        if self.icao_coords is None:
            self.icao_coords = _ICAO_COORDS
        if self.city_icao_map is None:
            self.city_icao_map = _CITY_ICAO_MAP

        # ── Strategy: override from .env ───────────────────────────
        s = self.strategy
        s.max_bet_pct = float(os.getenv("MAX_BET_PCT", str(s.max_bet_pct)))
        s.min_bet_size = float(os.getenv("MIN_BET_SIZE", str(s.min_bet_size)))
        s.kelly_fraction = float(os.getenv("KELLY_FRACTION", str(s.kelly_fraction)))
        s.daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", str(s.daily_loss_limit)))
        s.min_entry_price = float(os.getenv("MIN_ENTRY_PRICE", str(s.min_entry_price)))
        s.flat_bet_usd = float(os.getenv("FLAT_BET_USD", str(s.flat_bet_usd)))


# ── Config backward-compatibility proxy ────────────────────────────────────
# All reads/writes go through bot_config (single source of truth).
# This eliminates the dual Config / bot_config drift problem.


class _ConfigProxy:
    """Backward-compatible proxy. Delegates all attribute access to bot_config.

    Usage: ``Config.MAX_BET_PCT`` reads ``bot_config.strategy.max_bet_pct``.
    Assignment: ``Config.MAX_BET_PCT = 0.01`` writes back to ``bot_config``.
    """

    _MAP: dict[str, tuple[str, str]] = {
        # root-level BotConfig fields
        "INITIAL_PORTFOLIO": ("root", "initial_portfolio"),
        "MAX_EXPOSURE_PCT": ("root", "max_exposure_pct"),
        "CITY_CAP": ("root", "city_cap"),
        "WEATHER_FEE_RATE": ("root", "weather_fee_rate"),
        "SCAN_INTERVAL": ("root", "scan_interval"),
        "SETTLEMENT_INTERVAL": ("root", "settlement_interval"),
        "SIA_INTERVAL": ("root", "sia_interval"),
        "MIDNIGHT_SCAN_INTERVAL": ("root", "midnight_scan_interval"),
        "MIDNIGHT_SCAN_WINDOW": ("root", "midnight_scan_window"),
        "POLYMARKET_GAMMA_API": ("root", "polymarket_gamma_api"),
        "POLYMARKET_CLOB_API": ("root", "polymarket_clob_api"),
        "OPEN_METEO_API": ("root", "open_meteo_api"),
        "OPEN_METEO_BASE": ("root", "open_meteo_api"),
        "MODEL_WEIGHTS": ("root", "model_weights"),
        "LOG_LEVEL": ("root", "log_level"),
        "LOG_FILE": ("root", "log_file"),
        "LOG_FORMAT": ("root", "log_format"),
        "DB_PATH": ("root", "db_path"),
        "DB_ECHO": ("root", "db_echo"),
        "TEMP_UNIT": ("root", "temp_unit"),
        "DRY_RUN": ("root", "dry_run"),
        "HOST": ("root", "host"),
        "PORT": ("root", "port"),
        "ICAO_COORDS": ("root", "icao_coords"),
        "CITY_ICAO_MAP": ("root", "city_icao_map"),
        # strategy-level fields
        "MAX_BET_PCT": ("strategy", "max_bet_pct"),
        "MIN_BET_SIZE": ("strategy", "min_bet_size"),
        "KELLY_FRACTION": ("strategy", "kelly_fraction"),
        "MIN_ENTRY_PRICE": ("strategy", "min_entry_price"),
        "FLAT_BET_USD": ("strategy", "flat_bet_usd"),
        "DAILY_LOSS_LIMIT": ("strategy", "daily_loss_limit"),
        "FEE_DRAG": ("strategy", "fee_drag"),
        "TOTAL_EXPOSURE_PCT": ("strategy", "total_exposure_pct"),
    }

    def _resolve(self, name: str):
        """Return ``(target_obj, attr_name)`` for a Config attribute."""
        if name in self._MAP:
            section, attr = self._MAP[name]
            target = bot_config.strategy if section == "strategy" else bot_config
            return target, attr
        return None, name

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        target, attr = self._resolve(name)
        if target is not None and hasattr(target, attr):
            return getattr(target, attr)
        raise AttributeError(f"'Config' has no attribute '{name}'")

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        target, attr = self._resolve(name)
        if target is not None:
            setattr(target, attr, value)
        else:
            object.__setattr__(self, name, value)

    # ── Convenience methods ────────────────────────────────────────────

    @property
    def daily_loss_limit_amount(self) -> float:
        """Return absolute daily loss limit amount."""
        return bot_config.initial_portfolio * bot_config.strategy.daily_loss_limit

    @classmethod
    def get_model_weight(cls, model_name: str) -> float:
        return bot_config.model_weights.get(model_name, 0.0)

    @classmethod
    def get_normalized_weights(cls) -> dict:
        return bot_config.model_weights

    @classmethod
    def get_max_exposure_amount(cls, portfolio_value: float) -> float:
        return portfolio_value * bot_config.max_exposure_pct

    @classmethod
    def get_daily_loss_limit(cls, portfolio_value: float) -> float:
        return portfolio_value * bot_config.strategy.daily_loss_limit


# ── Singleton instances (bot_config FIRST, then Config proxy) ──────────────
bot_config = BotConfig()
Config = _ConfigProxy()
config = Config  # alias used by older modules


def apply_persisted_strategy_params() -> dict:
    """Overlay any persisted strategy params from data/strategy_params.json
    onto the in-memory bot_config (single source of truth).

    Returns the params dict that was applied (empty dict if no file found).
    """
    try:
        from utils.weights_store import load_strategy_params
    except Exception:
        return {}

    persisted = load_strategy_params()
    if not persisted:
        return {}

    applied = {}
    s = bot_config.strategy

    if "min_edge" in persisted:
        try:
            s.min_edge = float(persisted["min_edge"])
            applied["min_edge"] = s.min_edge
        except (TypeError, ValueError):
            pass
    if "kelly_fraction" in persisted:
        try:
            s.kelly_fraction = float(persisted["kelly_fraction"])
            applied["kelly_fraction"] = s.kelly_fraction
        except (TypeError, ValueError):
            pass
    # NOTE: max_bet_pct is intentionally NOT loaded from strategy_params.json.
    # It MUST come ONLY from .env so that calculator.py, bet_placer.py, and
    # utils/kelly.py all use the same cap via max_bet_cap().
    if "min_entry_price" in persisted:
        try:
            s.min_entry_price = float(persisted["min_entry_price"])
            applied["min_entry_price"] = s.min_entry_price
        except (TypeError, ValueError):
            pass
    if "inefficiency_min" in persisted:
        try:
            s.inefficiency_min = float(persisted["inefficiency_min"])
            applied["inefficiency_min"] = s.inefficiency_min
        except (TypeError, ValueError):
            pass

    return applied


def fetch_and_apply_fee_rate() -> float:
    """Fetch fee rate from Polymarket API for weather category.

    Polymarket uses category-based fee rates:
    - Weather: 5% (default)
    - Crypto: 7%
    - Sports: 6%

    This function fetches the current rate from the CLOB API endpoint.
    If the API call fails, returns the default (0.05).
    """
    import requests

    try:
        # Polymarket CLOB API endpoint for fee rates
        url = f"{bot_config.polymarket_clob_api}/fee"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # Look for weather category fee rate
            if "fee_rate" in data:
                fee_rate = float(data["fee_rate"])
                bot_config.strategy.current_fee_rate = fee_rate
                return fee_rate
            # Try nested structure
            if "categories" in data and "weather" in data["categories"]:
                fee_rate = float(data["categories"]["weather"])
                bot_config.strategy.current_fee_rate = fee_rate
                return fee_rate
    except Exception as e:
        import logging
        logging.getLogger("CONFIG").warning("Could not fetch fee rate from API: %s", e)

    # Fallback to default
    return bot_config.strategy.current_fee_rate


# Apply persisted Karpathy-search winners at import time.
try:
    _applied_params = apply_persisted_strategy_params()
    if _applied_params:
        import logging

        logging.getLogger("CONFIG").info(
            "Applied Karpathy-search strategy params from disk: %s",
            ", ".join(f"{k}={v}" for k, v in _applied_params.items()),
        )
except Exception as _e:
    import logging

    logging.getLogger("CONFIG").warning("Could not apply persisted strategy params: %s", _e)

# Fetch dynamic fee rate from Polymarket API at import time
try:
    _fetched_fee = fetch_and_apply_fee_rate()
    import logging
    logging.getLogger("CONFIG").info("Polymarket fee rate: %.2f%%", _fetched_fee * 100)
except Exception as _e:
    import logging
    logging.getLogger("CONFIG").warning("Could not fetch fee rate: %s", _e)
