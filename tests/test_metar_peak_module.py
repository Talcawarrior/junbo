"""METAR-peak modul testleri (2026-08-16, guncel 2026-08-18).

2026-08-18 kullanici kararlari:
  - "Metar betleri acilirken bias a gerek yok" -> bias-top sehir filtresi
    KALDIRILDI, TUM sehirlerin acik marketlerine bakilir.
  - "ya koy" -> kilitli peak ASILIRSA (cur_max > peak) yanlis bucket betleri
    2 dusus beklenmeden DERHAL kapatilir (Milan 18 Agu canli ornegi).
"""

import sys

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")

from datetime import datetime, timedelta, timezone

import jobs.metar_peak as mp


class TestMetarPeakConfig:
    def test_metar_stake_is_3_usd(self):
        """Kullanici karari 2026-08-16: METAR bet stake 3 USD (optimum)."""
        assert mp.METAR_STAKE == 3.0

    def test_min_hours_before_close(self):
        """Kullanici karari 2026-08-16: erken giris -> kapanisa <2 saat kala bet acilmaz."""
        assert mp.MIN_HOURS_BEFORE_CLOSE == 2


class TestMetarPeakMarketTypeFilter:
    """BUGFIX 2026-08-18: peak mantigi sadece temperature_max + RANGE (tam
    bucket) marketlerine bet acar. Canli 9 bet HIGH/LOW/min marketlerine
    acilmisti (hepsi 0.01 entry, 4 lost) — or-above/or-below marketleri tam
    bucket kazananli degildir, min marketlerine round(peak) ile bet acilamaz.
    """

    def test_sadece_range_temperature_max_markete_bet_acilir(self, market_factory):
        """Bias verisi OLMAYAN sehir de islenir (2026-08-18: bias filtresi
        kaldirildi) ve SADECE RANGE + temperature_max markete bet acilir."""
        from unittest.mock import patch

        from database.db import get_session
        from database.models import WeatherMarket

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


class TestMetarPeakBrokenLock:
    """2026-08-18 kullanici: "ya koy" — kilitli peak asilirsa (cur_max >
    kilitli peak) yanlis bucket betleri 2 dusus beklenmeden kapatilir.
    Milan 18 Agu: kilit 31C, sonra 32C geldi; eski kod beklerken 31C fiyati
    0.0005'e coktu ve bet -$3 kaybetti.
    """

    def test_kilit_asilinca_kapatma_cagrilir_bet_acilmaz(self, market_factory):
        from unittest.mock import patch

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # kilitli peak 24.0 ama son gozlem 25.0 (zirve asildi)
        rows = [
            (int(now.timestamp()) - 3600, 24.0),
            (int(now.timestamp()) - 1800, 25.0),
        ]
        tgt = now + timedelta(hours=8)
        m_24 = market_factory(
            city="Milan",
            city_code="LIMC",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
        )
        closed_calls: list[float] = []

        with (
            patch("scrapers.metar.fetch_metar_day", return_value=rows),
            patch("scrapers.metar.detect_peak", return_value=(24.0, True)),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_open_metar_bet", return_value=None) as fake_bet,
            patch.object(
                mp,
                "_close_wrong_bucket_bets",
                side_effect=lambda _s, _c, _td, bucket: closed_calls.append(bucket),
            ),
        ):
            mp.run_metar_peak_bets()

        # yeni zirve (25.0) kazanan sayilir, kapatma onunla cagrilir
        # (diger testlerden kalan sehir marketleri de ayni mock'u gorur;
        # onlar icin de kapatma 25.0 ile cagrilir — hepsi ayni kural)
        assert closed_calls and all(c == 25.0 for c in closed_calls)
        # eski kilitli bucket'a (24) yeni bet ACILMAZ
        fake_bet.assert_not_called()
        assert m_24 is not None
