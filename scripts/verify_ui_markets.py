"""Polymarket UI doğrulama scripti.

Gamma API'den güncel sıcaklık marketlerini çekip, DB'deki açık
WeatherMarket'ler ile karşılaştırır. Hızlı: sadece 4 genel sorgu yapar.

Kullanım:
  python scripts/verify_ui_markets.py              # tüm açık tarihler
  python scripts/verify_ui_markets.py 2026-08-03  # spesifik tarih
"""

import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from database.db import get_session  # noqa: E402
from database.models import WeatherMarket  # noqa: E402

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"


def _normalize_city(city: str) -> str:
    """Şehir isimlerini normalleştir: parantezleri kaldır, küçük harfe çevir, mapping uygula."""
    import re as _re

    city = city.strip().lower()
    # Parantez içeriğini kaldır: "Seoul (Incheon)" -> "Seoul"
    city = _re.sub(r"\s*\([^)]*\)", "", city)
    # "New York City" -> "New York"
    city = city.replace(" city", "")
    # Tireleri boşlukla değiştir
    city = city.replace("-", " ")
    city = city.strip()

    # Polymarket şehir isim mapping'i
    _POLY_CITY_MAP = {
        "nyc": "new york",
        "mexico": "mexico city",
        "washington": "washington dc",
        "st louis": "st. louis",
    }
    return _POLY_CITY_MAP.get(city, city)


def _date_strings(target_date: datetime) -> list[str]:
    """Generate date string variants to match Polymarket titles."""
    import calendar

    out = []
    for i in range(-1, 2):
        d = target_date + timedelta(days=i)
        mn = calendar.month_name[d.month]
        ma = calendar.month_abbr[d.month]
        dn = str(d.day)
        dp = f"{d.day:02d}"
        out.extend([f"{mn} {dn}", f"{mn} {dp}", f"{ma} {dn}", f"{ma} {dp}"])
    return out


def _extract_city_from_title(title: str) -> Optional[str]:
    """Extract city from 'Highest temperature in <City> on <Date>'."""
    m = re.search(r"temperature\s+in\s+(.+?)\s+on\s+", title, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _extract_threshold_from_question(question: str) -> Optional[float]:
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*°[CF]", question)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:above|over|at\s+least|≥|>=)?\s*(\d+)\s*°[CF]", question)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:above|over|at\s+least|≥|>=)\s*(\d+)", question)
    if m:
        return float(m.group(1))
    return None


def _extract_metric(question: str) -> str:
    q = question.lower()
    if "highest" in q or "above" in q:
        return "temperature_max"
    if "lowest" in q or "below" in q:
        return "temperature_min"
    return "temperature_max"


