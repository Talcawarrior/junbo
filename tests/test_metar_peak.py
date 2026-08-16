"""detect_peak ve METAR-peak koruma testleri (2026-08-16).

Bug (2026-08-15): detect_peak sabahin gece sicakligini (00:00'da 25C) zirve
sanip erken kilitleniyordu -> yanlis bucket'a bet acildi (Paris 25 vs gercek
31). Duzeltme: UTC >= 15:00 sonrasi kilitlenir. Bu test o regresyonu korur.
"""
import sys
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')

from datetime import datetime, timezone


def _rows_at(hours_temp):
    """[(epoch, temp)] — verilen (saat, sicaklik) ciftlerini UTC'ye cevirir."""
    base = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
    rows = []
    for hour, temp in hours_temp:
        dt = base.replace(hour=hour)
        rows.append((int(dt.timestamp()), temp))
    return rows


class TestDetectPeakMorningBug:
    def test_sabah_dususu_zirve_sayilmaz(self):
        """00:00'da 25C sonra sabah 23C'ye dusus zirve KILITLENMEMELI.

        Bug (2026-08-15): Paris 00:00=25C, sabah 23C -> eski kod 'peak 25
        kilitlendi' diyordu ama gercek max oglen sonrasi 31C idi.
        """
        from scrapers.metar import detect_peak

        # sabah dususu: 00:00 25, 01:00 25, 02:00 24, 03:00 23, 04:00 23
        rows = _rows_at([(0, 25.0), (1, 25.0), (2, 24.0), (3, 23.0), (4, 23.0)])
        peak, confirmed = detect_peak(rows)
        assert confirmed is False, f"sabah dususu zirve sayilmamali: peak={peak}"

    def test_ogleden_sonra_2_dusus_kilitler(self):
        """UTC 15:00 sonrasi 2 ardısık dusus zirveyi kilitler."""
        from scrapers.metar import detect_peak

        # 15:00 30, 16:00 31 (max), 17:00 30, 18:00 29 -> 31 kilitlenmeli
        rows = _rows_at([(13, 28.0), (14, 30.0), (15, 31.0), (16, 30.0), (17, 29.0)])
        peak, confirmed = detect_peak(rows)
        assert confirmed is True
        assert peak == 31.0

    def test_saat_esigi_oncesi_kilitlenmez(self):
        """Yerel 13:00 oncesi (sabah) 2 dusus olsa bile kilitlenmez."""
        from scrapers.metar import detect_peak

        # 06:00 30, 07:00 29, 08:00 28 -> 2 dusus ama yerel saat < 13 -> kilitlenmez
        rows = _rows_at([(6, 30.0), (7, 29.0), (8, 28.0)])
        peak, confirmed = detect_peak(rows)
        assert confirmed is False

    def test_gercek_paris_serisi_31_kilitler(self):
        """Gercek Paris 2026-08-15 serisi (bug bulgusu) 31 kilitlemeli."""
        from scrapers.metar import detect_peak

        # Paris: 00:00 25, sabah 22-23, oglen 28-30, 15:00-17:00 31, sonra 30-29
        rows = _rows_at([
            (0, 25.0), (3, 24.0), (4, 23.0), (5, 22.0), (8, 25.0),
            (9, 28.0), (11, 30.0), (12, 29.0), (14, 30.0), (15, 31.0),
            (16, 31.0), (17, 31.0), (18, 30.0), (19, 29.0),
        ])
        peak, confirmed = detect_peak(rows)
        assert confirmed is True
        assert peak == 31.0, f"Paris gercek max 31 olmali: {peak}"


