"""HEAT ``prediction_log`` okuma katmani.

Kanonik sema ``heat.schema`` icinde tanimlidir. Burada yine de kolon
varligina gore okuma yapilir: sema guncellenmemis bir depo (ornegin yeni
restore edilmis bir yedek) okundugunda job'in patlamak yerine anlamli bir
uyari verip bos liste donmesi gerekir.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

from heat.config import LOOKBACK_DAYS, MAX_RUN_AGE_HOURS, heat_db_path

logger = logging.getLogger("HEAT_PREDICTIONS")

#: Kanonik kolon -> eski depodaki karsiligi (geriye uyum sirasi).
_PRED_CANDIDATES = ("final_prediction", "quantile_50")
_RUN_CANDIDATES = ("run_at_utc", "created_at")


def _parse_run(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def newest_run_age_hours(preds: list[dict]) -> float | None:
    """Tahminlerin en yeni kosusunun kac saat once oldugu (yoksa None)."""
    stamps = [d for d in (_parse_run(p.get("run", "")) for p in preds) if d is not None]
    if not stamps:
        return None
    return (datetime.now(timezone.utc) - max(stamps)).total_seconds() / 3600.0


def _columns(con: sqlite3.Connection, table: str) -> dict[str, str]:
    try:
        cur = con.execute(f"PRAGMA table_info({table})")
    except sqlite3.DatabaseError:
        return {}
    return {str(row[1]): str(row[2] or "") for row in cur.fetchall()}


def _pick(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in columns:
            return name
    return None


def fetch_live_predictions(db_path: str | None = None, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Her istasyon + hedef tarih icin EN SON kosunun tahmini.

    Donen her kayit: ``station``, ``target``, ``run``, ``pred``, ``icao``,
    ``city``, ``lat``, ``lon``.
    """
    path = db_path or heat_db_path()
    if not os.path.exists(path):
        logger.warning("HEAT DB bulunamadi: %s", path)
        return []

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        logger.warning("HEAT DB acilamadi (%s): %s", path, exc)
        return []

    try:
        pred_cols = _columns(con, "prediction_log")
        if not pred_cols:
            logger.warning("HEAT prediction_log tablosu yok/okunamadi: %s", path)
            return []
        pred_col = _pick(pred_cols, _PRED_CANDIDATES)
        if pred_col is None:
            logger.warning(
                "HEAT prediction_log'da tahmin kolonu yok (beklenen: %s): %s",
                "/".join(_PRED_CANDIDATES),
                path,
            )
            return []
        run_col = _pick(pred_cols, _RUN_CANDIDATES)
        order = f"{run_col} DESC" if run_col else "rowid DESC"

        cur = con.cursor()
        cur.execute(
            f"SELECT station_id, target_date, "
            f"{run_col if run_col else 'NULL'}, {pred_col} "
            f"FROM prediction_log "
            f"WHERE {pred_col} IS NOT NULL AND target_date >= date('now', ?) "
            f"ORDER BY {order}",
            (f"-{int(lookback_days)} day",),
        )
        seen: dict[tuple[str, str], dict] = {}
        for sid, tgt, run, pred in cur.fetchall():
            try:
                key = (str(sid).lower(), str(tgt)[:10])
                if key in seen:
                    continue
                seen[key] = {
                    "station": str(sid).lower(),
                    "target": str(tgt)[:10],
                    "run": str(run) if run is not None else "",
                    "pred": float(pred),
                }
            except (TypeError, ValueError):
                continue

        meta: dict[str, dict] = {}
        if _columns(con, "stations"):
            cur.execute("SELECT id, icao, city, latitude, longitude FROM stations")
            for sid, icao, city, lat, lon in cur.fetchall():
                if sid and icao:
                    meta[str(sid).lower()] = {
                        "icao": str(icao).upper(),
                        "city": city or str(icao).upper(),
                        "lat": lat,
                        "lon": lon,
                    }
    finally:
        con.close()

    out = []
    for (sid, _), row in seen.items():
        m = meta.get(sid)
        if not m:
            continue
        row.update(m)
        out.append(row)

    if not out:
        logger.warning(
            "HEAT prediction_log bos (pred_col=%s, pencere=%s gun): %s",
            pred_col,
            lookback_days,
            path,
        )
    else:
        age = newest_run_age_hours(out)
        if age is not None and age > MAX_RUN_AGE_HOURS:
            # Hat sessizce olurse tahminler eskir; bot eski veriyle karar verir.
            logger.warning(
                "HEAT tahmin hatti bayat: en son kosu %.1f saat once (esik=%d saat, %s)",
                age,
                MAX_RUN_AGE_HOURS,
                path,
            )
    return out
