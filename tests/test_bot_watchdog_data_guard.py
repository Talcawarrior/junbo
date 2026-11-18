"""scripts/bot_watchdog.py ensure_data_watchdog -- yanlis 'stale' alarmi (2026-11-18).

Olay: 17:59'daki data-watchdog tick'i collect_orderbook yuzunden 19 dk log
yazmadi; bot_watchdog bunu 'stale' sanip 4 kez enable+run yapti. Artik gorev
Running iken 40 dk'ya kadar alarm yok; takilmis gorev (>40 dk) end+run edilir.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bot_watchdog.py"


@pytest.fixture
def bw(tmp_path, monkeypatch):
    name = "bot_watchdog_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    data_log = tmp_path / "data_watchdog.log"
    data_log.write_text("tick", encoding="utf-8")
    monkeypatch.setattr(mod, "DATA_WATCHDOG_LOG", str(data_log))
    monkeypatch.setattr(mod, "LOG", str(tmp_path / "watchdog.log"))
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run", lambda cmd: calls.append(cmd) or 0)
    yield mod, data_log, calls
    sys.modules.pop(name, None)


def _set_age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_fresh_log_no_action(bw, monkeypatch):
    mod, data_log, calls = bw
    _set_age(data_log, 60)
    monkeypatch.setattr(mod, "_task_state", lambda name: pytest.fail("taze logda gorev sorgulanmamali"))
    mod.ensure_data_watchdog()
    assert calls == []


def test_long_running_tick_is_not_alarmed(bw, monkeypatch):
    mod, data_log, calls = bw
    _set_age(data_log, 20 * 60)
    monkeypatch.setattr(mod, "_task_state", lambda name: "Running")
    mod.ensure_data_watchdog()
    assert calls == []


def test_hung_tick_is_ended_and_rerun(bw, monkeypatch):
    mod, data_log, calls = bw
    _set_age(data_log, 50 * 60)
    monkeypatch.setattr(mod, "_task_state", lambda name: "Running")
    mod.ensure_data_watchdog()
    assert [c[1] for c in calls] == ["/end", "/run"]


def test_stopped_watchdog_is_enabled_and_run(bw, monkeypatch):
    mod, data_log, calls = bw
    _set_age(data_log, 20 * 60)
    monkeypatch.setattr(mod, "_task_state", lambda name: "Ready")
    mod.ensure_data_watchdog()
    assert [c[1] for c in calls] == ["/change", "/run"]
    assert "/enable" in calls[0]
