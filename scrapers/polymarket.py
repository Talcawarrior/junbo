"""Polymarket scraper module fetching and filtering weather events."""

import json
import logging
import re
from datetime import UTC, datetime

import requests

from config.settings import bot_config, config
from database.db import get_session
from database.models import WeatherMarket
from engine.market_parser import MarketParser
from scrapers.async_client import AsyncHttpClient
from utils.errors import ScraperError
from utils.retry import retry

logger = logging.getLogger("SCRAPER_POLYMARKET")


class PolymarketScraper:
    """Scrapes weather prediction markets from Polymarket Gamma API."""

    def __init__(self):
        self.gamma_url = bot_config.polymarket.gamma_url
        self.keywords = bot_config.polymarket.weather_keywords
        self._async_client = None

    async def init_session(self):
        """Mock init session for test compatibility."""
        pass

    async def close_session(self):
        """Close the AsyncHttpClient aiohttp session (if any)."""
        client = getattr(self, "_async_client", None)
        if client is not None:
            await client.aclose()

    @retry(max_attempts=3, delay=5, exceptions=(requests.RequestException,))
    def _fetch_raw_markets(self) -> list[dict]:
        """Polymarket'ten ham veri çek — public-search + today+2 gün + parallel.

        Tier 3 #12: parallel path now goes through AsyncHttpClient which
        uses aiohttp + bounded concurrency (8) + 250 ms per-host throttle
        and an in-process cache. The sync ThreadPoolExecutor path is
        kept as the no-aiohttp fallback (the AsyncHttpClient handles
        that automatically via ``_HAS_AIOHTTP``).
        """
        from datetime import timedelta
        from urllib.parse import urlparse

        today = datetime.now(UTC).replace(tzinfo=None)
        # Generate date strings in multiple formats to match Polymarket titles
        # which use "June 7" (no zero-pad), "June 07" (zero-pad), or "Jun 7".
        import calendar

        date_strs = []
        for i in range(3):
            d = today + timedelta(days=i)
            month_name = calendar.month_name[d.month]  # "June"
            month_abbr = calendar.month_abbr[d.month]  # "Jun"
            day_no_pad = str(d.day)  # "7"
            day_zero_pad = f"{d.day:02d}"  # "07"
            date_strs.extend(
                [
                    f"{month_name} {day_no_pad}",  # "June 7"
                    f"{month_name} {day_zero_pad}",  # "June 07"
                    f"{month_abbr} {day_no_pad}",  # "Jun 7"
                    f"{month_abbr} {day_zero_pad}",  # "Jun 07"
                ]
            )

        queries = [
            "highest temperature",
            "lowest temperature",
            "temperature",
            "weather temperature",
        ]
        # Also add 5 city-specific queries to broaden coverage beyond
        # the public-search top results.
        queries += [
            # Top US markets (highest volume on Polymarket)
            "dallas temperature",
            "miami temperature",
            "new york temperature",
            "chicago temperature",
            "houston temperature",
            "los angeles temperature",
            "phoenix temperature",
            # International (frequent on Polymarket)
            "london temperature",
            "paris temperature",
            "tokyo temperature",
            "seoul temperature",
            "istanbul temperature",
        ]

        gamma_host = urlparse(self.gamma_url).netloc
        # Build the batched (url, params, host) tuples once. AsyncHttpClient
        # takes care of bounded concurrency, per-host throttle and cache.
        items = [
            (
                f"{self.gamma_url}/public-search",
                {"q": q, "limit_per_type": 50},
                gamma_host,
            )
            for q in queries
        ]
        if not hasattr(self, "_async_client") or self._async_client is None:
            self._async_client = AsyncHttpClient()
        results = self._async_client.fetch_many(items)
        # Each entry is the parsed JSON or None on failure; events live
        # under the "events" key. Skip failures.
        per_query_events: list[list[dict]] = []
        for r in results:
            if not r:
                per_query_events.append([])
                continue
            per_query_events.append(r.get("events", []) or [])

        all_events: list[dict] = []
        seen_slugs: set[str] = set()
        for events in per_query_events:
            for e in events:
                slug = e.get("slug", "")
                title = e.get("title", "")
                if slug in seen_slugs:
                    continue
                # Keep only today + next 2 days
                if not any(d in title for d in date_strs):
                    continue
                seen_slugs.add(slug)
                # Flatten event's markets so the rest of the pipeline
                # (which expects raw market dicts) keeps working.
                for m in e.get("markets", []):
                    m.setdefault("title", title)
                    m.setdefault("description", title)
                    m.setdefault("event_slug", slug)
                    all_events.append(m)

        logger.info(
            f"Toplam {len(all_events)} market çekildi ({len(seen_slugs)} event, {len(queries)} sorgu)"
        )
        return all_events

    async def fetch_polymarket_events(self, limit: int = 100) -> list[dict]:
        """Fetch daily-temperature events for compatibility with test suite."""
        raw_markets = self._fetch_raw_markets()
        formatted = []
        for raw in raw_markets[:limit]:
            formatted.append(self._parse_market(raw))
        return formatted

    def _is_weather_market(self, market: dict) -> bool:
        """Weather market check: BOTH a known city AND a strong weather term required.

        Only temperature markets are accepted. Precipitation, wind, storm,
        and humidity markets are explicitly rejected.
        """
        question = (
            market.get("question", "")
            + " "
            + market.get("description", "")
            + " "
            + market.get("title", "")
        ).lower()
        # 1) Must mention a known city (any key from CITY_ICAO_MAP)
        city_match = any(
            city_key in question for city_key in config.CITY_ICAO_MAP.keys()
        )
        if not city_match:
            return False
        # 2) Must contain a strong weather term (reject sports/politics that
        #    happen to share a city name like "Boston Bruins" or "Dallas Cowboys")
        strong_terms = (
            "temperature",
            "highest",
            "lowest",
            "heat",
            "cold",
            "°F",
            "°C",
            "celsius",
            "fahrenheit",
            "weather",
        )
        if not any(term in question for term in strong_terms):
            return False
        # 3) Explicitly reject non-temperature weather markets (rain, snow, storm, etc.)
        reject_terms = (
            "rain",
            "snow",
            "storm",
            "hurricane",
            "tornado",
            "precipitation",
            "humidity",
            "wind",
            "snowfall",
            "rainfall",
        )
        if any(term in question for term in reject_terms):
            return False
        return True

    def _parse_market(self, raw: dict) -> dict:
        """Ham marketi yapılandırılmış veriye çevir."""
        # 1) YES/NO price — handle both /markets (tokens[]) and
        #    /public-search (lastTradePrice / bestBid / bestAsk) formats.
        yes_price = None
        no_price = None
        for token in raw.get("tokens", []) or []:
            outcome = (token.get("outcome", "") or "").upper()
            try:
                p = float(token.get("price", 0) or 0)
            except (TypeError, ValueError):
                p = None
            if outcome == "YES" and p is not None:
                yes_price = p
            elif outcome == "NO" and p is not None:
                no_price = p
        # Fallback: public-search fields
        if yes_price is None:
            for key in ("lastTradePrice", "bestBid", "yes_price", "yesPrice"):
                v = raw.get(key)
                if v is not None:
                    try:
                        yes_price = float(v)
                        break
                    except (TypeError, ValueError):
                        pass
        if no_price is None:
            for key in ("noPrice", "no_price", "bestAsk"):
                v = raw.get(key)
                if v is not None:
                    try:
                        no_price = float(v)
                        break
                    except (TypeError, ValueError):
                        pass
        if no_price is None and yes_price is not None:
            no_price = max(0.0, min(1.0, 1.0 - yes_price))
        if yes_price is None:
            yes_price = 0.5
        if no_price is None:
            no_price = 0.5

        # Extract city name dynamically from ICAO map keys
        city_name = "Unknown"
        title = raw.get("title", "") or raw.get("question", "")
        question = (
            raw.get("question", "")
            or raw.get("description", "")
            or raw.get("title", "")
        )
        title_lower = (title or "").lower()
        question_lower = (question or "").lower()
        for k in config.CITY_ICAO_MAP.keys():
            if k in title_lower or k in question_lower:
                city_name = k.title()
                break

        if city_name == "Unknown":
            event_title = title or ""
            city_name = (
                event_title.split(" - ")[0].strip()
                if event_title and " - " in event_title
                else (event_title.split()[0] if event_title else "Unknown")
            )

        # Parse structured market metadata
        target_date = self._extract_date(title)
        parser = MarketParser()
        threshold_result = parser._extract_threshold(question)
        threshold, threshold_unit, threshold_low, threshold_high = (
            threshold_result if threshold_result else (0.0, "celsius", None, None)
        )
        metric = (
            "temperature_max"
            if "highest" in question_lower or "above" in question_lower
            else "temperature_min"
        )
        city_code = self._extract_city(question)
        market_type = self._determine_market_type(question)
        coords = self.get_city_coords(city_code) if city_code else None

        # Ensure correct numeric market ID matching the betting and settlement engines
        market_id_val = str(raw.get("id"))

        return {
            "id": market_id_val,
            "condition_id": raw.get("condition_id"),
            "question": question,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": float(raw.get("volume", 0) or 0),
            "liquidity": float(raw.get("liquidity", 0) or 0),
            "end_date": raw.get("end_date_iso") or raw.get("endDate"),
            "raw_data": json.dumps(raw),
            "city_name": city_name,
            "city": city_name,
            "target_date": target_date,
            "threshold": threshold,
            "threshold_unit": threshold_unit,
            "threshold_low": threshold_low,
            "threshold_high": threshold_high,
            "metric": metric,
            "city_code": city_code,
            "market_type": market_type,
            "latitude": coords[0] if coords else 0.0,
            "longitude": coords[1] if coords else 0.0,
        }

    def fetch_and_save(self) -> int:
        """Ana fonksiyon: Çek -> Filtrele -> Kaydet."""
        try:
            raw_markets = self._fetch_raw_markets()
        except Exception as e:
            raise ScraperError(f"Polymarket API hatası: {e}")

        weather_markets = [m for m in raw_markets if self._is_weather_market(m)]
        logger.info(f"{len(weather_markets)} hava durumu marketi bulundu")

        saved = 0
        with get_session() as session:
            for raw in weather_markets:
                try:
                    parsed = self._parse_market(raw)

                    # Markets without ICAO coordinates → no_coords status
                    has_coords = parsed["latitude"] != 0.0 or parsed["longitude"] != 0.0
                    if not has_coords and parsed["city_code"]:
                        logger.warning(
                            "No coordinates for city=%s (ICAO=%s) market=%s question=%r — status=no_coords",
                            parsed.get("city_name", "?"),
                            parsed["city_code"],
                            parsed["id"],
                            (parsed.get("question") or "")[:80],
                        )

                    # Upsert
                    existing = (
                        session.query(WeatherMarket).filter_by(id=parsed["id"]).first()
                    )

                    # Skip markets with missing target_date or zero threshold
                    if parsed["target_date"] is None:
                        logger.warning(
                            f"Skipping market {parsed['id']}: no target_date parsed"
                        )
                        continue
                    threshold_c = parsed["threshold"]
                    if threshold_c == 0.0:
                        logger.warning(
                            f"Skipping market {parsed['id']}: threshold is 0.0"
                        )
                        continue
                    # Sanity guard: Celsius değer -40..55 aralığında değilse atla
                    if threshold_c < -40 or threshold_c > 55:
                        logger.warning(
                            "Skipping market %s: threshold %.1f°C outside sane range [-40, 55] — question=%r",
                            parsed["id"],
                            threshold_c,
                            (parsed.get("question") or "")[:80],
                        )
                        continue

                    status = "no_coords" if not has_coords else "open"

                    if existing:
                        existing.yes_price = parsed["yes_price"]
                        existing.no_price = parsed["no_price"]
                        existing.volume = parsed["volume"]
                        existing.liquidity = parsed["liquidity"]
                        existing.city = parsed["city"]
                        existing.last_updated = datetime.now(UTC).replace(tzinfo=None)
                        existing.raw_data = parsed["raw_data"]
                        existing.target_date = parsed["target_date"]
                        existing.threshold = parsed["threshold"]
                        existing.metric = parsed["metric"]
                        existing.city_code = parsed["city_code"]
                        existing.latitude = parsed["latitude"]
                        existing.longitude = parsed["longitude"]
                        existing.status = status
                        existing.threshold_low = parsed.get("threshold_low")
                        existing.threshold_high = parsed.get("threshold_high")
                    else:
                        market = WeatherMarket(
                            id=parsed["id"],
                            question=parsed["question"],
                            yes_price=parsed["yes_price"],
                            no_price=parsed["no_price"],
                            volume=parsed["volume"],
                            liquidity=parsed["liquidity"],
                            city=parsed["city"],
                            first_seen=datetime.now(UTC).replace(tzinfo=None),
                            last_updated=datetime.now(UTC).replace(tzinfo=None),
                            raw_data=parsed["raw_data"],
                            status=status,
                            target_date=parsed["target_date"],
                            threshold=parsed["threshold"],
                            threshold_low=parsed.get("threshold_low"),
                            threshold_high=parsed.get("threshold_high"),
                            metric=parsed["metric"],
                            city_code=parsed["city_code"],
                            market_type=parsed["market_type"],
                            latitude=parsed["latitude"],
                            longitude=parsed["longitude"],
                        )
                        session.add(market)
                    saved += 1

                except Exception as e:
                    logger.error(f"Market parse hatası {raw.get('id')}: {e}")
                    continue

            logger.info(f"{saved} market kaydedildi/güncellendi")
        return saved

    @staticmethod
    def get_city_coords(city_code: str) -> tuple | None:
        """ICAO kodundan koordinat bul — merkezi Config.ICAO_COORDS."""
        return config.ICAO_COORDS.get(city_code)

    def _extract_date(self, title: str) -> datetime | None:
        """Parse a date from a market title string.

        Tries three patterns in order:
          1. "June 9 2026" or "June 9th, 2026"
          2. "2026-06-09" (ISO)
          3. "June 9"       (yearless — uses current year)

        Returns a datetime at 23:59:59 on the parsed day, or None.
        """
        if not title:
            return None
        # Pattern 1: "June 9 2026" or "June 9th, 2026" or "Jun 9 2026"
        match = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})", title)
        if match:
            month_str, day, year = (
                match.group(1),
                int(match.group(2)),
                int(match.group(3)),
            )
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    dt = datetime.strptime(f"{month_str} {day} {year}", fmt)
                    return dt.replace(hour=23, minute=59, second=59)
                except ValueError:
                    continue
        # Pattern 2: ISO "2026-06-09"
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", title)
        if match:
            year, month, day = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
            return datetime(year, month, day, 23, 59, 59)
        # Pattern 3: "June 9" (yearless) — only valid month names to avoid
        # false matches like "above 90" or "will 100"
        _MONTH_NAMES = (  # noqa: N806
            "January|February|March|April|May|June|July|"
            "August|September|October|November|December|"
            "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
        )
        match = re.search(rf"(?:{_MONTH_NAMES})\s+(\d{{1,2}})", title, re.IGNORECASE)
        if match:
            month_str, day = match.group(0).split()[0], int(match.group(1))
            today = datetime.now()
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    dt = datetime.strptime(f"{month_str} {day} {today.year}", fmt)
                    return dt.replace(hour=23, minute=59, second=59)
                except ValueError:
                    continue
        return None

    def _extract_city(self, text: str) -> str:
        if not text:
            return ""
        text_lower = text.lower()
        for city_name, icao_code in config.CITY_ICAO_MAP.items():
            if city_name in text_lower:
                return icao_code
        return ""

    def _extract_strike(self, question: str) -> float:
        if not question:
            return 0.0
        patterns = [
            r"(\d+)\s*\°\s*C",
            r"(\d+)\s*\°\s*F",
            r"(\d+)\s*degrees?\s*[CF]?",
            r"above\s+(\d+)",
            r"below\s+(\d+)",
            r"be\s+(\d+)\s*\°?",
        ]
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                try:
                    strike = float(match.group(1))
                    if "F" in question.upper() or "FAHRENHEIT" in question.upper():
                        strike = (strike - 32) * 5 / 9
                    return round(strike, 1)
                except ValueError:
                    continue
        return 0.0

    def _determine_market_type(self, question: str) -> str:
        question_lower = question.lower()
        if (
            "above" in question_lower
            or "higher" in question_lower
            or "over" in question_lower
        ):
            return "HIGH"
        if (
            "below" in question_lower
            or "lower" in question_lower
            or "under" in question_lower
        ):
            return "LOW"
        if "or below" in question_lower or "or higher" in question_lower:
            if "or below" in question_lower:
                return "LOW"
            if "or higher" in question_lower:
                return "HIGH"
        return "RANGE"
