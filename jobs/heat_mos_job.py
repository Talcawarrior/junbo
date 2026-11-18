"""Heat MOS canli tahminlerini junbo kaynagi yap (2026-09-18).

2026-10-27 (S15): HEAT depo semasi artik ``heat`` paketinde tanimli
(tek sema). Bu job eskiden kolon adlarini ve DB yolunu sabit kodluyordu;
HEAT tarafinda kolonlar ``created_at``/``quantile_50`` iken burada
``run_at_utc``/``final_prediction`` bekleniyordu. Sorgu hata verince
istisna yutuluyor ve entegrasyon sessizce duruyordu (Junbo DB'sinde
``heat_mos`` tahmini 0 satir). Artik:

* ``heat.ensure_schema`` her kosuda depoyu kanonik semaya getirir,
* ``heat.fetch_live_predictions`` her iki kolon varyantini okur,
* bos sonuc sessiz kalmaz, WARNING olarak loglanir.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from heat import LOOKBACK_DAYS, MARKER, SOURCE, ensure_schema
from heat import fetch_live_predictions as _fetch_live_predictions
from heat.config import heat_db_path

logger = logging.getLogger("HEAT_MOS_JOB")

_MARKER = MARKER


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def should_run() -> bool:
    try:
        with open(_MARKER, encoding="utf-8") as fh:
            return fh.read().strip() != _today()
    except Exception:
        return True


def _write_marker() -> None:
    try:
        os.makedirs(os.path.dirname(_MARKER), exist_ok=True)
        with open(_MARKER, "w", encoding="utf-8") as fh:
            fh.write(_today())
    except Exception as exc:
        logger.warning("heat_mos marker yazilamadi: %s", exc)


def fetch_live_predictions() -> list[dict]:
    """HEAT prediction_log: her istasyon+hedef icin SON kosu."""
    return _fetch_live_predictions()


def run() -> dict:
    """Tahminleri weather_forecasts'a yaz (idempotent)."""
    from database.db import get_session
    from database.models import WeatherForecast

    schema = ensure_schema()
    if schema.get("error"):
        logger.warning("heat_mos: HEAT deposu hazir degil (%s)", schema["error"])

    preds = fetch_live_predictions()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    added = skipped = 0
    with get_session() as session:
        for p in preds:
            try:
                tgt = datetime.fromisoformat(p["target"])
            except Exception:
                continue
            exists = (
                session.query(WeatherForecast)
                .filter(
                    WeatherForecast.city == p["icao"],
                    WeatherForecast.source == SOURCE,
                    WeatherForecast.target_date >= tgt.replace(hour=0, minute=0, second=0),
                    WeatherForecast.target_date < tgt.replace(hour=0, minute=0, second=0) + timedelta(days=1),
                    WeatherForecast.metric == "temperature_max",
                )
                .first()
            )
            if exists:
                skipped += 1
                continue
            session.add(
                WeatherForecast(
                    city=p["icao"],
                    lat=p.get("lat"),
                    lon=p.get("lon"),
                    target_date=tgt,
                    metric="temperature_max",
                    source=SOURCE,
                    predicted_value=p["pred"],
                    fetched_at=now,
                    raw_data=f"heat_mos run={p['run']}",
                )
            )
            added += 1

    if not preds:
        # Sessiz kalma: bu kaynak olmadan bot yalnizca Open-Meteo ile karar verir.
        logger.warning(
            "heat_mos: HEAT deposunda tahmin yok (%s, pencere=%d gun). weather_forecasts'a hicbir satir yazilmadi.",
            heat_db_path(),
            LOOKBACK_DAYS,
        )
    else:
        logger.info("heat_mos: fetched=%d added=%d skipped=%d", len(preds), added, skipped)
    return {"fetched": len(preds), "added": added, "skipped": skipped}


def maybe_run() -> dict | None:
    if not should_run():
        return None
    try:
        out = run()
    except Exception as exc:
        logger.error("heat_mos job hatasi: %s", exc, exc_info=True)
        return {"error": str(exc)[:200]}
    _write_marker()
    return out


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import json

    print(json.dumps(maybe_run(), indent=2, ensure_ascii=False))