class TestDetectPeakLocalTime:
    """2026-08-16 kullanici karari: peak sehirin YEREL saatine gore kilitlenir.

    Kullanici: "benim saatimle degil, sehirin yerel saatine gore gir. Yerel
    saatte en yuksek ne zaman oluyorsa o zaman gir." Sabit UTC esigi (15:00)
    dogu sehirlerinde peak'i kaciriyordu (Wellington 03:00 UTC max yapiyor).
    """

    def test_wellington_local_offset_erken_peak_yakalar(self):
        """Wellington (UTC+12): 03:00 UTC = yerel 15:00 -> peak kilitlenmeli."""
        from scrapers.metar import detect_peak

        # Wellington: 01:00 UTC 8, 02:00 9, 03:00 9 (max), 04:00 8, 05:00 7
        rows = _rows_at([(1, 8.0), (2, 9.0), (3, 9.0), (4, 8.0), (5, 7.0)])
        peak, confirmed = detect_peak(rows, utc_offset_hours=12.0)
        assert confirmed is True, f"Wellington yerel 15:00 peak kilitlenmeli: {peak}"
        assert peak == 9.0

    def test_wellington_same_rows_no_offset_not_confirmed(self):
        """Ayni seri offset'siz (UTC esigi 13) kilitlenmez — offset sart."""
        from scrapers.metar import detect_peak

        rows = _rows_at([(1, 8.0), (2, 9.0), (3, 9.0), (4, 8.0), (5, 7.0)])
        peak, confirmed = detect_peak(rows, utc_offset_hours=0.0)
        # 01-05 UTC = yerel 01-05 (offset 0) -> yerel < 13 -> kilitlenmez
        assert confirmed is False

    def test_hong_kong_local_offset(self):
        """Hong Kong (UTC+8): 07:00 UTC = yerel 15:00 -> peak kilitlenmeli."""
        from scrapers.metar import detect_peak

        rows = _rows_at([(5, 32.0), (6, 34.0), (7, 34.0), (8, 33.0), (9, 32.0)])
        peak, confirmed = detect_peak(rows, utc_offset_hours=8.0)
        assert confirmed is True, f"Hong Kong yerel 15:00 peak kilitlenmeli: {peak}"
        assert peak == 34.0


class TestCloseWrongBucketBets:
    """2026-08-16 kullanici karari (3. adim): peak kilitlenince kazanan bucket
    DISINDAKI acik betler kapatilir. Onceden 75 acik betin 69'u yanlis
    bucket'ta settlement'a kadar acik kaldi."""

    def test_closes_only_wrong_bucket(self):
        from database.db import get_session
        from database.models import Bet, Portfolio, WeatherMarket
        from datetime import datetime, timezone

        from jobs.metar_peak import _close_wrong_bucket_bets

        day = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        with get_session() as s:
            s.query(Bet).delete()
            s.query(WeatherMarket).delete()
            s.query(Portfolio).delete()
            pf = Portfolio(id=1, cash_balance=100.0, total_value=100.0)
            s.add(pf)
            # kazanan bucket 34C; 32C ve 34C marketleri
            w1 = WeatherMarket(id="M1", city="Hong Kong", city_code="VHHH",
                               question="Hong Kong temperature", threshold=32.0,
                               target_date=day, status="open",
                               yes_price=0.25, latitude=22.3, longitude=114.2)
            w2 = WeatherMarket(id="M2", city="Hong Kong", city_code="VHHH",
                               question="Hong Kong temperature", threshold=34.0,
                               target_date=day, status="open",
                               yes_price=0.45, latitude=22.3, longitude=114.2)
            s.add_all([w1, w2])
            b1 = Bet(id=1, market_id="M1", city="Hong Kong", side="YES",
                     amount=2.0, stake_amount=2.0, price=0.25, entry_price=0.25,
                     shares=8.0, current_price=0.25, status="placed",
                     strike_temp=32.0, order_id="spread_1")
            b2 = Bet(id=2, market_id="M2", city="Hong Kong", side="YES",
                     amount=2.0, stake_amount=2.0, price=0.45, entry_price=0.45,
                     shares=4.44, current_price=0.45, status="placed",
                     strike_temp=34.0, order_id="spread_2")
            s.add_all([b1, b2])
            s.commit()

            closed = _close_wrong_bucket_bets(s, "VHHH", day, 34)
            s.commit()
            statuses = {b.id: b.status for b in s.query(Bet).all()}
        assert closed == 1, f"Sadece yanlis bucket (32C) kapatilmali: {closed}"
        assert statuses[1] == "closed", "32C beti kapanmali"
        assert statuses[2] in ("placed", "open"), "34C kazanan bucket TUTULMALI"
