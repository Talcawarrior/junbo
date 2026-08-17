"""METAR-kaynakli calibration testleri (2026-08-18).

Kullanici karari: "bias Open-Meteo'dan yanlis hesaplaniyorsa METAR/WU'dan
alalim". Polymarket weather marketleri WU (NOAA METAR) verisiyle cozer;
round(METAR max) == kazanan bucket %74 (test_metar_vs_settlement) vs Open-Meteo
Archive %30. `scripts/backfill_calibration.py --source metar` bias referansini
METAR istasyonu yapar. Bu test `_load_metar_actuals` cekirdek mantigini dogrular.
"""

import sys

sys.path.insert(0, r"C:\Users\fdemir\Documents\New project\junbo")

from datetime import datetime, timezone

from scripts.backfill_calibration import _load_metar_actuals


class TestLoadMetarActuals:
    def test_past_day_max_min_eklenir_bugun_atlanir(self):
        from database.db import get_session
        from database.models import MetarObservation

        with get_session() as s:
            # gecmis gun: tam gun verisi
            for h, t in [(0, 15.0), (12, 27.5), (13, 27.8), (18, 20.0)]:
                s.add(
                    MetarObservation(
                        city_code="EGLL",
                        city="London",
                        temp_c=t,
                        obs_time=datetime(2026, 8, 16, h, 0, 0, tzinfo=timezone.utc),
                        day="2026-08-16",
                    )
                )
            # bugun: kismi gun -> bias'ta kullanilmamali
            today = datetime.now().strftime("%Y-%m-%d")
            s.add(
                MetarObservation(
                    city_code="EGLL",
                    city="London",
                    temp_c=18.0,
                    obs_time=datetime.now(timezone.utc),
                    day=today,
                )
            )
            s.commit()

        with get_session() as s:
            actuals = _load_metar_actuals(s)

        assert ("EGLL", "2026-08-16", "temperature_max") in actuals
        assert ("EGLL", "2026-08-16", "temperature_min") in actuals
        assert actuals[("EGLL", "2026-08-16", "temperature_max")] == 27.8
        assert actuals[("EGLL", "2026-08-16", "temperature_min")] == 15.0
        # bugunun kismi gunu eklenmez (tam veri yarin gelir)
        assert ("EGLL", today, "temperature_max") not in actuals

    def test_metar_yoksa_anahtar_uretilmez(self):
        from database.db import get_session
        from database.models import MetarObservation

        with get_session() as s:
            s.query(MetarObservation).delete()
            s.commit()
        with get_session() as s:
            actuals = _load_metar_actuals(s)
        assert actuals == {}
