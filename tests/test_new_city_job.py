"""Yeni sehir oto-kesif testleri (2026-09-13)."""

import json


def test_overrides_merge(tmp_path, monkeypatch):
    import config.settings as st

    ov = tmp_path / "city_overrides.json"
    ov.write_text(json.dumps({"aliases": {"newtown": "KXYZ"}, "coords": {"KXYZ": [10.0, 20.0]}}), encoding="utf-8")
    monkeypatch.setattr(st, "CITY_OVERRIDES_PATH", str(ov))
    m = st.get_live_city_map()
    assert m["newtown"] == "KXYZ"
    assert m["istanbul"] == "LTFM"  # baz harita korunur
    c = st.get_live_icao_coords()
    assert c["KXYZ"] == (10.0, 20.0)


def test_overrides_missing_file(monkeypatch):
    import config.settings as st

    monkeypatch.setattr(st, "CITY_OVERRIDES_PATH", "/yok/boyle/bir/dosya.json")
    m = st.get_live_city_map()
    assert m["istanbul"] == "LTFM"


def test_city_token_from_title():
    from jobs.new_city_job import _city_token_from_title

    assert _city_token_from_title("Highest temperature in Austin on September 14?") == "Austin"
    assert _city_token_from_title("Will it rain in New York tomorrow?") != ""


def test_haversine():
    from jobs.new_city_job import _haversine_km

    d = _haversine_km(41.0, 29.0, 41.1, 29.1)
    assert 10 < d < 20


def test_geocode_mock(monkeypatch):
    import jobs.new_city_job as nj

    class R:
        status_code = 200

        def json(self):
            return {"results": [{"name": "Austin", "country": "USA", "latitude": 30.27, "longitude": -97.74}]}

    monkeypatch.setattr(nj.requests, "get", lambda *a, **k: R())
    g = nj.geocode_city("Austin")
    assert g["lat"] == 30.27


def test_spread_fallback_new_city(market_factory):
    """Bias'i olmayan sehir global ortalama ile KABUL edilir (atlamaz)."""
    from database.db import get_session
    from database.models import HistoricalCalibration, WeatherForecast
    from datetime import datetime, timezone

    tgt = datetime.now(timezone.utc).replace(tzinfo=None)
    market_factory(
        city="Istanbul",
        city_code="LTFM",
        metric="temperature_max",
        market_type="RANGE",
        threshold=25.0,
        target_date=tgt,
        status="open",
        yes_price=0.3,
    )
    market_factory(
        city="Newtown",
        city_code="KXYZ",
        metric="temperature_max",
        market_type="RANGE",
        threshold=25.0,
        target_date=tgt,
        status="open",
        yes_price=0.3,
    )
    with get_session() as s:
        _d = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(
            HistoricalCalibration(
                city="Istanbul",
                city_code="LTFM",
                date=_d,
                metric="temperature_max",
                model="icon_global",
                predicted_value=28.0,
                actual_value=27.0,
                bias=1.0,
            )
        )
        s.add(
            HistoricalCalibration(
                city="Istanbul",
                city_code="LTFM",
                date=_d,
                metric="temperature_max",
                model="icon_global",
                predicted_value=30.0,
                actual_value=27.0,
                bias=3.0,
            )
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(
            WeatherForecast(
                city="LTFM",
                metric="temperature_max",
                source="icon_global",
                predicted_value=27.0,
                target_date=tgt,
                fetched_at=now,
            )
        )
        s.add(
            WeatherForecast(
                city="KXYZ",
                metric="temperature_max",
                source="icon_global",
                predicted_value=27.0,
                target_date=tgt,
                fetched_at=now,
            )
        )
        s.commit()
        from executor.spread_placer import _city_accuracy, _last_forecast_per_city_metric

        acc = _city_accuracy(s)
        assert "LTFM" in acc and "KXYZ" not in acc
        fc = _last_forecast_per_city_metric(s, tgt.date())
        assert ("LTFM", "temperature_max") in fc
        assert ("KXYZ", "temperature_max") in fc  # fallback ile dahil
