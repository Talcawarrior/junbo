"""Unit tests for utils.poly_dns (gamma-api DoH override, 2026-11-08).

No network: the DoH lookup is monkeypatched, and the installed resolver is
restored in ``finally`` so the global socket patch cannot leak into other
tests.
"""

from __future__ import annotations

import socket

import utils.poly_dns as poly_dns


def _reset_state(monkeypatch, proxy: str | None = None) -> None:
    monkeypatch.setattr(poly_dns, "_installed", False)
    monkeypatch.setattr(poly_dns, "_ips", [])
    monkeypatch.setattr(poly_dns, "_expires", 0.0)
    monkeypatch.setattr(poly_dns, "_next_try", 0.0)
    monkeypatch.setattr(poly_dns, "_proxy", proxy)


def test_get_ips_falls_back_to_env_when_doh_fails(monkeypatch):
    _reset_state(monkeypatch, "socks5://127.0.0.1:40000")
    monkeypatch.setattr(poly_dns, "_doh_lookup", lambda: [])
    monkeypatch.setenv("POLY_GAMMA_IP", "203.0.113.10, 203.0.113.11")

    assert poly_dns._get_ips() == ["203.0.113.10", "203.0.113.11"]


def test_get_ips_caches_doh_answer(monkeypatch):
    _reset_state(monkeypatch, "socks5://127.0.0.1:40000")
    calls = {"n": 0}

    def fake_doh():
        calls["n"] += 1
        return ["203.0.113.20"]

    monkeypatch.setattr(poly_dns, "_doh_lookup", fake_doh)

    assert poly_dns._get_ips() == ["203.0.113.20"]
    assert poly_dns._get_ips() == ["203.0.113.20"]
    assert calls["n"] == 1


def test_install_overrides_only_gamma_host(monkeypatch):
    original = socket.getaddrinfo
    seen: list[str] = []

    def fake_original(host, port, *args, **kwargs):
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    socket.getaddrinfo = fake_original
    try:
        _reset_state(monkeypatch, None)
        monkeypatch.setattr(poly_dns, "_get_ips", lambda: ["203.0.113.30"])
        poly_dns.install()

        socket.getaddrinfo("gamma-api.polymarket.com", 443)
        assert seen[-1] == "203.0.113.30"

        socket.getaddrinfo("example.com", 443)
        assert seen[-1] == "example.com"
    finally:
        socket.getaddrinfo = original
        poly_dns._installed = False
        poly_dns._ips = []
        poly_dns._expires = 0.0
        poly_dns._next_try = 0.0
        poly_dns._proxy = None
