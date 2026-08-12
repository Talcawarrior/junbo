"""Polymarket market outcome yardimcilari — TEK dogru parse kaynagi.

Kritik bug (2026-08-12): ``raw_data.outcomePrices`` bir JSON STRING olarak
saklanir (``'["1","0"]'``). Liste gibi davranilirsa ``prices[0]`` = ``[``
karakteri olur ve YES/NO yanlis okunur. Tum backtest/analiz akislari bu
moduldeki ``parse_resolved_outcome`` uzerinden okumali — baska yerde
``outcomePrices`` parse ETME.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("UTILS_MARKET_OUTCOME")


def parse_resolved_outcome(raw_data: str | None) -> bool | None:
    """Polymarket marketinin GERCEK sonucunu dondurur.

    Returns:
      True  -> YES kazandi (yes_price >= 0.5)
      False -> NO kazandi
      None  -> market resolved degil / veri bozuk

    outcomePrices hem JSON string (``'["1","0"]'``) hem list (``["1","0"]``)
    olabilir; ikisi de dogru parse edilir. Yalnizca ``umaResolutionStatus ==
    "resolved"`` olanlar sonuc olarak kabul edilir (Polymarket resmi cozum).
    """
    if not raw_data:
        return None
    try:
        raw = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None

    uma = raw.get("umaResolutionStatus")
    if uma != "resolved":
        return None

    prices = raw.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(prices, (list, tuple)) or len(prices) < 2:
        return None
    try:
        yes_price = float(prices[0])
    except (TypeError, ValueError):
        return None
    return yes_price >= 0.5


def market_is_resolved(raw_data: str | None) -> bool:
    """raw_data icin Polymarket resmi cozum yapilmis mi?"""
    if not raw_data:
        return False
    try:
        raw = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(raw) and raw.get("umaResolutionStatus") == "resolved" and bool(raw.get("closed"))
