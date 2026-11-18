"""Replay motoru uc-uca smoke testi.

`replay.main.main`'i kisa bir pencerede calistirir (arsiv verisine karsi,
botun GERCEK kodunu): temp-DB takasi + ReplayClock + mock'lar + scheduler.
Dogrular: donus kodu 0, rapor dosyalari uretildi, bet/portfolio tutarligini
kiran istisna yok.

PRODUCTION DB GUVENLIGI: test kendi temp DB'sini kurar (setup_temp_db);
teardown'da DB_PATH onceki degerine geri cekilir + database modulleri
yeniden init edilir (sonraki testlerin izolasyonu korunur).

Bu test arsiv verisine bagli oldugu icin ~1-2 dk surer; smoke grubundadir.

Turkish ASCII kurali: c g i o s u.
"""

from __future__ import annotations

import os
import sys

import pytest

REPLAY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "replay")


def _restore_db(prev_path: str) -> None:
    import config.settings as cfg_mod

    cfg_mod.config.DB_PATH = prev_path
    sys.modules.pop("database.db", None)
    sys.modules.pop("database.models", None)
    from database.db import init_db

    init_db()
    # Replay temp-DB takasi sonrasi strateji modullerinin modul-seviyesi
    # get_session'i replay temp engine'ine bagli kalmisti; sonraki testlerin
    # bet_placer'i "0 open market" goruyordu. Guncel engine'e geri bagla.
    from replay.seeding import rebind_get_session

    rebind_get_session()


def test_replay_engine_smoke(tmp_path):
    """Kisa pencere (1 gun, 30dk tick) uc-uca calisir ve rapor uretir."""
    import config.settings as cfg_mod

    prev_db = cfg_mod.config.DB_PATH
    out_dir = str(tmp_path / "replay_out")

    try:
        from replay import main as replay_main

        rc = replay_main.main(
            [
                "--window",
                "2026-08-20..2026-08-20",
                "--strategy",
                "both",
                "--tick-minutes",
                "30",
                "--stake-mode",
                "fixed",
                "--out-dir",
                out_dir,
            ]
        )
    finally:
        _restore_db(prev_db)

    assert rc == 0, f"replay.main cikis kodu {rc} olmali"

    # main.py her stake modunu <out_dir>/<mode>/ altina yazar (both -> fixed + compound)
    mode_dir = os.path.join(out_dir, "fixed")
    rapor = os.path.join(mode_dir, "rapor.md")
    bets_csv = os.path.join(mode_dir, "ledger_bets.csv")
    trace_csv = os.path.join(mode_dir, "mechanism_trace.csv")
    assert os.path.exists(rapor), "rapor.md uretilmeli"
    assert os.path.exists(bets_csv), "ledger_bets.csv uretilmeli"
    assert os.path.exists(trace_csv), "mechanism_trace.csv uretilmeli"
    with open(rapor, encoding="utf-8") as f:
        body = f.read()
    assert "Gunluk Ozet" in body
    assert "2026-08-20" in body


def test_replay_window_parse():
    """CLI pencere ayristirmasi: '2026-08-04..2026-08-22' -> (start, end+1)."""
    from replay.main import _parse_window

    start, end = _parse_window("2026-08-04..2026-08-22")
    assert start.isoformat() == "2026-08-04T00:00:00"
    assert end.isoformat() == "2026-08-23T00:00:00"  # end + 1 gun (kapsayici)


def test_compound_stake_override():
    """Compound modda stake bankroll*0.03'e ezilir, bot kodu korunur."""
    from replay.main import _apply_stake_mode

    import jobs.metar_peak as mp

    prev_metar_stake = mp.METAR_STAKE
    prev_spread_stake = None
    try:
        from config.settings import bot_config

        prev_spread_stake = bot_config.strategy.spread_stake_usd
        _apply_stake_mode("compound", 1000.0)
        assert bot_config.strategy.spread_stake_usd == pytest.approx(30.0)
        assert mp.METAR_STAKE == pytest.approx(30.0)
    finally:
        if prev_spread_stake is not None:
            from config.settings import bot_config

            bot_config.strategy.spread_stake_usd = prev_spread_stake
        mp.METAR_STAKE = prev_metar_stake
