"""Piyasa sorusunu çözümleyen parser modülü (Regex & Kural tabanlı)."""

import logging
import re
from datetime import datetime

from config.settings import Config, config
from database.db import get_session
from database.models import WeatherMarket

logger = logging.getLogger("ENGINE_MARKET_PARSER")


class MarketParser:
    """Parses text questions to extract structural fields."""

    # Synced with config.settings.Config.CITY_ICAO_MAP (54+ cities).
    # Polymarket's /public-search currently exposes ~15 unique cities in
    # any given 24-hour window, but the parser must recognise all the
    # cities in ICAO_MAP so that a market question like "the highest
    # temperature in Toronto be 22°C on June 7" still extracts a city
    # and a metric we can score. Adding more cities here does NOT
    # generate markets — Polymarket still has to list them — but it
    # does prevent a recognised Polymarket market from being dropped
    # because the parser can't normalise the city name.
    CITY_ALIASES = {
        # North America (USA)
        "nyc": "new york",
        "new york city": "new york",
        "la": "los angeles",
        "sf": "san francisco",
        "dc": "washington",
        "phx": "phoenix",
        "dallas": "dallas",
        "miami": "miami",
        "chicago": "chicago",
        "new york": "new york",
        "newyork": "new york",
        "los angeles": "los angeles",
        "las vegas": "las vegas",
        "phoenix": "phoenix",
        "houston": "houston",
        "atlanta": "atlanta",
        "boston": "boston",
        "seattle": "seattle",
        "denver": "denver",
        "washington": "washington",
        "san francisco": "san francisco",
        "orlando": "orlando",
        "tampa": "tampa",
        "minneapolis": "minneapolis",
        "detroit": "detroit",
        "philadelphia": "philadelphia",
        "portland": "portland",
        # North America (CA / MX)
        "toronto": "toronto",
        "vancouver": "vancouver",
        "montreal": "montreal",
        "mexico city": "mexico city",
        "guadalajara": "guadalajara",
        # South America
        "sao paulo": "sao paulo",
        "rio de janeiro": "rio de janeiro",
        "buenos aires": "buenos aires",
        "santiago": "santiago",
        "lima": "lima",
        "bogota": "bogota",
        # Europe
        "london": "london",
        "paris": "paris",
        "berlin": "berlin",
        "moscow": "moscow",
        "frankfurt": "frankfurt",
        "amsterdam": "amsterdam",
        "madrid": "madrid",
        "rome": "rome",
        "barcelona": "barcelona",
        "munich": "munich",
        "zurich": "zurich",
        "vienna": "vienna",
        "stockholm": "stockholm",
        "oslo": "oslo",
        "copenhagen": "copenhagen",
        "helsinki": "helsinki",
        "warsaw": "warsaw",
        "athens": "athens",
        "lisbon": "lisbon",
        # Middle East
        "dubai": "dubai",
        "tel aviv": "tel aviv",
        "doha": "doha",
        "riyadh": "riyadh",
        # Asia
        "tokyo": "tokyo",
        "osaka": "osaka",
        "shanghai": "shanghai",
        "jinan": "jinan",
        "zhengzhou": "zhengzhou",
        "beijing": "beijing",
        "seoul": "seoul",
        "hong kong": "hong kong",
        "taipei": "taipei",
        "singapore": "singapore",
        "bangkok": "bangkok",
        "jakarta": "jakarta",
        "manila": "manila",
        "kuala lumpur": "kuala lumpur",
        "mumbai": "mumbai",
        "delhi": "delhi",
        # Oceania
        "sydney": "sydney",
        "melbourne": "melbourne",
        "auckland": "auckland",
        "wellington": "wellington",
        # Africa
        "cairo": "cairo",
        "cape town": "cape town",
        "johannesburg": "johannesburg",
        # Turkey
        "istanbul": "istanbul",
        "ankara": "ankara",
        "izmir": "izmir",
        "antalya": "antalya",
    }

    def _extract_city(self, question: str) -> str | None:
        q = question.lower()
        all_cities = list(self.CITY_ALIASES.values()) + list(self.CITY_ALIASES.keys())
        for city in sorted(all_cities, key=len, reverse=True):
            if city in q:
                return self.CITY_ALIASES.get(city, city)
        return None

    @staticmethod
    def _resolve_unit(question: str, city: str | None) -> str:
        """Birim tespiti: 1) açık birim regex'i, 2) şehre göre (K→ICAO=US→°F),
        3) varsayılan Celsius.

        US şehirleri Polymarket'ta °F, uluslararası °C konvansiyonundadır.
        """
        # 1) Açık birim varsa (°F / °C / Fahrenheit / Celsius, sayıya bitişik)
        explicit = re.search(
            r"(?:\d\s*°?\s*[Ff](?:ahrenheit)?|°[Ff]|"
            r"\d\s*°?\s*[Cc](?:elsius)?|°[Cc])",
            question,
        )
        if explicit:
            unit_text = explicit.group(0).upper()
            if "F" in unit_text:
                return "fahrenheit"
            return "celsius"

        # 2) Birim yoksa, şehre göre karar ver
        if city:
            for alias, icao in Config.CITY_ICAO_MAP.items():
                if alias.lower() in city.lower():
                    # US/territory ICAO'ları K ile başlar
                    return "fahrenheit" if icao.upper().startswith("K") else "celsius"

        # 3) Varsayılan
        return "celsius"

    def _extract_threshold(
        self, question: str
    ) -> tuple[float, str, float | None, float | None] | None:
        """Sıcaklık eşiğini ve varsa aralığı bul.

        Returns
        -------
        tuple (value_celsius, unit, low_c, high_c) or None
            value_celsius: Celsius'a dönüşmüş ana eşik (tek değer veya aralığın ortası).
            unit: her zaman "celsius".
            low_c / high_c: "between A-B°F/°C" kalıbı için alt/üst sınır (°C).
        """
        city = self._extract_city(question)

        # ── 1) Aralık kalıbı (range): "between A-B°F/°C"
        range_match = re.search(
            r"between\s+(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?\s*([FfCc]?)",
            question,
            re.IGNORECASE,
        )
        if range_match:
            try:
                low_val = float(range_match.group(1))
                high_val = float(range_match.group(2))
                unit_char = range_match.group(3).lower() if range_match.group(3) else ""
                # Birim belirtilmemişse şehre göre karar ver
                is_f = unit_char == "f" or (
                    not unit_char and self._resolve_unit(question, city) == "fahrenheit"
                )
                if is_f:
                    low_c = round((low_val - 32) * 5 / 9, 1)
                    high_c = round((high_val - 32) * 5 / 9, 1)
                else:
                    low_c = round(low_val, 1)
                    high_c = round(high_val, 1)
                mid = round((low_c + high_c) / 2, 1)
                return (mid, "celsius", low_c, high_c)
            except ValueError:
                pass

        # ── 2) Açık birimli tek değer
        patterns = [
            (r"(\d+\.?\d*)\s*°?\s*[Ff](?:ahrenheit)?", "fahrenheit"),
            (r"(\d+\.?\d*)\s*°?\s*[Cc](?:elsius)?", "celsius"),
            (r"(\d+\.?\d*)\s*degrees?\s*[Ff]", "fahrenheit"),
            (r"(\d+\.?\d*)\s*degrees?\s*[Cc]", "celsius"),
        ]
        for pattern, expected_unit in patterns:
            match = re.search(pattern, question)
            if match:
                try:
                    value = float(match.group(1))
                    if expected_unit == "fahrenheit":
                        value_c = round((value - 32) * 5 / 9, 1)
                    else:
                        value_c = round(value, 1)
                    return (value_c, "celsius", None, None)
                except ValueError:
                    continue

        # ── 3) Birimsiz sayı kalıpları (exceed, above, below, be, over, under)
        unitless_patterns = [
            r"exceed\s+(\d+\.?\d*)",
            r"above\s+(\d+\.?\d*)",
            r"below\s+(\d+\.?\d*)",
            r"over\s+(\d+\.?\d*)",
            r"under\s+(\d+\.?\d*)",
            r"be\s+(\d+\.?\d*)",
        ]
        detected_unit = self._resolve_unit(question, city)

        for pattern in unitless_patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                try:
                    value = float(match.group(1))
                    if detected_unit == "fahrenheit":
                        value_c = round((value - 32) * 5 / 9, 1)
                    else:
                        value_c = round(value, 1)
                    return (value_c, "celsius", None, None)
                except ValueError:
                    continue

        return None

    def _extract_date(self, question: str) -> datetime | None:
        """Tarih bul."""
        patterns = [
            r"(\w+ \d{1,2},?\s*\d{4})",  # July 4, 2025
            r"(\d{4}-\d{2}-\d{2})",  # 2025-07-04
            r"(\d{1,2}/\d{1,2}/\d{4})",  # 7/4/2025
            # Use word-boundary on "on" so substrings like "London" don't
            # accidentally match (the "on" inside "London" is preceded by
            # a word character, so \b prevents a match there).
            r"\bon\s+(\w+\s+\d{1,2})\b",  # on May 20
        ]

        for pattern in patterns:
            match = re.search(pattern, question)
            if match:
                date_str = match.group(1)
                # Handle simplified date format e.g. "May 20" by assuming
                # current year. The "on" prefix pattern uses \s+ (regex
                # whitespace), not a literal space, so detect it by checking
                # the pattern start instead of substring.
                if (
                    pattern.startswith(r"\bon")
                    or pattern.startswith("on")
                    or "on " in pattern
                ):
                    date_str = f"{date_str} {datetime.now().year}"
                for fmt in [
                    "%B %d, %Y",
                    "%B %d %Y",
                    "%Y-%m-%d",
                    "%m/%d/%Y",
                ]:
                    try:
                        d = datetime.strptime(date_str.strip(), fmt)
                        # Set to end-of-day (23:59:59) so that "today" markets
                        # are not filtered out by a strict >= comparison
                        # against the current time-of-day.
                        return d.replace(hour=23, minute=59, second=59)
                    except ValueError:
                        continue
        return None

    def _extract_metric(self, question: str) -> str:
        """Ne ölçülüyor?"""
        q = question.lower()

        if any(
            w in q
            for w in [
                "high temp",
                "max temp",
                "exceed",
                "above",
                "over",
                "hot",
                "highest",
            ]
        ):
            return "temperature_max"
        if any(
            w in q for w in ["low temp", "min temp", "below", "under", "cold", "lowest"]
        ):
            return "temperature_min"
        if any(w in q for w in ["rain", "precipitation", "rainfall"]):
            return "precipitation_mm"
        if any(w in q for w in ["snow", "snowfall"]):
            return "snow_cm"
        if any(w in q for w in ["wind", "gust"]):
            return "wind_speed_kmh"

        return "temperature_max"  # Default

    def parse_and_update(self, market_id: str) -> bool:
        """Bir marketi parse et ve DB'yi güncelle."""
        with get_session() as session:
            market = session.query(WeatherMarket).filter_by(id=market_id).first()
            if not market:
                return False

            question = market.question

            city = self._extract_city(question)
            threshold_result = self._extract_threshold(question)
            target_date = self._extract_date(question)
            metric = self._extract_metric(question)

            if city:
                market.city = city.title()
                # Map city code (for ICAO compatibility)
                from scrapers.polymarket import PolymarketScraper

                for k, v in config.CITY_ICAO_MAP.items():
                    if k in city.lower():
                        market.city_code = v
                        coords = PolymarketScraper().get_city_coords(v)
                        if coords:
                            market.latitude, market.longitude = coords
                        break

            if threshold_result:
                value_c, unit, low_c, high_c = threshold_result
                market.threshold = value_c
                market.threshold_unit = unit
                market.threshold_low = low_c
                market.threshold_high = high_c
            if target_date:
                market.target_date = target_date
            market.metric = metric

            parsed = bool(city and threshold_result and target_date)

            if not parsed:
                logger.warning(
                    f"Market {market_id} tam parse edilemedi: "
                    f"city={city}, threshold={threshold_result}, date={target_date}"
                )

            return parsed

    def parse_all_unparsed(self) -> int:
        """Parse edilmemiş tüm marketleri parse et."""
        count = 0
        with get_session() as session:
            unparsed = (
                session.query(WeatherMarket)
                .filter(
                    WeatherMarket.city.is_(None) | WeatherMarket.target_date.is_(None)
                )
                .all()
            )
            market_ids = [m.id for m in unparsed]

        for mid in market_ids:
            try:
                if self.parse_and_update(mid):
                    count += 1
            except Exception as e:
                logger.error(f"Parse hatası {mid}: {e}")
                continue

        return count
