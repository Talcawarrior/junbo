"""Junbo Data Pipeline modules.

High-fidelity data ingestion layer for backtesting. Five modules, each with
a single responsibility, all producing pandas DataFrames that the
unified_datastore joins into walk-forward-out-of-sample splits.

Modules:
  weather_ensemble      - Open-Meteo 8-model ensemble (forecast + archive actuals)
  poly_data_ingest      - Polygon on-chain OrderFilled events (warproxxx/poly_data port)
  resolvedmarkets_ingest - resolvedmarkets.com REST client (orderbook snapshots)
  polymarket_ingest     - Polymarket Gamma API bulk market + outcome fetcher
  unified_datastore     - Joins all 4 sources, builds walk-forward OOS splits

The previous poly_data_helper.py and resolved_markets_helper.py were mock
skeletons; they remain in the package for backwards compatibility but the
new *_ingest.py modules above are the production-grade implementations.
"""

__all__ = [
    "weather_ensemble",
    "poly_data_ingest",
    "resolvedmarkets_ingest",
    "polymarket_ingest",
    "unified_datastore",
]
