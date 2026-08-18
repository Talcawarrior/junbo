"""SIMDIKI KAZANAN CONFIG SABITLEME TESTLERI (2026-08-18).

Kullanici: "bu tespitleri, simdiki config i kesinlikle not et, ileride
kaybetmeyelim, bu configi test edip bulacak testleri yaz."

Bu dosya 2026-08-18'de backtest ile dogrulanan config'i sabitler
(05-17 Agu: metar_peak_live 202 bet %88.6 +$593.62 ROI %+98; gunluk
BIRLESIK +$955.31 ROI %+120.5). .env veya kod yanlislikla degisirse bu
testler PATLAR — degisiklik bilincli karar gerektirir.

Sabitlenen kurallar:
  - SPREAD: radius=0, bias-top 15, max_entry 0.95, stake $2, gunluk 120 bet
  - METAR-PEAK: stake $3, MIN_ENTRY 0.10, kapanisa <2h yok, SADECE
    temperature_max + RANGE, bias filtre YOK (tum sehirler)
  - KILIT: yerel 13:00+ + 1 DUSUS (kullanici: "20 21 22 22 21 diyorsa 22")
  - AKTAR: zirve asilirsa kapat + yeni zirveye dusus beklemeden bet
  - ESIK: half-up int(x+0.5) — US esikleri float C (35.9 -> bucket 36)
  - PAPER MODE: bet_placer _live_allowed=False dokunulmaz
"""

import os
import sys

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")


def _env_value(key: str) -> str | None:
    """.env dosyasindan ham degeri oku (pytest .env yuklemez — config'in
    KAYNAGI .env oldugu icin test dogrudan dosyayi sabitler)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return None


class TestMetarPeakConfigLocked:
    """METAR-peak sabitleri — canli kod (jobs/metar_peak.py)."""

    def test_metar_stake_3(self):
        import jobs.metar_peak as mp

        assert mp.METAR_STAKE == 3.0

    def test_min_entry_005(self):
        import jobs.metar_peak as mp

        assert mp.MIN_ENTRY == 0.05

    def test_min_hours_before_close_0(self):
        import jobs.metar_peak as mp

        assert mp.MIN_HOURS_BEFORE_CLOSE == 0

    def test_bias_top_cities_kaldirildi(self):
        """2026-08-18 kullanici: bias filtresi KALDIRILDI — tum sehirler."""
        import jobs.metar_peak as mp

        assert not hasattr(mp, "BIAS_TOP_CITIES"), "METAR-peak bias filtresi KALDIRILDI"


class TestSpreadConfigLocked:
    """SPREAD sabitleri — .env dosyasinin kendisini sabitler (config kaynagi)."""

    def test_spread_radius_0(self):
        assert _env_value("SPREAD_RADIUS") == "0", "radius=0 (tek esik) — backtest en iyi config"

    def test_spread_max_cities_15(self):
        assert _env_value("SPREAD_MAX_CITIES") == "15", "bias-top 15"

    def test_spread_max_entry_095(self):
        assert _env_value("SPREAD_MAX_ENTRY") == "0.95"

    def test_spread_stake_2(self):
        # .env'de SPREAD_STAKE_USD anahtari YOK — deger settings default'undan
        # gelir (2.0). Anahtar varsa da 2.0 olmali.
        v = _env_value("SPREAD_STAKE_USD")
        if v is None:
            from config.settings import bot_config

            assert bot_config.strategy.spread_stake_usd == 2.0
        else:
            assert v == "2.0"

    def test_spread_max_bets_120(self):
        assert _env_value("SPREAD_MAX_BETS_PER_DAY") == "120"

    def test_betting_strategy_spread(self):
        assert _env_value("BETTING_STRATEGY") == "spread"


class TestKilitKuraliBirDusus:
    """1 DUSUS kilit kurali — kullanici ornegi ile birebir (2026-08-18)."""

    def _rows(self, pairs):
        from datetime import datetime, timezone

        base = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
        return [(int(base.replace(hour=h).timestamp()), t) for h, t in pairs]

    def test_kullanici_ornegi_22_kilitler(self):
        """20 21 22 22 21 -> 22 kilitlenir (esitlik dusus sayilmaz)."""
        from scrapers.metar import detect_peak

        rows = self._rows([(13, 20.0), (14, 21.0), (15, 22.0), (16, 22.0), (17, 21.0)])
        peak, confirmed = detect_peak(rows)
        assert confirmed is True
        assert peak == 22.0

    def test_backtest_peak_lock_ayni_kural(self):
        """scripts/backtest.py peak_lock detect_peak ile BIREBIR olmali."""
        from scripts.backtest import peak_lock

        rows = self._rows([(13, 20.0), (14, 21.0), (15, 22.0), (16, 22.0), (17, 21.0)])
        peak, _lock = peak_lock(rows, 0.0)
        assert peak == 22.0

    def test_sabah_dususu_kilit_sayilmaz(self):
        """Yerel 13:00 oncesi dususler zirve sayilmaz (regresyon korumasi)."""
        from scrapers.metar import detect_peak

        rows = self._rows([(6, 30.0), (7, 29.0), (8, 28.0)])
        _peak, confirmed = detect_peak(rows)
        assert confirmed is False


class TestEsikHalfUp:
    """Half-up esik eslesmesi: US esikleri float C'dir (F'den donusum).

    int() truncate "market yok" yanlisligi uretiyordu (Austin 35.9C marketi
    bucket 36'ya karsilik gelir) — 2026-08-18 duzeltildi.
    """

    def test_359_bucket_36(self):
        assert int(35.9 + 0.5) == 36

    def test_347_bucket_35(self):
        assert int(34.7 + 0.5) == 35

    def test_370_bucket_37(self):
        assert int(37.0 + 0.5) == 37

    def test_metar_peak_karsilastirma_half_up(self):
        """jobs/metar_peak.py market eslemesi half-up kullanir (kod yapisi)."""
        import inspect

        import jobs.metar_peak as mp

        src = inspect.getsource(mp.run_metar_peak_bets)
        assert "int(float(m.threshold) + 0.5)" in src, "esik eslemesi half-up olmali"
        assert "int(float(m.threshold))" not in src.replace("int(float(m.threshold) + 0.5)", ""), (
            "truncate esleme kalmamali"
        )


class TestPaperMode:
    def test_live_allowed_false_kod_seviyesinde(self):
        """HER ZAMAN PAPER MODE — `_live_allowed = False` local atamasi
        bet acma akisinda KOD SEVIYESINDE sabittir (2026-08-07 kullanici
        karari). LIVE_TRADING_ENABLED benzeri anahtar OLMAMALIDIR."""
        import inspect

        from executor import bet_placer

        src = inspect.getsource(bet_placer.BetPlacer.place_bet)
        assert "_live_allowed = False" in src, "paper mode kilitli kalmalidir"
        assert "LIVE_TRADING_ENABLED =" not in src, "live trade anahtari EKLENEMEZ"
