"""scripts/warp_health.ps1 -- WARP proxy saglik kontrolu (2026-11-18; gamma-api yoklamasi 2026-11-26).

Bot ve forecast runner WARP'i PROXY modunda (socks5 127.0.0.1:40000) kullanir.
Eski start_warp.ps1 `warp-cli mode warp` (tam VPN) calistiriyordu; bu SOCKS
portunu kapatir. Yeni script modu ASLA degistirmemeli, yalnizca yoklayip
gerekirse disconnect/connect yapmali.

2026-11-25: WARP Cloudflare disi hedeflere ulasamazken ("Host unreachable")
cdn-cgi/trace yoklamasi gecmeye devam etti; bot gamma-api'ye ulasamadi ve
reconnect olmadi. Ikinci yoklama gamma-api'yi (CloudFront) DoH adresiyle yoklar.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import threading
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


# -- gamma-api yoklamasi (2026-11-26) ---------------------------------------------------------------

needs_ps_curl = pytest.mark.skipif(
    shutil.which("powershell") is None or shutil.which("curl.exe") is None, reason="powershell/curl.exe yok"
)
_LOADED_VARS = "'proxy','proxyLocalDns','probeUrl','gammaHost','gammaUrl','dohUrl'"


def _run_probe(call: str, setup: str = "") -> str:
    """Script'in fonksiyonlarini ve proxy/URL degiskenlerini AST'den yukleyip `call`'i calistirir.

    Ust duzey akis (yoklama, reconnect, log/state yazimi) CALISMAZ; yalnizca tanimlar yuklenir.
    `setup` degisken/fonksiyon ezmek icindir.
    """
    path = str(SCRIPT).replace("'", "''")
    parts = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$null)",
        "foreach ($s in $ast.EndBlock.Statements) { "
        "if ($s -is [System.Management.Automation.Language.FunctionDefinitionAst] -or "
        "($s -is [System.Management.Automation.Language.AssignmentStatementAst] -and "
        f"$s.Left.VariablePath.UserPath -in @({_LOADED_VARS}))) "
        "{ . ([scriptblock]::Create($s.Extent.Text)) } }",
    ]
    if setup:
        parts.append(setup)
    parts.append(f"'RESULT=' + ({call})")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(parts)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    m = re.search(r"^RESULT=(.*)$", out.stdout, re.MULTILINE)
    assert m, out.stdout + out.stderr
    return m.group(1).strip()


class _UnreachableSocks5:
    """Her CONNECT'e REP=0x04 "Host unreachable" donen sahte SOCKS5 proxy.

    2026-11-25 arizasinin aynisi: proxy ayakta, ama hedefe ulasamiyor.
    """

    def __init__(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.connects = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(5)
                    head = conn.recv(2)  # VER, NMETHODS
                    if len(head) < 2:
                        continue
                    conn.recv(head[1])  # METHODS
                    conn.sendall(b"\x05\x00")  # kimlik dogrulama yok
                    req = conn.recv(262)  # VER CMD RSV ATYP ADDR PORT
                    if len(req) >= 2 and req[1] == 1:  # CONNECT
                        self.connects += 1
                    conn.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)  # REP=0x04 host unreachable
                except OSError:
                    pass

    def close(self) -> None:
        self._srv.close()


@pytest.fixture
def unreachable_socks():
    srv = _UnreachableSocks5()
    yield srv
    srv.close()


def test_gamma_probe_pins_doh_address_over_local_dns_socks():
    """socks5h:// ile WARP adi makine hosts sabitlemesinden cozer -> Cloudflare HTTP 421 (2026-11-26 olcumu).

    Gamma yoklamasi DoH adresini --resolve ile socks5:// uzerinden sabitlemeli.
    """
    lines = _code_lines()
    code = "\n".join(lines)
    assert "https://1.1.1.1/dns-query" in code
    assert "$proxyLocalDns = 'socks5://127.0.0.1:40000'" in code
    resolve_lines = [ln for ln in lines if "--resolve" in ln]
    assert resolve_lines and all("--proxy $proxyLocalDns" in ln for ln in resolve_lines)


def test_every_tick_decision_uses_both_probes():
    """Ilk yoklama, 5 sn sonraki tekrar ve reconnect sonrasi kontrol Get-ProbeFailure kullanir."""
    code = "\n".join(_code_lines())
    assert len(re.findall(r"\$why = Get-ProbeFailure", code)) == 3
    # Test-WarpProxy: yalnizca tanimi + Get-ProbeFailure icindeki tek cagri
    assert len(re.findall(r"Test-WarpProxy", code)) == 2


@needs_ps_curl
def test_gamma_probe_fails_when_proxy_cannot_reach_cloudfront(unreachable_socks):
    """DoH cevap verdi ama proxy adrese ulasamiyor; en fazla 2 adres denenir (PT5M gorev siniri)."""
    res = _run_probe(
        "Test-GammaProxy",
        "function Get-GammaIps { return @('192.0.2.10', '192.0.2.11', '192.0.2.12') }; "
        f"$proxyLocalDns = 'socks5://127.0.0.1:{unreachable_socks.port}'",
    )
    assert res == "gamma-api (CloudFront) unreachable through proxy"
    assert unreachable_socks.connects == 2


@needs_ps_curl
def test_gamma_probe_fails_when_doh_unreachable(unreachable_socks):
    res = _run_probe("Test-GammaProxy", f"$proxy = 'socks5h://127.0.0.1:{unreachable_socks.port}'")
    assert res == "gamma-api DoH lookup through proxy failed"
    assert unreachable_socks.connects == 1


@needs_ps_curl
def test_tick_fails_when_only_gamma_is_unreachable(unreachable_socks):
    """2026-11-25 durumu: cdn-cgi/trace geciyor, gamma-api ulasilamiyor -> tick basarisiz."""
    res = _run_probe(
        "Get-ProbeFailure",
        "function Test-WarpProxy { return $true }; function Get-GammaIps { return @('192.0.2.10') }; "
        f"$proxyLocalDns = 'socks5://127.0.0.1:{unreachable_socks.port}'",
    )
    assert res == "gamma-api (CloudFront) unreachable through proxy"


@needs_ps_curl
@pytest.mark.usefixtures("production_host")
def test_both_probes_pass_on_production():
    """Uretim makinesinde WARP saglikliyken iki yoklama da gecer (ariza = FAIL)."""
    assert _run_probe("Get-ProbeFailure") == ""
