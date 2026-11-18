"""HEAT deposu ayarlari.

Canli HEAT hatti (2026-10-27 dogrulamasi): Node runner
``Heat/windows-runner/index.mjs`` her dongude ``prediction_log``'a yazar.
D: diski arizali oldugu icin depo 2026-09-30'da C: altina tasindi:

* canli : ``C:\\HEAT data\\db\\tempmarket-edge.sqlite`` (3,34 GB, 72.663 tahmin)
* eski  : ``D:\\HEAT data\\db\\tempmarket-edge.sqlite`` (legacy sema, 0 tahmin)

2026-11-18: runner Junbo icine gomulu (``forecast_runner``); canli depo
``C:\\JUNBO data\\tempmarket-edge.sqlite`` (.env ``JUNBO_HEAT_DB``).

Yol import aninda sabitlenmez: testler ``JUNBO_HEAT_DB`` ortam degiskeni ile
kendi gecici DB'lerini gosterir. Sabit bir modul degiskeni kullanilsaydi,
ilk import'tan sonra yapilan monkeypatch etkisiz kalirdi.
"""

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

#: Canli depolar, oncelik sirasiyla. Ilk var olan kullanilir.
#: 2026-11-18: runner LIVE deposu C:\JUNBO data'ya tasindi (start-runner.ps1
#: TEMP_MARKET_DB); .env JUNBO_HEAT_DB yine onceliklidir, bu liste yedektir.
HEAT_DB_CANDIDATES = (
    r"C:\JUNBO data\tempmarket-edge.sqlite",
    r"C:\HEAT data\db\tempmarket-edge.sqlite",
    r"D:\HEAT data\db\tempmarket-edge.sqlite",
)

DEFAULT_HEAT_DB = HEAT_DB_CANDIDATES[0]

#: Junbo'nun weather_forecasts kaynagi olarak kullandigi etiket.
SOURCE = "heat_mos"

#: Gunluk calisma isareti (restart'ta job tekrar kosmasin).
MARKER = str(_ROOT / "data" / ".last_heat_mos_run")

#: prediction_log'dan kac gun geriye okunacagi (hedef tarih penceresi).
LOOKBACK_DAYS = 1

#: En son kosunun kac saatten eski olmasi "hat olu" sayilir (uyari esigi).
MAX_RUN_AGE_HOURS = 36


def heat_db_path() -> str:
    """HEAT deposunun yolu.

    Sirasiyla: ``JUNBO_HEAT_DB`` ortam degiskeni -> var olan ilk aday ->
    varsayilan aday (dosya yoksa da yol dondurulur, cagiran uyarir).
    """
    override = os.environ.get("JUNBO_HEAT_DB")
    if override:
        return override
    for candidate in HEAT_DB_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return DEFAULT_HEAT_DB