def _fetch_gamma_keyset(target_dates: set[str] | None = None) -> dict[str, dict[str, list[dict]]]:
    """Gamma API events/keyset ile sıcaklık marketlerini çek.

    Polymarket UI'nın kullandığı aynı API. tag_slug=weather ile çeker,
    sadece temperature piyasalarını filtreler. Hedef tarihler verilirse
    sadece o tarihlerdeki event'leri çeker (performans için).

    Returns: {date_str: {city: [market_dict]}}
    """
    result: dict[str, dict[str, list[dict]]] = {}
    seen_ids: set[str] = set()
    cursor = None
    max_pages = 50  # 5000 event (temperature ~%10 = 500 market)

    for page in range(max_pages):
        try:
            params: dict = {
                "limit": 100,
                "tag_slug": "weather",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
            }
            if cursor:
                params["cursor"] = cursor

            resp = requests.get(
                f"{GAMMA_URL}/events/keyset",
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", [])
            cursor = data.get("next_cursor")

            if not events:
                break

            for event in events:
                title = event.get("title", "")
                title_lower = title.lower()

                # Sadece temperature piyasaları
                if not any(t in title_lower for t in ("temperature", "°c", "°f")):
                    continue

                # Tarih çıkarma
                td_match = re.search(
                    r"on\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d+)",
                    title,
                    re.IGNORECASE,
                )
                if not td_match:
                    continue

                month_num = datetime.strptime(td_match.group(1), "%B").month
                day = int(td_match.group(2))
                now = datetime.utcnow().replace(tzinfo=None)
                mkt_date = now.replace(month=month_num, day=day)
                mkt_date_str = mkt_date.strftime("%Y-%m-%d")

                # Hedef tarih filtresi
                if target_dates and mkt_date_str not in target_dates:
                    continue

                city = _extract_city_from_title(title)
                if not city:
                    continue

                for market in event.get("markets", []):
                    mid = str(market.get("id", ""))
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    question = market.get("question", "") or title
                    threshold = _extract_threshold_from_question(question)
                    if threshold is None:
                        continue
                    metric = _extract_metric(question)

                    city_key = _normalize_city(city)
                    result.setdefault(mkt_date_str, {}).setdefault(city_key, []).append(
                        {
                            "id": mid,
                            "threshold": threshold,
                            "metric": metric,
                            "question": question,
                        }
                    )

            if not cursor:
                break

        except Exception as e:
            logger.error("Gamma keyset API hatası (page=%d): %s", page, e)
            break

    return result


def _fetch_gamma_quick(target_date: datetime) -> dict[str, dict[str, list[dict]]]:
    """Hızlı Gamma API çekimi: sadece 4 genel sorgu.

    Returns: {date_str: {city: [market_dict]}}
    """
    date_strs = _date_strings(target_date)
    queries = ["highest temperature", "lowest temperature", "temperature", "weather temperature"]

    result: dict[str, dict[str, list[dict]]] = {}
    seen_ids: set[str] = set()

    for query in queries:
        try:
            resp = requests.get(
                f"{GAMMA_URL}/public-search",
                params={"q": query, "limit_per_type": 100},
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json().get("events", []) or []

            for event in events:
                title = event.get("title", "")
                if not any(d in title for d in date_strs):
                    continue
                title_lower = title.lower()
                if not any(t in title_lower for t in ("temperature", "°c", "°f")):
                    continue

                for market in event.get("markets", []):
                    mid = str(market.get("id", ""))
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    question = market.get("question", "") or title
                    city = _extract_city_from_title(title)
                    if not city:
                        continue
                    threshold = _extract_threshold_from_question(question)
                    if threshold is None:
                        continue
                    metric = _extract_metric(question)

                    # Tarih çıkarma
                    td_match = re.search(
                        r"on\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d+)",
                        title,
                        re.IGNORECASE,
                    )
                    if td_match:
                        month_name = td_match.group(1)
                        day = int(td_match.group(2))
                        month_num = datetime.strptime(month_name, "%B").month
                        mkt_date = target_date.replace(month=month_num, day=day)
                    else:
                        mkt_date = target_date

                    mkt_date_str = mkt_date.strftime("%Y-%m-%d")
                    city_key = _normalize_city(city)
                    result.setdefault(mkt_date_str, {}).setdefault(city_key, []).append(
                        {
                            "id": mid,
                            "threshold": threshold,
                            "metric": metric,
                            "question": question,
                        }
                    )

        except Exception as e:
            logger.error("Gamma API hatası (query=%s): %s", query, e)

    return result


def verify_all_open_dates() -> str:
    """Tüm açık tarihler için Gamma API vs DB karşılaştırması."""
    # DB'deki açık tarihleri bul + verileri session açıkken oku
    db_by_date: dict[str, dict[str, list]] = {}
    with get_session() as session:
        db_all = (
            session.query(WeatherMarket)
            .filter(
                WeatherMarket.target_date.isnot(None),
                WeatherMarket.status.in_(["open", "active"]),
            )
            .all()
        )
        for m in db_all:
            td = m.target_date
            city_name = m.city
            th = m.threshold
            met = m.metric
            if td is None:
                continue
            ds = td.strftime("%Y-%m-%d")
            city = _normalize_city(city_name or "")
            db_by_date.setdefault(ds, {}).setdefault(city, []).append(
                {
                    "threshold": th,
                    "metric": met,
                }
            )

    if not db_by_date:
        return "DB'de açık market yok"

    # Her tarih için Gamma API'den çek
    all_gamma: dict[str, dict[str, list]] = {}
    unique_dates = sorted(db_by_date.keys())
    for ds in unique_dates:
        td = datetime.strptime(ds, "%Y-%m-%d")
        gamma = _fetch_gamma_quick(td)
        for gds, cities in gamma.items():
            if gds not in all_gamma:
                all_gamma[gds] = {}
            for city, markets in cities.items():
                all_gamma[gds].setdefault(city, []).extend(markets)

    # Karşılaştır
    report_lines = []
    all_dates = sorted(set(list(all_gamma.keys()) + list(db_by_date.keys())))

    for date_str in all_dates:
        gamma_cities = set(all_gamma.get(date_str, {}).keys())
        db_cities = set(db_by_date.get(date_str, {}).keys())

        missing = gamma_cities - db_cities
        extra = db_cities - gamma_cities

        if not missing and not extra:
            continue

        report_lines.append(f"\n=== {date_str} ===")

        if missing:
            report_lines.append(f"  ❌ EKSİK ({len(missing)} şehir — Polymarket'te var, DB'de yok):")
            for city in sorted(missing):
                strikes = sorted({f"{m['threshold']}°C" for m in all_gamma[date_str][city]})
                report_lines.append(f"     {city}: {', '.join(strikes)}")

        if extra:
            report_lines.append(f"  ⚠️  FAZLA ({len(extra)} şehir — DB'de var, Polymarket'te yok):")
            for city in sorted(extra):
                strikes = sorted({f"{m['threshold']}°C" for m in db_by_date[date_str][city]})
                report_lines.append(f"     {city}: {', '.join(strikes)}")

        # Eşik farkları
        for city in gamma_cities & db_cities:
            g_t = {m["threshold"] for m in all_gamma[date_str][city]}
            d_t = {m["threshold"] for m in db_by_date[date_str][city]}
            diff = g_t.symmetric_difference(d_t)
            if diff:
                report_lines.append(f"  🔀 EŞİK FARKI — {city}:")
                for t in sorted(diff):
                    side = "sadece Gamma" if any(abs(t - x) < 0.5 for x in g_t) else "sadece DB"
                    report_lines.append(f"     {t}°C — {side}")

    return "\n".join(report_lines) if report_lines else ""


def _fetch_gamma_all(target_date: datetime) -> dict[str, dict[str, list[dict]]]:
    """Gamma API'den tüm sıcaklık marketlerini çek (city-specific sorgularla).

    Returns: {date_str: {city: [market_dict]}}
    """
    date_strs = _date_strings(target_date)
    # City-specific queries (scraper ile aynı)
    cities = [
        "dallas",
        "miami",
        "new york",
        "chicago",
        "houston",
        "los angeles",
        "phoenix",
        "denver",
        "seattle",
        "atlanta",
        "san francisco",
        "london",
        "paris",
        "tokyo",
        "seoul",
        "busan",
        "istanbul",
        "moscow",
        "berlin",
        "madrid",
        "rome",
        "amsterdam",
        "vienna",
        "warsaw",
        "copenhagen",
        "stockholm",
        "oslo",
        "helsinki",
        "dublin",
        "zurich",
        "brussels",
        "munich",
        "milan",
        "toronto",
        "vancouver",
        "sydney",
        "melbourne",
        "bangkok",
        "singapore",
        "hong kong",
        "taipei",
        "shanghai",
        "shenzhen",
        "guangzhou",
        "beijing",
        "chongqing",
        "chengdu",
        "wuhan",
        "qingdao",
        "mumbai",
        "delhi",
        "lucknow",
        "dubai",
        "johannesburg",
        "cape town",
        "cairo",
        "buenos aires",
        "sao paulo",
        "mexico city",
        "lima",
        "santiago",
        "ankara",
        "tel aviv",
        "riyadh",
        "jeddah",
        "karachi",
        "dhaka",
        "jakarta",
        "manila",
        "hanoi",
        "kuala lumpur",
        "wellington",
        "austin",
        "boston",
        "nashville",
        "portland",
        "las vegas",
        "minneapolis",
        "detroit",
        "philadelphia",
        "washington",
        "charlotte",
        "indianapolis",
        "columbus",
        "san diego",
        "tampa",
        "orlando",
        "sacramento",
        "pittsburgh",
        "st louis",
        "baltimore",
        "milwaukee",
        "kansas city",
        "salt lake city",
        "panama city",
    ]
    queries = ["highest temperature", "lowest temperature"]
    queries += [f"{c} temperature" for c in cities]

    result: dict[str, dict[str, list[dict]]] = {}
    seen_ids: set[str] = set()

    for query in queries:
        try:
            resp = requests.get(
                f"{GAMMA_URL}/public-search",
                params={"q": query, "limit_per_type": 100},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json().get("events", []) or []

            for event in events:
                title = event.get("title", "")
                if not any(d in title for d in date_strs):
                    continue

                for market in event.get("markets", []):
                    mid = str(market.get("id", ""))
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    question = market.get("question", "") or title
                    city = _extract_city_from_title(title)
                    if not city:
                        continue
                    threshold = _extract_threshold_from_question(question)
                    if threshold is None:
                        continue
                    metric = _extract_metric(question)

                    td_match = re.search(
                        r"on\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d+)",
                        title,
                        re.IGNORECASE,
                    )
                    if td_match:
                        month_num = datetime.strptime(td_match.group(1), "%B").month
                        day = int(td_match.group(2))
                        mkt_date = target_date.replace(month=month_num, day=day)
                    else:
                        mkt_date = target_date

                    mkt_date_str = mkt_date.strftime("%Y-%m-%d")
                    city_key = _normalize_city(city)
                    result.setdefault(mkt_date_str, {}).setdefault(city_key, []).append(
                        {
                            "id": mid,
                            "threshold": threshold,
                            "metric": metric,
                            "question": question,
                        }
                    )
        except Exception:
            pass

    return result


def verify_db_vs_poly() -> str:
    """DB bet'leri ile Polymarket UI piyasalarını karşılaştır.

    Şehir-level kontrol: DB'deki her bet'in şehrü Polymarket'te mevcut mu?
    Rapor döndürür: eşleşmeyen bet'ler listelenir.
    """
    from database.models import Bet

    now = datetime.utcnow().replace(tzinfo=None)

    # DB: aktif bet'leri çek (city, metric, target_date)
    db_bets = {}
    with get_session() as session:
        bets = (
            session.query(Bet)
            .join(WeatherMarket, Bet.market_id == WeatherMarket.id)
            .filter(
                WeatherMarket.target_date.isnot(None),
                WeatherMarket.target_date >= now - timedelta(days=1),
                WeatherMarket.target_date <= now + timedelta(days=3),
                Bet.status.in_(["placed", "open", "partial_fill", "filled"]),
            )
            .all()
        )
        for b in bets:
            m = session.query(WeatherMarket).filter_by(id=b.market_id).first()
            if not m or not m.target_date:
                continue
            ds = m.target_date.strftime("%Y-%m-%d")
            city = (b.city or "").strip()
            metric = m.metric or "temperature_max"
            key = (ds, _normalize_city(city), metric)
            if key not in db_bets:
                db_bets[key] = {"city": city, "metric": metric, "date": ds, "bet_id": b.id, "count": 0}
            db_bets[key]["count"] += 1

    if not db_bets:
        return ""

    # Polymarket:hem events/keyset hem public-search ile çek
    # events/keyset bazı temperature_min piyasalarını kaçırıyor
    target_dates = set(k[0] for k in db_bets.keys())
    gamma = _fetch_gamma_keyset(target_dates)

    # Eksik şehirleri public-search ile tamamla
    missing_cities = set()
    for ds, city, metric in db_bets.keys():
        if ds not in gamma or city not in gamma.get(ds, {}):
            missing_cities.add(city)

    if missing_cities:
        # City-specific queries ile eksik şehirleri çek
        for city in missing_cities:
            for query in [f"{city} temperature", f"lowest temperature {city}", f"highest temperature {city}"]:
                try:
                    resp = requests.get(
                        f"{GAMMA_URL}/public-search",
                        params={"q": query, "limit_per_type": 50},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    events = resp.json().get("events", []) or []
                    for event in events:
                        title = event.get("title", "")
                        td_match = re.search(
                            r"on\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d+)",
                            title,
                            re.IGNORECASE,
                        )
                        if not td_match:
                            continue
                        month_num = datetime.strptime(td_match.group(1), "%B").month
                        day = int(td_match.group(2))
                        now = datetime.utcnow().replace(tzinfo=None)
                        mkt_date = now.replace(month=month_num, day=day)
                        mkt_date_str = mkt_date.strftime("%Y-%m-%d")
                        if mkt_date_str not in target_dates:
                            continue

                        extracted_city = _extract_city_from_title(title)
                        if not extracted_city:
                            continue
                        city_key = _normalize_city(extracted_city)

                        for market in event.get("markets", []):
                            question = market.get("question", "") or title
                            threshold = _extract_threshold_from_question(question)
                            if threshold is None:
                                continue
                            metric = _extract_metric(question)
                            gamma.setdefault(mkt_date_str, {}).setdefault(city_key, []).append(
                                {
                                    "id": str(market.get("id", "")),
                                    "threshold": threshold,
                                    "metric": metric,
                                    "question": question,
                                }
                            )
                except Exception:
                    pass

    poly_cities = set()  # (date, city, metric)
    for gds, cities in gamma.items():
        for city, markets in cities.items():
            for m in markets:
                poly_cities.add((gds, _normalize_city(city), m["metric"]))

    # Karşılaştır
    matched = set(db_bets.keys()) & poly_cities
    missing = set(db_bets.keys()) - poly_cities

    if not missing:
        return ""

    lines = [f"DB vs Polymarket: {len(matched)}/{len(db_bets)} eslesti, {len(missing)} eslesmedi:"]
    for key in sorted(missing):
        info = db_bets[key]
        lines.append(
            f"  {info['date']} {info['city']:18s} {info['metric']:18s} ({info['count']} bet, bet#{info['bet_id']})"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] != "--all":
        td = datetime.strptime(sys.argv[1], "%Y-%m-%d")
        gamma = _fetch_gamma_quick(td)
        for ds in sorted(gamma.keys()):
            for city in sorted(gamma[ds].keys()):
                for m in gamma[ds][city]:
                    print(f"{ds}  {city:20s}  {m['threshold']:6.1f}°C  {m['metric']}")
    else:
        report = verify_all_open_dates()
        if report:
            import io

            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            print(report)
            sys.exit(1)
        else:
            print("✅ Tüm marketler eşleşiyor")
