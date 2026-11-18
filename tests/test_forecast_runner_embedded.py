"""Smoke tests for the embedded forecast runner (junbo/forecast_runner)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "forecast_runner"


def test_runner_entrypoint_and_config_exist():
    assert (RUNNER / "windows-runner" / "index.mjs").is_file()
    assert (RUNNER / "windows-runner" / "config.json").is_file()


def test_runner_config_has_stations():
    config = json.loads((RUNNER / "windows-runner" / "config.json").read_text(encoding="utf-8"))
    stations = config.get("stations") or []
    assert len(stations) > 0


def test_launcher_points_to_consolidated_data_root():
    text = (RUNNER / "windows-runner" / "start-runner.ps1").read_text(encoding="utf-8")
    assert "C:\\JUNBO data" in text
    assert "tempmarket-edge.sqlite" in text
    assert "TEMP_MARKET_DATA_DIR" in text
    assert "HEAT_MAIN_DB" in text
