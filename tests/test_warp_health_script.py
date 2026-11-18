"""scripts/warp_health.ps1 -- WARP proxy saglik kontrolu (2026-11-18).

Bot ve forecast runner WARP'i PROXY modunda (socks5 127.0.0.1:40000) kullanir.
Eski start_warp.ps1 `warp-cli mode warp` (tam VPN) calistiriyordu; bu SOCKS
portunu kapatir. Yeni script modu ASLA degistirmemeli, yalnizca yoklayip
gerekirse disconnect/connect yapmali.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "warp_health.ps1"


def _code_lines() -> list[str]:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def test_never_switches_warp_mode():
    offenders = [ln for ln in _code_lines() if re.search(r"(\$cli|warp-cli(\.exe)?)\s+mode\b", ln, re.IGNORECASE)]
    assert offenders == []


def test_probes_proxy_and_reconnects_with_cooldown():
    code = "\n".join(_code_lines())
    assert "cdn-cgi/trace" in code and "socks5h://127.0.0.1:40000" in code
    assert re.search(r"\$cli disconnect", code) and re.search(r"\$cli connect", code)
    assert "$cooldownMin" in code and "$failThreshold" in code


@pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell yok")
def test_script_parses_without_errors():
    path = str(SCRIPT).replace("'", "''")
    cmd = (
        "$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$null, [ref]$e) | Out-Null; $e.Count"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=60
    )
    assert out.stdout.strip() == "0", out.stdout + out.stderr
