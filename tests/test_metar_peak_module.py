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
        """2026-08-18 E config: kapanisa kadar bet acilir (0) — yanlis bucket
        kapatmalari bundan ETKILENMEZ (aktar mekanizmasi ayri calisir)."""
        assert mp.MIN_HOURS_BEFORE_CLOSE == 0


class TestMetarPeakMarketTypeFilter:
    """BUGFIX 2026-08-18: peak mantigi sadece temperature_max + RANGE (tam
    bucket) marketlerine bet acar. Canli 9 bet HIGH/LOW/min marketlerine
    acilmisti (hepsi 0.01 entry, 4 lost) — or-above/or-below marketleri tam
    bucket kazananli degildir, min marketlerine round(peak) ile bet acilamaz.
    """

    def test_sadece_range_temperature_max_markete_bet_acilir(self, market_factory):
        """Bias verisi OLMAYAN sehir de islenir (2026-08-18: bias filtresi
        kaldirildi) ve SADECE RANGE + temperature_max markete bet acilir."""
        import time as _time

        from unittest.mock import patch

        from database.db import get_session
        from database.models import WeatherMarket

        # Ayni sehir, ayni esik, 3 farkli market tipi — sadece 1'i hedef.
        # 2026-08-19: target_date BUGUN olmali (yarinin marketleri islenmez).
        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
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
            patch(
                "scrapers.metar.fetch_metar_day",
                # 2026-08-20: gercek epoch (time.time) — naive .timestamp()
                # lokal tz ile yorumlanip bayat korumasini yaniltiyordu.
                return_value=[(int(_time.time()), 24.0)],
            ),
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
    """2026-08-18 kullanici: "23 e ciktiginda 1 adet dusmesini beklemeyecek
    hemen acacak, cunku 21 den 23 e cikti" — kilitli peak ASILIRSA (cur_max >
    kilitli peak) eski bucket betleri kapatilir VE yeni zirvenin bucket'ina
    dusus beklenmeden bet acilir. Milan 18 Agu: kilit 31C, sonra 32C geldi.
    """

    def test_kilit_asilinca_kapat_ve_yeni_peake_ac(self, market_factory):
        import time as _time

        from unittest.mock import patch

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # kilitli peak 24.0 ama son gozlem 25.0 (zirve asildi).
        # 2026-08-20: gozlemler GUNCEL GERCEK epoch olmali — naive .timestamp()
        # lokal tz ile yorumlanip 45dk bayat korumasini yaniltiyordu.
        rows = [
            (int(_time.time()) - 120, 24.0),
            (int(_time.time()) - 60, 25.0),
        ]
        # 2026-08-19: target_date BUGUN olmali (yarinin marketleri islenmez).
        tgt = now
        m_24 = market_factory(
            city="Milan",
            city_code="LIMC",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
        )
        m_25 = market_factory(
            city="Milan",
            city_code="LIMC",
            metric="temperature_max",
            market_type="RANGE",
            threshold=25.0,
            target_date=tgt,
        )
        closed_calls: list[float] = []
        bet_calls: list[float] = []

        def _fake_bet(_session, market, peak):
            bet_calls.append(float(market.threshold))
            return None

        with (
            patch("scrapers.metar.fetch_metar_day", return_value=rows),
            patch("scrapers.metar.detect_peak", return_value=(24.0, True)),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
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
        # ESKI kilitli bucket'a (24) bet YOK; YENI zirveye (25) dusus
        # beklenmeden bet acilir
        assert 25.0 in bet_calls
        assert 24.0 not in bet_calls
        assert m_24 is not None
        assert m_25 is not None


class TestMetarPeakBlacklist:
    """2026-08-20 kullanici onayi: METAR havalimani istasyonu WU sehir
    verisinden sistematik sapan sehirler (VHHH %20, ZGSZ %29 tutma) — bu
    sehirlerde METAR-peak bet acilmaz; veri toplama devam eder.
    """

    def test_kara_liste_sehrine_bet_acilmaz(self, market_factory):
        import time as _time

        from unittest.mock import patch

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        m_hk = market_factory(
            city="Hong Kong",
            city_code="VHHH",
            metric="temperature_max",
            market_type="RANGE",
            threshold=30.0,
            target_date=tgt,
            yes_price=0.30,
        )
        called_ids: list[str] = []

        def _fake_bet(*args):
            market = args[1]
            called_ids.append(str(market.id))
            return None

        with (
            patch(
                "scrapers.metar.fetch_metar_day",
                return_value=[(int(_time.time()) - 120, 30.0), (int(_time.time()) - 60, 30.0)],
            ),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_avg_peak_hour", return_value=0.0),  # saat gelmis
            patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
            patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
        ):
            mp.run_metar_peak_bets()

        assert called_ids == []  # kara listede -> bet acilmaz
        assert "VHHH" in mp.METAR_PEAK_BLACKLIST
        assert m_hk is not None

    def test_kara_liste_sehri_peak_watch_satiri_durust(self, market_factory):
        """2026-08-20 kullanici: donmus "kilitli" satir yerine kara listedeki
        sehir "kara liste (bet yok)" statüsüyle yazilir (peak=None)."""
        import time as _time

        from unittest.mock import patch

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        market_factory(
            city="Hong Kong",
            city_code="VHHH",
            metric="temperature_max",
            market_type="RANGE",
            threshold=30.0,
            target_date=tgt,
            yes_price=0.30,
        )
        with (
            patch(
                "scrapers.metar.fetch_metar_day",
                return_value=[(int(_time.time()) - 120, 30.0), (int(_time.time()) - 60, 30.0)],
            ),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_open_metar_bet", return_value=None),
            patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
            patch("utils.activity_log.update_peak_watch") as _uw,
        ):
            mp.run_metar_peak_bets()

        rows = _uw.call_args[0][0] if _uw.call_args else []
        hk = [r for r in rows if r["city"] == "Hong Kong"]
        assert hk, "kara listedeki sehrin peak_watch satiri olmali"
        assert hk[0]["status"] == "kara liste (bet yok)"
        assert hk[0]["peak"] is None  # kilit YOK


class TestMetarPeakStaleRefresh:
    """2026-08-20 kullanici: "duzeltmeye calismadi" — bayat METAR gorunce
    pasif atlamak yerine DERHAL yeniden cekim denenir; taze veri gelirse
    bet mantigi devam eder."""

    def test_bayat_metar_yeniden_cekim_taze_gelirse_bet_acilir(self, market_factory):
        import time as _time

        from unittest.mock import patch

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        m_24 = market_factory(
            city="London",
            city_code="EGLC",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        called_ids: list[str] = []
        stale = int(_time.time()) - 3600  # 60 dk eski (bayat)

        def _fake_bet(*args):
            market = args[1]
            called_ids.append(str(market.id))
            return None

        def _fetch_side(*a, **k):
            # ilk cagri bayat seri, ikinci cagri (yeniden cekim) taze
            if _fetch_side.n == 0:
                _fetch_side.n += 1
                return [(stale, 23.0), (stale + 600, 24.0)]
            return [(stale + 3600, 24.0), (int(_time.time()), 24.0)]

        _fetch_side.n = 0

        with (
            patch("scrapers.metar.fetch_metar_day", side_effect=_fetch_side),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_avg_peak_hour", return_value=0.0),
            patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
            patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
        ):
            mp.run_metar_peak_bets()

        # bayat -> yeniden cekim -> taze veri -> bet ACILIR
        assert called_ids == [m_24]

    def test_bayat_metar_yeniden_cekim_de_bayat_atlanir(self, market_factory):
        import time as _time

        from unittest.mock import patch

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        m_24 = market_factory(
            city="Paris",
            city_code="LFPB",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        called_ids: list[str] = []
        stale = int(_time.time()) - 3600  # 60 dk eski (bayat)

        def _fake_bet(*args):
            market = args[1]
            called_ids.append(str(market.id))
            return None

        with (
            # iki cagri da bayat (yeniden cekim cozmedi)
            patch("scrapers.metar.fetch_metar_day", return_value=[(stale, 24.0)]),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch.object(mp, "_avg_peak_hour", return_value=0.0),
            patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
            patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
        ):
            mp.run_metar_peak_bets()

        assert called_ids == []  # tazelenemedi -> atlandi
        assert m_24 is not None

    def test_bayat_veride_aktar_calisir_yeni_bet_yok(self, market_factory):
        """2026-08-20 kullanici onayi: bayat veri YENI bet acmayi engeller
        ama aktar (yanlis bucket kapatma) calismaya devam eder."""
        import time as _time

        from unittest.mock import patch

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        m_24 = market_factory(
            city="Amsterdam",
            city_code="EHAM",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        called_ids: list[str] = []
        closed_calls: list[float] = []
        stale = int(_time.time()) - 3600  # 60 dk eski (bayat)

        def _fake_bet(*args):
            market = args[1]
            called_ids.append(str(market.id))
            return None

        with (
            # yeniden cekim de bayat doner
            patch("scrapers.metar.fetch_metar_day", return_value=[(stale, 24.0)]),
            patch("scrapers.metar.archive_metar_observations", return_value=0),
            patch("scrapers.metar.detect_peak", return_value=(24.0, True)),
            patch.object(mp, "_avg_peak_hour", return_value=0.0),
            patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
            patch.object(
                mp,
                "_close_wrong_bucket_bets",
                side_effect=lambda _s, _c, _td, bucket: closed_calls.append(bucket),
            ),
        ):
            mp.run_metar_peak_bets()

        # aktar CALISTI (kazanan bucket 24 ile kapatma denendi)...
        assert closed_calls and all(c == 24.0 for c in closed_calls)
        # ...ama bayat veri yuzunden YENI BET ACILMADI
        assert called_ids == []
        assert m_24 is not None


class TestMetarPeakStaleThreshold:
    """2026-08-20 kullanici onayi: bayat esigi istasyon kadansina gore.
    60dk (saatlik) istasyonlarda 90dk — 45dk esigi her saat tetikleniyordu
    (Wuhan/Chongqing/Qingdao/Busan); 30dk istasyonlarda 45dk kalir."""

    def test_saatlik_istasyonda_90dk_esik(self):
        from database.db import get_session
        from database.models import MetarObservation

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with get_session() as s:
            for i in range(10):
                obs = now - timedelta(hours=10 - i)
                s.add(
                    MetarObservation(
                        city_code="ZHHH",
                        city="Wuhan",
                        temp_c=30.0,
                        obs_time=obs,
                        day=obs.strftime("%Y-%m-%d"),
                    )
                )
            s.commit()
            # 60dk araliklar -> medyan >= 55 -> 90dk esik
            assert mp._stale_threshold_min(s, "ZHHH") == 90.0

    def test_30dk_istasyonda_45dk_esik(self):
        from database.db import get_session
        from database.models import MetarObservation

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with get_session() as s:
            for i in range(10):
                obs = now - timedelta(minutes=30 * (10 - i))
                s.add(
                    MetarObservation(
                        city_code="EGLC",
                        city="London",
                        temp_c=20.0,
                        obs_time=obs,
                        day=obs.strftime("%Y-%m-%d"),
                    )
                )
            s.commit()
            # 30dk araliklar -> 45dk esik (varsayilan)
            assert mp._stale_threshold_min(s, "EGLC") == 45.0

    def test_veri_yoksa_45dk_varsayilan(self):
        from database.db import get_session

        with get_session() as s:
            assert mp._stale_threshold_min(s, "BILINMEYEN") == 45.0


class TestMetarPeakDailyCap:
    """2026-08-21 kullanici karari: gunluk METAR-peak bet cap'i
    (METAR_PEAK_MAX_BETS_PER_DAY=12, backtest cap12 en karli +$834 %82.6).
    Cap dolunca YENI bet acilmaz; aktar kapatma cap'tan bagimsiz devam eder."""

    def test_cap_dolunca_bir_market_disari_kalir(self, market_factory):
        """cap=1 iken 2 uygun marketten SADECE 1'i bet acar (ikisi de ayni
        bucket 24C — ikisi qualify olur, cap ikincisini keser)."""
        import time as _time

        from unittest.mock import patch

        from config.settings import bot_config
        from database.db import get_session
        from database.models import Bet

        # 2026-08-21: conftest temp DB session-scoped (paylasimli) — onceki
        # testlerin biraktigi bet/archive satirlari cap sayacini veya bucket'i
        # kirletir. Temiz defter ile basla (TestCloseWrongBucketBets deseni).
        from database.models import MetarObservation, WeatherMarket

        with get_session() as s:
            s.query(Bet).delete()
            s.query(MetarObservation).delete()
            s.query(WeatherMarket).delete()
            s.commit()

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        market_factory(
            city="London",
            city_code="EGLC",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        market_factory(
            city="Paris",
            city_code="LFPG",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        called_ids: list[str] = []

        def _fake_bet(*args):
            market = args[1]
            called_ids.append(str(market.id))
            # basarili acilis simule et (None donerse cap sayaci artmaz)
            from types import SimpleNamespace

            return SimpleNamespace(entry_price=0.5, stake_amount=3.0)

        bot_config.strategy.metar_peak_max_bets_per_day = 1  # test: cap=1
        try:
            with (
                patch(
                    "scrapers.metar.fetch_metar_day",
                    return_value=[(int(_time.time()) - 120, 23.0), (int(_time.time()) - 60, 24.0)],
                ),
                patch("scrapers.metar.archive_metar_observations", return_value=0),
                patch.object(mp, "_avg_peak_hour", return_value=0.0),  # saat gelmis
                patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
                patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
            ):
                mp.run_metar_peak_bets()
        finally:
            bot_config.strategy.metar_peak_max_bets_per_day = 12
        assert len(called_ids) == 1, f"cap=1 iken yalnizca 1 bet acilmali: {called_ids}"

    def test_cap_mevcut_gunluk_betlerle_birlikte_sayilir(self, market_factory):
        """Gun icinde acilan metar beti cap'e sayilir — bugun zaten 1 metar bet
        varsa cap=1 iken YENI bet acilmaz."""
        import time as _time

        from unittest.mock import patch

        from config.settings import bot_config
        from database.db import get_session
        from database.models import Bet

        # 2026-08-21: paylasimli temp DB temiz defter (onceki test kirliligi).
        from database.models import MetarObservation, WeatherMarket

        with get_session() as s:
            s.query(Bet).delete()
            s.query(MetarObservation).delete()
            s.query(WeatherMarket).delete()
            s.commit()

        tgt = datetime.now(timezone.utc).replace(tzinfo=None)
        # qualify olan market (RANGE temp_max)
        market_factory(
            city="London",
            city_code="EGLC",
            metric="temperature_max",
            market_type="RANGE",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        # qualify OLMAYAN market (HIGH — run_metar_peak_bets'de aday degil);
        # bu markete bugun acilmis metar beti cap sayacina girer.
        m_high = market_factory(
            city="Paris",
            city_code="LFPG",
            metric="temperature_max",
            market_type="HIGH",
            threshold=24.0,
            target_date=tgt,
            yes_price=0.30,
        )
        called_ids: list[str] = []

        def _fake_bet(*args):
            market = args[1]
            called_ids.append(str(market.id))
            from types import SimpleNamespace

            return SimpleNamespace(entry_price=0.5, stake_amount=3.0)

        with get_session() as s:
            # bugun acilmis 1 metar beti zaten var (status bagimsiz sayilir)
            s.add(
                Bet(
                    market_id=m_high,
                    order_id="metar_prev_1",
                    status="placed",
                    placed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    city="Paris",
                    stake_amount=3.0,
                    entry_price=0.3,
                )
            )
            s.commit()

        bot_config.strategy.metar_peak_max_bets_per_day = 1
        try:
            with (
                patch(
                    "scrapers.metar.fetch_metar_day",
                    return_value=[(int(_time.time()) - 120, 24.0), (int(_time.time()) - 60, 24.0)],
                ),
                patch("scrapers.metar.archive_metar_observations", return_value=0),
                patch.object(mp, "_avg_peak_hour", return_value=0.0),  # saat gelmis
                patch.object(mp, "_open_metar_bet", side_effect=_fake_bet),
                patch.object(mp, "_close_wrong_bucket_bets", return_value=0),
            ):
                mp.run_metar_peak_bets()
        finally:
            bot_config.strategy.metar_peak_max_bets_per_day = 12
        assert called_ids == [], f"cap zaten dolu -> yeni bet acilmamali: {called_ids}"


class TestPeakWatchOrdering:
    """2026-08-21 kullanici: "kilit takibi sehir isimlerini utc ye gore
    sirala, en doguda sehir en basta en batidaki en sonda" — peak_watch_list
    lon (boylam) DESC doner; lon'suz eski kayitlar sona gider."""

    def test_peak_watch_dogu_bati_sirali(self, tmp_path):
        import utils.activity_log as al

        db = tmp_path / "peakwatch_test.db"
        original = al._DB_PATH
        al._DB_PATH = str(db)
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            al.update_peak_watch(
                [
                    {
                        "city": "Los Angeles",
                        "cur": 33.0,
                        "prev": 33.0,
                        "direction": "FLAT",
                        "status": "kilitli peak=33.0",
                        "peak": 33.0,
                        "day": today,
                        "lon": -118.2,
                    },
                    {
                        "city": "Wellington",
                        "cur": 20.0,
                        "prev": 19.0,
                        "direction": "UP",
                        "status": "takip",
                        "peak": None,
                        "day": today,
                        "lon": 174.8,
                    },
                    {
                        "city": "London",
                        "cur": 24.0,
                        "prev": 23.0,
                        "direction": "UP",
                        "status": "erken giris",
                        "peak": 24.0,
                        "day": today,
                        "lon": -0.1,
                    },
                    # lon'suz eski kayit (r.get("lon") -> None) sona gider
                    {
                        "city": "EskiSehir",
                        "cur": 20.0,
                        "prev": None,
                        "direction": "-",
                        "status": "takip",
                        "peak": None,
                        "day": today,
                    },
                ]
            )
            cities = [r["city"] for r in al.peak_watch_list()]
        finally:
            al._DB_PATH = original
        assert cities == ["Wellington", "London", "Los Angeles", "EskiSehir"], cities


class TestCityLonFallback:
    """2026-08-21: peak_watch lon bos marketlerde _ICAO_COORDS fallback'i.

    Kullanici: "kilit takibi sehir isimlerini utc ye gore sirala" — market
    kaydinda longitude 0/None olan sehirler (bazilari weather_markets'ta
    bos kaliyor) _ICAO_COORDS (ICAO->koordinat) tablosundan doldurulur.
    """

    def test_lon_bos_ise_icao_fallback(self):
        from types import SimpleNamespace

        from config.settings import _ICAO_COORDS
        from jobs.metar_peak import _city_lon

        # longitude bos (None) -> ICAO tablosundan (London EGLL)
        m = SimpleNamespace(city_code="EGLL", longitude=None)
        assert _city_lon(m) == _ICAO_COORDS["EGLL"][1]

        # longitude 0.0 (0 == False) -> ICAO tablosundan
        m0 = SimpleNamespace(city_code="KLAX", longitude=0.0)
        assert _city_lon(m0) == _ICAO_COORDS["KLAX"][1]

        # longitude dolu -> market onceliklidir (ICAO'ya bakilmaz)
        m_dolu = SimpleNamespace(city_code="EGLL", longitude=-0.055)
        assert _city_lon(m_dolu) == -0.055

        # ICAO'da da yok -> None (siralamada en sona duser)
        m_yer = SimpleNamespace(city_code="XXXX", longitude=None)
        assert _city_lon(m_yer) is None
