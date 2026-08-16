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
        """UTC 14:59'a kadar (15 oncesi) 2 dusus olsa bile kilitlenmez."""
        from scrapers.metar import detect_peak

        # 12:00 30, 13:00 29, 14:00 28 -> 2 dusus ama saat < 15 -> kilitlenmez
        rows = _rows_at([(12, 30.0), (13, 29.0), (14, 28.0)])
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
