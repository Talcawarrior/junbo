"""Polymarket METAR cozum kurali testleri (2026-11-22).

Kural (23 Eki 2026'dan itibaren METAR sehirleri; market description):
  - yalnizca ROUTINE gozlemler; SPECI sayilmaz
  - deger: RMK T grubu (onda bir C), yoksa govdedeki tam sayi
  - yuvarlama: floor(x + 0.5) -> 13.9->14, 17.4->17, 16.5->17, -1.7->-2, -8.5->-8
"""

from unittest.mock import MagicMock, patch

import pytest

from scrapers import metar
from scrapers.metar import _is_speci, _parse_metar_temp, polymarket_round


class TestPolymarketRound:
    @pytest.mark.parametrize(
        "value,expected",
        [
            # Polymarket market description'indaki ornekler (birebir)
            (13.9, 14),
            (17.4, 17),
            (16.5, 17),
            (-1.7, -2),
            (-8.5, -8),
            # eski int(x+0.5) ve simetrik yuvarlamanin bozuldugu degerler
            (-1.0, -1),
            (-6.0, -6),
            (-0.5, 0),
            (0.0, 0),
            (35.9, 36),
        ],
    )
    def test_kurala_uyar(self, value, expected):
        assert polymarket_round(value) == expected


class TestParseMetarTemp:
    def test_t_grubu_onda_bir(self):
        raw = "METAR KATL 221752Z 01004KT 6SM BR OVC008 16/14 A2997 RMK AO2 SLP145 T01560139 10156 20128 58012 $"
        assert _parse_metar_temp(raw) == pytest.approx(15.6)

    def test_t_grubu_negatif(self):
        raw = "METAR KORD 141051Z 27008KT 10SM CLR M01/M06 A3012 RMK AO2 SLP210 T10111061"
        assert _parse_metar_temp(raw) == pytest.approx(-1.1)

    def test_t_grubu_yoksa_govde(self):
        raw = "METAR LFPB 241000Z 22010KT CAVOK 13/07 Q1018 NOSIG"
        assert _parse_metar_temp(raw) == 13.0

    def test_govde_negatif_m(self):
        raw = "METAR UUWW 190600Z 00000MPS CAVOK M11/M17 Q1030 NOSIG"
        assert _parse_metar_temp(raw) == -11.0

    def test_sicaklik_yok(self):
        assert _parse_metar_temp("METAR XXXX 190600Z AUTO NIL") is None
        assert _parse_metar_temp("") is None

    def test_dewpoint_dikkate_alinmaz(self):
        # T grubu: ilk 4 hane sicaklik, son 4 hane ciy noktasi
        raw = "METAR KLGA 121951Z 18010KT 10SM FEW250 17/04 A3001 RMK AO2 T01610044"
        assert _parse_metar_temp(raw) == pytest.approx(16.1)


class TestIsSpeci:
    def test_metar_type_alani(self):
        assert _is_speci({"metarType": "SPECI", "rawOb": "SPECI KATL ..."})
        assert not _is_speci({"metarType": "METAR", "rawOb": "METAR KATL ..."})

    def test_raw_on_eki(self):
        assert _is_speci({"rawOb": "SPECI KATL 221731Z 05005KT"})
        # etiketsiz rapor rutin sayilir (Polymarket: "carries no METAR or SPECI label")
        assert not _is_speci({"rawOb": "KATL 221752Z 01004KT"})


class TestFetchMetarFiltersSpeci:
    """_fetch_metar SPECI'yi atar ve T grubundaki onda bir degeri kullanir.
    Gercek KATL 22 Kas 2026 ornegi (aviationweather JSON)."""

    def _resp(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        r.raise_for_status.return_value = None
        return r

    def test_speci_dislanir_t_grubu_kullanilir(self):
        payload = [
            {
                "obsTime": 1795369920,
                "temp": 15.6,
                "metarType": "METAR",
                "rawOb": "METAR KATL 221752Z 01004KT 6SM BR OVC008 16/14 A2997 RMK AO2 T01560139 $",
            },
            {
                # SPECI daha yuksek olsa bile sayilmaz
                "obsTime": 1795368660,
                "temp": 17.0,
                "metarType": "SPECI",
                "rawOb": "SPECI KATL 221731Z 05005KT 6SM BR OVC008 17/14 A2997 RMK AO2 T01700139 $",
            },
            {
                "obsTime": 1795366320,
                "temp": 15,
                "metarType": "METAR",
                "rawOb": "METAR KATL 221652Z 04005KT 2SM BR OVC008 15/13 A2999 RMK AO2 T01500133 $",
            },
        ]
        with patch.object(metar._SESSION, "get", return_value=self._resp(payload)):
            rows = metar._fetch_metar("KATL", hours=3)
        assert rows == [(1795366320, 15.0), (1795369920, 15.6)]
        assert max(t for _, t in rows) == pytest.approx(15.6)

    def test_raw_yoksa_json_temp_yedek(self):
        payload = [{"obsTime": 1795369920, "temp": 12.0, "metarType": "METAR", "rawOb": ""}]
        with patch.object(metar._SESSION, "get", return_value=self._resp(payload)):
            rows = metar._fetch_metar("XXXX", hours=1)
        assert rows == [(1795369920, 12.0)]


class TestPanamaStation:
    def test_panama_mpmg(self):
        """Polymarket Panama marketleri MPMG (Marcos A. Gelabert) ile cozulur."""
        from config.settings import _CITY_ICAO_MAP, _ICAO_COORDS

        assert _CITY_ICAO_MAP["panama city"] == "MPMG"
        assert "MPMG" in _ICAO_COORDS
