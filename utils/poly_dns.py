"""Client-side DNS override for gamma-api.polymarket.com.

The machine hosts file pins gamma-api.polymarket.com to a Cloudflare
edge, but the API is served by CloudFront and answers HTTP 421
("not served on this path").  Resolving through Cloudflare DoH via the
existing Polymarket SOCKS proxy and pinning the answer in-process fixes
the lookup without editing the system hosts file.

The override is installed lazily: no network call happens until the
name is actually resolved.  Set POLY_DNS_FIX=0 to disable it.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

_TARGET = "gamma-api.polymarket.com"
_DOH_URL = "https://1.1.1.1/dns-query"
_TTL_S = 3600.0
_RETRY_S = 60.0

_log = logging.getLogger("POLY_DNS")

_lock = threading.Lock()
_ips: list[str] = []
_expires = 0.0
_next_try = 0.0
_proxy: str | None = None
_installed = False


def _doh_lookup() -> list[str]:
    """Resolve the target through DoH.  Never raises."""
    if not _proxy:
        return []
    try:
        import requests

        resp = requests.get(
            _DOH_URL,
            params={"name": _TARGET, "type": "A"},
            headers={"accept": "application/dns-json"},
            proxies={"http": _proxy, "https": _proxy},
            timeout=4,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - DNS fix must never break requests
        _log.warning("gamma-api DoH lookup failed: %s", exc)
        return []
    answers = data.get("Answer") or []
    return [a.get("data", "") for a in answers if a.get("type") == 1 and a.get("data")]


def _get_ips() -> list[str]:
    """Return cached IPs, refreshing through DoH when stale."""
    global _ips, _expires, _next_try
    now = time.monotonic()
    if _ips and now < _expires:
        return _ips
    if now < _next_try:
        return _ips
    with _lock:
        now = time.monotonic()
        if _ips and now < _expires:
            return _ips
        if now < _next_try:
            return _ips
        static = [ip.strip() for ip in os.getenv("POLY_GAMMA_IP", "").split(",") if ip.strip()]
        ips = _doh_lookup() or static
        if ips:
            _ips = ips
            _expires = now + _TTL_S
        else:
            _next_try = now + _RETRY_S
        return _ips


def install(proxy_url: str | None = None) -> None:
    """Patch socket.getaddrinfo for the target host only.  Idempotent."""
    global _proxy, _installed
    if _installed:
        return
    _proxy = proxy_url or None
    _orig = socket.getaddrinfo

    def _resolver(host, port, *args, **kwargs):
        try:
            name = host.decode() if isinstance(host, bytes) else host
            if isinstance(name, str) and name.rstrip(".").lower() == _TARGET:
                ips = _get_ips()
                if ips:
                    results = []
                    for ip in ips:
                        results.extend(_orig(ip, port, *args, **kwargs))
                    return results
        except Exception:  # noqa: BLE001 - fall back to the system resolver
            pass
        return _orig(host, port, *args, **kwargs)

    socket.getaddrinfo = _resolver
    _installed = True
