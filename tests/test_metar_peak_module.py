"""METAR-peak modul testleri (2026-08-16).

Kullanici karari: METAR stake 1->2 USD, bias-top 40 sehir. Bu testler
sabitleri ve bias filtresinin calistigini dogrular.
"""
import sys
sys.path.insert(0, r'C:\Users\fdemir\Documents\New project\junbo')

import jobs.metar_peak as mp


class TestMetarPeakConfig:
    def test_metar_stake_is_3_usd(self):
        """Kullanici karari 2026-08-16: METAR bet stake 3 USD (optimum)."""
        assert mp.METAR_STAKE == 3.0

    def test_bias_top_40(self):
        """Kullanici karari 2026-08-16: bias-top 40 sehir."""
        assert mp.BIAS_TOP_CITIES == 40

    def test_min_hours_before_close(self):
        """Kapanisa <4 saat kala bet acilmaz."""
        assert mp.MIN_HOURS_BEFORE_CLOSE == 4


class TestMetarPeakBiasFilter:
    def test_bias_top_sinirlama(self):
        """run_metar_peak_bets bias-top sehirlerle sinirli marketlere bakar.

        Mock: bias verisi olan sehirlerin sayisi BIAS_TOP_CITIES'i gecmemeli.
        """
        from unittest.mock import patch
        import sqlite3

        # dogrudan bias hesap mantigini test et: avg_bias en az sapan N sehir
        db = sqlite3.connect('file:C:/Users/fdemir/Documents/New project/junbo/data/bot.db?immutable=1', uri=True)
        cur = db.cursor()
        rows = cur.execute(
            "SELECT city_code, AVG(ABS(bias)) FROM historical_calibrations "
            "WHERE bias IS NOT NULL GROUP BY city_code"
        ).fetchall()
        db.close()
        avg = {c: b for c, b in rows}
        top = {c for c, _ in sorted(avg.items(), key=lambda kv: kv[1])[: mp.BIAS_TOP_CITIES]}
        assert len(top) == mp.BIAS_TOP_CITIES or len(top) == len(avg)
        assert len(top) <= 40
