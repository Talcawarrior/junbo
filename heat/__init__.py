"""HEAT tahmin motoru - Junbo icinde, tek sema / tek surec (2026-10-27).

Gecmis: HEAT ayri bir projeydi ve tahminlerini
``D:\\HEAT data\\db\\tempmarket-edge.sqlite`` icindeki ``prediction_log``
tablosuna yaziyordu; Junbo da ``jobs/heat_mos_job.py`` ile bu tabloyu
okuyordu. Iki tarafin semasi zamanla ayristi:

* Junbo'nun okudugu kolonlar : ``run_at_utc``, ``final_prediction``
* HEAT DB'sindeki kolonlar   : ``created_at``, ``quantile_50``

Sonuc: okuma sorgusu hata verdi, job hatayi yuttu ve entegrasyon sessizce
tamamen durdu (2026-10-27 olcumu: Junbo DB'sinde ``heat_mos`` tahmini 0
satir, HEAT ``prediction_log`` 0 satir).

Kullanici karari (2026-10-27): TEK SEMA, TEK SUREC. Sema tanimi artik bu
pakette yasar ve Junbo kendi surecinde uygular. Boylece "hangi DB'de hangi
kolon var" sorusu ortadan kalkar.
"""

from heat.config import (
    DEFAULT_HEAT_DB,
    LOOKBACK_DAYS,
    MARKER,
    MAX_RUN_AGE_HOURS,
    SOURCE,
    heat_db_path,
)
from heat.predictions import fetch_live_predictions, newest_run_age_hours
from heat.schema import ensure_schema

__all__ = [
    "DEFAULT_HEAT_DB",
    "LOOKBACK_DAYS",
    "MARKER",
    "MAX_RUN_AGE_HOURS",
    "SOURCE",
    "ensure_schema",
    "fetch_live_predictions",
    "heat_db_path",
    "newest_run_age_hours",
]
