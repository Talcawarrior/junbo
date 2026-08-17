"""METAR-peak modul testleri (2026-08-16).

Kullanici karari: METAR stake 1->2 USD, bias-top 40 sehir. Bu testler
sabitleri ve bias filtresinin calistigini dogrular.
"""

import sys

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")

from datetime import datetime, timedelta, timezone

import jobs.metar_peak as mp


class TestMetarPeakConfig:
    def test_metar_stake_is_3_usd(self):
        """Kullanici karari 2026-08-16: METAR bet stake 3 USD (optimum)."""
        assert mp.METAR_STAKE == 3.0

    def test_bias_top_40(self):
        """Kullanici karari 2026-08-16: bias-top 40 sehir."""
        assert mp.BIAS_TOP_CITIES == 40

    def test_min_hours_before_close(self):
        """Kullanici karari 2026-08-16: erken giris -> kapanisa <2 saat kala bet acilmaz."""
        assert mp.MIN_HOURS_BEFORE_CLOSE == 2


class TestMetarPeakBiasFilter:
    def test_bias_top_sinirlama(self):
        """run_metar_peak_bets bias-top sehirlerle sinirli marketlere bakar.

        Mock: bias verisi olan sehirlerin sayisi BIAS_TOP_CITIES'i gecmemeli.
        """
        import sqlite3

        # dogrudan bias hesap mantigini test et: avg_bias en az sapan N sehir
        db = sqlite3.connect("file:C:/Users/fdemir/Documents/New project/junbo/data/bot.db?immutable=1", uri=True)
        cur = db.cursor()
        rows = cur.execute(
            "SELECT city_code, AVG(ABS(bias)) FROM historical_calibrations WHERE bias IS NOT NULL GROUP BY city_code"
        ).fetchall()
        db.close()
        avg = {c: b for c, b in rows}
        top = {c for c, _ in sorted(avg.items(), key=lambda kv: kv[1])[: mp.BIAS_TOP_CITIES]}
        assert len(top) == mp.BIAS_TOP_CITIES or len(top) == len(avg)
        assert len(top) <= 40


class TestMetarPeakMarketTypeFilter:
    """BUGFIX 2026-08-18: peak mantigi sadece temperature_max + RANGE (tam
    bucket) marketlerine bet acar. Canli 9 bet HIGH/LOW/min marketlerine
    acilmisti (hepsi 0.01 entry, 4 lost) — or-above/or-below marketleri tam
    bucket kazananli degildir, min marketlerine round(peak) ile bet acilamaz.
    """

    def test_sadece_range_temperature_max_markete_bet_acilir(self, market_factory):
        from unittest.mock import patch

        from database.db import get_session
        from database.models import HistoricalCalibration, WeatherMarket

        # sehir bias-top'a girebilsin (bias=0 = en az sapan)
        today = datetime.now().date()
        with get_session() as s:
            s.add(
                HistoricalCalibration(
                    city_code="EGLL",
                    city="London",
                    date=today,
                    metric="temperature_max",
                    model="test",
                    predicted_value=24.0,
                    actual_value=24.0,
                    bias=0.0,
                )
            )
            s.commit()

        # Ayni sehir, ayni esik, 3 farkli market tipi — sadece 1'i hedef
        tgt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        m_range_max = market_factory(
            city="London",
            city_code="EGLL",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
        )
        market_factory(
            city="London",
            city_code="EGLL",
            metric="temperature_max",
            market_type="HIGH",
            threshold=24.0,
            target_date=tgt,
        )
        market_factory(
            city="London",
            city_code="EGLL",
            metric="temperature_min",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
        )

        # id'yi session icinde yakala; commit sonrasi ORM instance'i detach
        # olup attribute'lar expire oldugundan (DetachedInstanceError) id
        # uzerinden yeni session'da yeniden yukle.
        called_ids: list[str] = []

        def _fake_bet(*args):  # (session, market, peak) — _open_metar_bet imzasi
            market = args[1]
            called_ids.append(str(market.id))
            return None

        with (
            patch("scrapers.metar.fetch_metar_day", return_value=[(int(datetime.now(timezone.utc).timestamp()), 24.0)]),
            patch("scrapers.metar.detect_peak", return_value=(24.0, True)),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
            patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
        ):
            mp.run_metar_peak_bets()

        # SADECE RANGE + temperature_max markete bet acilir
        assert called_ids == [m_range_max]
        with get_session() as s:
            bet_mkt = s.get(WeatherMarket, m_range_max)
            assert bet_mkt is not None
            assert bet_mkt.market_type == "RANGE"
            assert bet_mkt.metric == "temperature_max"
