"""Junbo - Polymarket Weather Prediction Bot - Configuration Dataclasses & Legacy Config."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Compute repo root (parent of config/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env from repo root
load_dotenv(os.path.join(BASE_DIR, ".env"))


# Hard ceiling for max bet — no single bet may ever exceed this fraction
# of the portfolio, regardless of strategy config. Guards against runaway
# sizing (e.g. a misconfigured value that would stake the whole book).
MAX_BET_PCT_CEILING = 0.33  # max 33 % of portfolio on a single bet


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

    # Proxy for geo-blocked regions (Turkey, etc.)
    # Format: "socks5h://user:pass@host:port" or "http://host:port"
    proxy_url: str = os.getenv("POLY_PROXY", "")

    # Fee rates by category (dynamic, fetched from API)
    fee_categories: dict | None = None  # {"weather": 0.05, ...}

    def get_proxies(self) -> dict | None:
        """Return requests-compatible proxy dict, or None if no proxy configured."""
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

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
                "weather": 0.05,  # Weather markets: 5% fee
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
    min_edge: float = 0.05  # 5% - minimum edge to accept a bet
    max_bet_amount: float = 1000.0  # Maximum $1000 per bet (flat)
    max_bet_pct: float = 1.0  # Safety ceiling (flat_bet_usd overrides Kelly sizing)
    min_bet_size: float = 1.0  # Minimum bet size in USD
    total_exposure_pct: float = 1.0  # Max total exposure as % of previous-day capital
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
    kelly_fraction: float = 0.25  # Half-Kelly (agresif ama dalgalanmaya dayanikli)
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
    # Weather category exponent (0.5 = flatter fee curve per Polymarket docs).
    # Other categories use 1.0 (standard quadratic).
    fee_exponent: float = 0.5

    # Bot scope: today + 1 + 2 + 3 days ahead (0..3 inclusive).
    # Forecast reliability degrades past 3 days, but 3-day coverage
    # gives the bot more opportunities to find edge.
    max_days_ahead: int = 3

    # ── Slippage model ────────────────────────────────────────────────
    # "flat"   — fixed slippage_pct from strategy_params.json
    # "tiered" — 3-tier by entry price (<0.05: 3%, 0.05-0.10: 1%, >0.10: 0.5%)
    # "orderbook" — live depth-based (future, falls back to tiered)
    slippage_model: str = "orderbook"
    slippage_pct: float = 0.005  # used when slippage_model="flat"
    gas_cost_usd: float = 0.10  # Polygon gas per round-trip

    # ── Flat bet override & Daily loss limit (synced from Config) ─────────
    flat_bet_usd: float = 2.0  # Fixed $2 per bet
    daily_loss_limit: float = 0.0  # Disabled: no daily loss circuit breaker

    # ── Tie betting: ayni en yuksek fiyata sahip marketlere ayni anda ac ─
    tie_bet_enabled: bool = False  # Kapali: gereksiz sermaye bolusu ve maliyet yaratir
    tie_loser_gap: float = 0.10  # ikiz betlerden biri %10+ one gecerse digerini kapat

    # ── Smart rotation: eski bet'i kapatip yenisini ac ─
    rotation_threshold: float = 0.15  # %15+ improvement gerekli rotation icin
    daily_rotation_limit: int = 5  # Gunluk max rotation sayisi (0 = limitsiz)

    # ── Max entry price ───────────────────────────────────────────────────
    # YES entries are accepted in [0.10, 0.95). The upper bound is strict.
    min_entry_price: float = 0.10
    max_entry_price: float = 0.95

    # ── Daily rotation limit: gunde max N rotasyon ───────────────────
    max_daily_rotations: int = 3  # gunde en fazla 3 rotasyon (maliyet kontrolu)

    # ── Betting windows (UTC saatleri, bahis sadece bu pencerelerde acilir) ─
    # Her pencere (baslangic_saati, bitis_saati) tuple olarak tanimlanir.
    # Pencereler disinda bahis acma kapalidir.
    betting_windows: list = None  # type: ignore[assignment]  # __post_init__'te初始化
    betting_window_enabled: bool = True  # bahis penceresi kontrolunu aktif et


@dataclass
class RiskConfig:
    """Active risk management: position-level stop-loss, take-profit, time decay, rebalance.

    DISABLED: all early exits set to extreme values so bets ONLY close at settlement (ST).
    """

    # Position-level limits
    stop_loss_pct: float = 0.20  # %20 kayipta pozisyonu kapat
    take_profit_pct: float = 999.0  # %999 karda kapat = asla tetiklenmez
    trailing_stop_pct: float = 999.0  # %999 trailing drop = asla tetiklenmez

    # Time-based exits
    time_decay_hours: int = 0  # 0 saat = time decay devre disi
    time_decay_threshold: float = -999.0  # %999 zararda kapat = asla tetiklenmez

    # Rebalancing (disabled via extreme ratio)
    min_rebalance_edge_ratio: float = 999.0
    rebalance_min_loss: float = -999.0

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
    # Additional US cities (11)
    "KMSP": (44.8848, -93.2223),
    "KPDX": (45.5887, -122.5975),
    "KSAN": (32.7338, -117.1900),
    "KTPA": (27.9755, -82.5332),
    "KSMF": (38.6954, -121.5908),
    "KPIT": (40.4915, -80.2329),
    "KSTL": (38.7487, -90.3700),
    "KBWI": (39.1774, -76.6684),
    "KMKE": (42.9472, -87.8966),
    "KMCI": (39.2976, -94.7139),
    "KSLC": (40.7884, -111.9778),
    "KAUS": (30.1945, -97.6700),  # Austin
    "WMKK": (2.7456, 101.7099),  # Kuala Lumpur
    "RPLL": (14.5086, 121.0194),  # Manila
    "LIMC": (45.6306, 8.7281),  # Milan
    "EPWA": (52.1657, 20.9671),  # Warsaw
    "RKPK": (35.0689, 128.9625),  # Busan
    "ZUUU": (30.5785, 103.9471),  # Chengdu
    "ZUCK": (29.7192, 106.6417),  # Chongqing
    "ZGGG": (23.3924, 113.2988),  # Guangzhou
    "EFHK": (60.3172, 24.9633),  # Helsinki
    "OEJN": (21.6796, 39.1565),  # Jeddah
    "OPKC": (24.9065, 67.1608),  # Karachi
    "RKSI": (37.4602, 126.4407),  # Seoul Incheon
    "ZGSZ": (22.6393, 113.8107),  # Shenzhen
    "NZWN": (-41.3272, 174.8053),  # Wellington
    "ZHHH": (30.7838, 114.2081),  # Wuhan
    "VILK": (26.7606, 80.8893),  # Lucknow
    "ZSQD": (36.0986, 120.3719),  # Qingdao
    "MPTO": (9.0716, -79.3829),  # Panama City
    # NYC alias
    "NYC": (40.7769, -73.8740),
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
    # Missing US cities from Polymarket
    "minneapolis": "KMSP",
    "portland": "KPDX",
    "san diego": "KSAN",
    "tampa": "KTPA",
    "sacramento": "KSMF",
    "pittsburgh": "KPIT",
    "st louis": "KSTL",
    "baltimore": "KBWI",
    "milwaukee": "KMKE",
    "kansas city": "KMCI",
    "salt lake city": "KSLC",
    # Missing international cities
    "osaka": "RJOO",
    "jakarta": "WIII",
    "mumbai": "VABB",
    "delhi": "VIDP",
    "sydney": "YSSY",
    "melbourne": "YMML",
    "auckland": "NZAA",
    "cairo": "HECA",
    "kuala lumpur": "WMKK",
    "manila": "RPLL",
    "milan": "LIMC",
    "warsaw": "EPWA",
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
    "shanghai": "ZSPD",
    "beijing": "ZBAA",
    "seoul": "RKSS",
    "hong kong": "VHHH",
    "taipei": "RCTP",
    "singapore": "WSSS",
    "bangkok": "VTBS",
    "cape town": "FACT",
    # Additional cities from Polymarket
    "austin": "KAUS",
    "busan": "RKPK",
    "chengdu": "ZUUU",
    "chongqing": "ZUCK",
    "guangzhou": "ZGGG",
    "helsinki": "EFHK",
    "jeddah": "OEJN",
    "karachi": "OPKC",
    "nyc": "KLGA",
    "seoul (incheon)": "RKSI",
    "shenzhen": "ZGSZ",
    "wellington": "NZWN",
    "wuhan": "ZHHH",
    "lucknow": "VILK",
    "qingdao": "ZSQD",
    "panama city": "MPTO",
}


@dataclass
class BotConfig:
    """Combined configurations — single source of truth for ALL config."""

    # ── Portfolio ──────────────────────────────────────────────────
    initial_portfolio: float = 1000.0
    max_exposure_pct: float = 1.0
    city_cap: int = 999  # no city limit - bet all cities
    weather_fee_rate: float = 0.05
    fee_exponent: float = 0.5  # Weather category: 0.5 (flatter curve)

    # ── Intervals ──────────────────────────────────────────────────
    scan_interval: int = 900  # 15 dakika (Open-Meteo rate limit icin)
    settlement_interval: int = 120
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

        # ── Betting windows initialization ──────────────────────────
        if self.strategy.betting_windows is None:
            self.strategy.betting_windows = [
                (0, 6),  # Pencere 1: 00:00-06:00 UTC (24h kuralina giren marketler aninda acilir)
                (12, 15),  # Pencere 2: 12:00-15:00 UTC (2-gun pazarlari oturdu)
                (19, 22),  # Pencere 3: 19:00-22:00 UTC (aksam runu + likidite)
            ]

        # ── Override from .env (single source: .env > dataclass defaults) ──
        self.initial_portfolio = float(os.getenv("INITIAL_PORTFOLIO", str(self.initial_portfolio)))
        self.max_exposure_pct = float(os.getenv("MAX_EXPOSURE_PCT", str(self.max_exposure_pct)))
        self.strategy.total_exposure_pct = self.max_exposure_pct
        self.city_cap = int(os.getenv("CITY_CAP", str(self.city_cap)))
        self.weather_fee_rate = float(os.getenv("WEATHER_FEE_RATE", str(self.weather_fee_rate)))
        self.scan_interval = int(os.getenv("SCAN_INTERVAL", str(self.scan_interval)))
        self.settlement_interval = int(os.getenv("SETTLEMENT_INTERVAL", str(self.settlement_interval)))
        self.midnight_scan_interval = int(os.getenv("MIDNIGHT_SCAN_INTERVAL", str(self.midnight_scan_interval)))
        self.midnight_scan_window = int(os.getenv("MIDNIGHT_SCAN_WINDOW", str(self.midnight_scan_window)))
        self.host = os.getenv("HOST", self.host)
        self.port = int(os.getenv("PORT", str(self.port)))
        self.dry_run = os.getenv("DRY_RUN", str(self.dry_run)).lower() == "true"
        self.log_level = os.getenv("LOG_LEVEL", self.log_level)
        self.db_echo = os.getenv("DB_ECHO", "false").lower() == "true"

        # Resolve paths
        self.db_path = _resolve_path(os.getenv("DB_PATH") or "", "data/bot.db")
        self.log_file = _resolve_path(os.getenv("LOG_FILE") or "", "logs/bot.log")

        # ── Constants (large dicts) ───────────────────────────────
        if self.model_weights is None:
            # ECMWF-first allocation: research shows ECMWF HRES outperforms
            # GFS at all lead times. GFS weight reduced from 0.30 to 0.15.
            self.model_weights = {
                "ecmwf_ifs025": 0.35,
                "gfs_seamless": 0.15,
                "gem_global": 0.12,
                "icon_global": 0.12,
                "jma_seamless": 0.10,
                "cma_grapes_global": 0.06,
                "ukmo_seamless": 0.05,
                "meteofrance_seamless": 0.05,
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
        s.flat_bet_usd = float(os.getenv("FLAT_BET_USD", str(s.flat_bet_usd)))
        s.min_entry_price = float(os.getenv("MIN_ENTRY_PRICE", str(s.min_entry_price)))
        s.max_entry_price = float(os.getenv("MAX_ENTRY_PRICE", str(s.max_entry_price)))


# ── Config backward-compatibility proxy ────────────────────────────────────
# All reads/writes go through bot_config (single source of truth).
# This eliminates the dual Config / bot_config drift problem.


class _ConfigProxy:
    """Backward-compatible proxy. Delegates all attribute access to bot_config.

    Usage: ``Config.MAX_BET_PCT`` reads ``bot_config.strategy.max_bet_pct``.
    """

    _MAP: dict[str, tuple[str, str]] = {
        # root-level BotConfig fields
        "INITIAL_PORTFOLIO": ("root", "initial_portfolio"),
        "MAX_EXPOSURE_PCT": ("root", "max_exposure_pct"),
        "CITY_CAP": ("root", "city_cap"),
        "WEATHER_FEE_RATE": ("root", "weather_fee_rate"),
        "FEE_EXPONENT": ("root", "fee_exponent"),
        "SCAN_INTERVAL": ("root", "scan_interval"),
        "SETTLEMENT_INTERVAL": ("root", "settlement_interval"),
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
        "FLAT_BET_USD": ("strategy", "flat_bet_usd"),
        "DAILY_LOSS_LIMIT": ("strategy", "daily_loss_limit"),
        "TOTAL_EXPOSURE_PCT": ("strategy", "total_exposure_pct"),
        "MAX_ENTRY_PRICE": ("strategy", "max_entry_price"),
        "MIN_ENTRY_PRICE": ("strategy", "min_entry_price"),
        "ROTATION_THRESHOLD": ("strategy", "rotation_threshold"),
        "DAILY_ROTATION_LIMIT": ("strategy", "daily_rotation_limit"),
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
    def get_normalized_weights(cls) -> dict:
        return bot_config.model_weights


# ── Singleton instances (bot_config FIRST, then Config proxy) ──────────────
bot_config = BotConfig()
Config = _ConfigProxy()
config = Config  # alias used by older modules

# Auto-set proxy env vars for ALL requests (covers ClobClient + direct calls)
if bot_config.polymarket.proxy_url:
    os.environ["HTTP_PROXY"] = bot_config.polymarket.proxy_url
    os.environ["HTTPS_PROXY"] = bot_config.polymarket.proxy_url
    os.environ["ALL_PROXY"] = bot_config.polymarket.proxy_url


def _load_strategy_params() -> dict:
    """Read data/strategy_params.json (adaptive sizing persistence)."""
    try:
        import json

        _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "strategy_params.json")
        with open(_p, encoding="utf-8") as _f:
            _raw = json.load(_f)
        return _raw if isinstance(_raw, dict) else {}
    except Exception:
        return {}


def apply_persisted_strategy_params() -> dict:
    """Overlay any persisted strategy params from data/strategy_params.json
    onto the in-memory bot_config (single source of truth).

    Returns the params dict that was applied (empty dict if no file found).
    """
    persisted = _load_strategy_params()
    if not persisted:
        return {}

    applied = {}
    s = bot_config.strategy

    if "min_edge" in persisted:
        try:
            s.min_edge = 0.05  # Hard floor — no override allowed
            applied["min_edge"] = s.min_edge
        except (TypeError, ValueError):
            pass
    if "kelly_fraction" in persisted:
        try:
            s.kelly_fraction = float(persisted["kelly_fraction"])
            applied["kelly_fraction"] = s.kelly_fraction
        except (TypeError, ValueError):
            pass

    return applied


try:
    _applied_params = apply_persisted_strategy_params()
    if _applied_params:
        import logging

        logging.getLogger("CONFIG").info(
            ", ".join(f"{k}={v}" for k, v in _applied_params.items()),
        )
except Exception as _e:
    import logging

    logging.getLogger("CONFIG").warning("Could not apply persisted strategy params: %s", _e)

# NOTE: Fee rate is fetched lazily (not at import time) to avoid blocking startup.
# Call fetch_and_apply_fee_rate() when needed, e.g., at bot startup.
# The default fee_rate_weather (0.05) is used until then.
