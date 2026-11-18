"""Replay-CANLI bet eslestirme metrikleri icin birim testler.

`replay.fidelity.compare_replay_vs_live` saf bir fonksiyondur (DB'siz) —
bu testler sentetik bet satirlariyla eslesme kurallarini dogrular:
  market_id esit + |placed_at| <= 15 dk + |entry| <= 0.05.

Turkish ASCII kurali: c g i o s u.
"""

from __future__ import annotations

from datetime import datetime

from replay.fidelity import compare_replay_vs_live, strategy_of


def _b(mid: str, iso: str, entry: float, order_id: str = "spread_x", pnl=None) -> dict:
    return {
        "market_id": mid,
        "city_code": "EGLL",
        "side": "YES",
        "stake": 2.0,
        "entry": entry,
        "status": "won",
        "order_id": order_id,
        "placed_at": datetime.fromisoformat(iso).replace(tzinfo=None),
        "pnl": pnl,
        "strike_temp": 30.0,
    }


def test_tam_eslesme_precision_recall_100():
    """Canli ve replay birebir ayni betler -> precision=recall=1."""
    live = [_b("1", "2026-08-20T10:00:00", 0.40), _b("2", "2026-08-20T11:00:00", 0.55)]
    replay = [_b("1", "2026-08-20T10:04:00", 0.41), _b("2", "2026-08-20T10:58:00", 0.54)]
    res = compare_replay_vs_live(replay, live, window_days=3)
    assert res["precision"] == 1.0
    assert res["recall"] == 1.0
    assert res["matched"] == 2


def test_market_id_uyusmazligi_eslesmez():
    live = [_b("1", "2026-08-20T10:00:00", 0.40)]
    replay = [_b("2", "2026-08-20T10:04:00", 0.40)]  # farkli market
    res = compare_replay_vs_live(replay, live)
    assert res["precision"] == 0.0
    assert res["recall"] == 0.0
    assert res["matched"] == 0


def test_zaman_penceresi_15dk():
    live = [_b("1", "2026-08-20T10:00:00", 0.40)]
    far = [_b("1", "2026-08-20T10:20:00", 0.40)]  # 20 dk fark -> eslesmez
    near = [_b("1", "2026-08-20T10:10:00", 0.40)]  # 10 dk fark -> eslesir
    assert compare_replay_vs_live(far, live)["matched"] == 0
    assert compare_replay_vs_live(near, live)["matched"] == 1


def test_entry_toleransi_005():
    live = [_b("1", "2026-08-20T10:00:00", 0.40)]
    ok = [_b("1", "2026-08-20T10:05:00", 0.45)]  # 0.05 fark -> sinirda, eslesir
    bad = [_b("1", "2026-08-20T10:05:00", 0.46)]  # 0.06 fark -> eslesmez
    assert compare_replay_vs_live(ok, live)["matched"] == 1
    assert compare_replay_vs_live(bad, live)["matched"] == 0


def test_ayni_canli_iki_replayle_eslesmez():
    """Bir canli bet yalnizca BIR replay betiyle eslenir (recall dogrulugu)."""
    live = [_b("1", "2026-08-20T10:00:00", 0.40)]
    replay = [
        _b("1", "2026-08-20T10:01:00", 0.40),
        _b("1", "2026-08-20T10:02:00", 0.41),
    ]
    res = compare_replay_vs_live(replay, live)
    assert res["matched"] == 1
    assert res["precision"] == 0.5  # 2 replay, 1 eslesen
    assert res["recall"] == 1.0


def test_strateji_kirilimi():
    live = [
        _b("1", "2026-08-20T10:00:00", 0.40, order_id="spread_a"),
        _b("2", "2026-08-20T11:00:00", 0.60, order_id="metar_b"),
    ]
    replay = [
        _b("1", "2026-08-20T10:04:00", 0.41, order_id="spread_a"),
        _b("2", "2026-08-20T10:58:00", 0.61, order_id="metar_b"),
    ]
    res = compare_replay_vs_live(replay, live)
    assert res["strategy"]["spread"]["matched"] == 1
    assert res["strategy"]["metar_peak"]["matched"] == 1
    assert res["strategy"]["other"]["matched"] == 0


def test_gunluk_rapor_son_N_gun():
    """window_days=1 yalnizca son gunu kapsar."""
    live = [_b("1", "2026-08-18T10:00:00", 0.40), _b("2", "2026-08-20T10:00:00", 0.40)]
    replay = [_b("1", "2026-08-18T10:04:00", 0.40), _b("2", "2026-08-20T10:04:00", 0.40)]
    res = compare_replay_vs_live(replay, live, window_days=1)
    assert len(res["days"]) == 1
    assert res["days"][0]["day"] == "2026-08-20"


def test_strategy_of_onek():
    assert strategy_of("spread_abc") == "spread"
    assert strategy_of("metar_abc") == "metar_peak"
    assert strategy_of("xyz") == "other"
    assert strategy_of(None) == "other"
