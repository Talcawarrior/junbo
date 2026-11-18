"""Settlement analiz scripti — belirli bir tarih icin tum YES kazananlari analiz et.

Kullanim: python scripts/analyze_settlement.py <gun> <ay>
Ornek: python scripts/analyze_settlement.py 3 8 (3 Agustos)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from database.db import get_session
from database.models import MarketSnapshot


# Sehir UTC offsetleri
CITY_OFFSET = {
    "Wellington": 12,
    "Seoul": 9,
    "Tokyo": 9,
    "Hong Kong": 8,
    "Shanghai": 8,
    "Beijing": 8,
    "Shenzhen": 8,
    "Singapore": 8,
    "Kuala Lumpur": 8,
    "Manila": 8,
    "Chongqing": 8,
    "Chengdu": 8,
    "Guangzhou": 8,
    "Taipei": 8,
    "Qingdao": 8,
    "Wuhan": 8,
    "London": 1,
    "Paris": 2,
    "Amsterdam": 2,
    "Madrid": 2,
    "Moscow": 3,
    "Helsinki": 3,
    "Warsaw": 2,
    "Milan": 2,
    "Munich": 2,
    "Istanbul": 3,
    "Ankara": 3,
    "Tel Aviv": 3,
    "New York": -4,
    "Toronto": -4,
    "Miami": -4,
    "Chicago": -5,
    "Houston": -5,
    "Dallas": -5,
    "Atlanta": -4,
    "Denver": -6,
    "Los Angeles": -7,
    "Seattle": -7,
    "San Francisco": -7,
    "Sao Paulo": -3,
    "Buenos Aires": -3,
    "Cape Town": 2,
}


def parse_city(question):
    for term in [" in ", " on "]:
        idx = question.lower().find(term)
        if idx > 0:
            rest = question[idx + len(term) :]
            for end in ["?", "be"]:
                ei = rest.lower().find(end)
                if ei > 0:
                    return rest[:ei].strip()
    return None


def fetch_yes_markets(target_date_str):
    """Belirli tarihin YES kazanan marketlerini cek."""
    markets = []
    for q in ["highest temperature", "lowest temperature"]:
        r = requests.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": q, "limit_per_type": 200},
            timeout=15,
        )
        if r.status_code != 200:
            continue
        for e in r.json().get("events", []):
            for m in e.get("markets", []):
                question = m.get("question", "")
                # Tarih kontrolu
                if target_date_str.lower() not in question.lower():
                    continue
                if m.get("closed") and float(m.get("lastTradePrice", 0) or 0) >= 0.95:
                    city = parse_city(question)
                    if city:
                        markets.append(
                            {
                                "city": city,
                                "market_id": m.get("id"),
                                "question": question[:70],
                            }
                        )
    return markets


def analyze_date(day, month):
    target_date_str = f"August {day}"
    print(f"=== {target_date_str.upper()} SETTLEMENT ANALIZI ===")
    print()

    yes_markets = fetch_yes_markets(target_date_str)
    print(f"YES kazanan market: {len(yes_markets)}")

    with get_session() as s:
        results = []
        for ym in yes_markets:
            snaps = (
                s.query(MarketSnapshot)
                .filter(MarketSnapshot.market_id == ym["market_id"])
                .order_by(MarketSnapshot.snapshot_time.asc())
                .all()
            )

            if len(snaps) < 2:
                continue

            city_offset = CITY_OFFSET.get(ym["city"], 0)

            # Settlement oncesi en yuksek YES
            best_snap = None
            best_yes = 0
            for snap in snaps:
                if snap.yes_price < 0.95 and snap.yes_price > best_yes:
                    best_yes = snap.yes_price
                    best_snap = snap

            if not best_snap:
                continue

            # Zirveden sonra dustu mu?
            zirve_idx = snaps.index(best_snap)
            dustu = False
            for snap in snaps[zirve_idx + 1 :]:
                if snap.yes_price < best_yes - 0.05:
                    dustu = True
                    break

            local_hour = (best_snap.snapshot_time.hour + city_offset) % 24
            kazanc = (1.0 - best_yes) * 10

            results.append(
                {
                    "city": ym["city"],
                    "zirve_saat": local_hour,
                    "zirve_yes": best_yes,
                    "dustu": dustu,
                    "kazanc": kazanc,
                    "snap_count": len(snaps),
                }
            )

        # Sonuclari listele
        print()
        print("SEHIR | ZIRVE SAATI | YES | DUSTU MU? | KAZANC ($10)")
        print("-" * 65)
        for r in sorted(results, key=lambda x: x["zirve_yes"], reverse=True):
            durum = "DUSTU" if r["dustu"] else "KALDI"
            print(
                f"  {r['city'][:18]:18s} | {r['zirve_saat']:02d}:00     | "
                f"{r['zirve_yes']:.3f} | {durum:8s} | ${r['kazanc']:.2f}"
            )

        # Ozet
        kalan = [r for r in results if not r["dustu"]]
        dusen = [r for r in results if r["dustu"]]
        toplam_kazanc = sum(r["kazanc"] for r in kalan)

        print()
        print("OZET:")
        print(f"  Toplam YES market: {len(results)}")
        print(f"  Zirve yuksek kalan: {len(kalan)}")
        print(f"  Dusen: {len(dusen)}")
        print(f"  Potansiyel toplam kar: ${toplam_kazanc:.2f} ($10/bet ile)")
        print(f"  Ortalama kar: ${toplam_kazanc / len(kalan):.2f}" if kalan else "")


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        day = int(sys.argv[1])
        month = int(sys.argv[2])
    else:
        day = 3
        month = 8
    analyze_date(day, month)
