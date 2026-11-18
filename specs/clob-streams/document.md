# CLOB orderbook and WebSocket reconciliation

## Overview
Use Polymarket CLOB best ask and real ask depth for YES fills, stream market
updates over the public market WebSocket, and reconcile authenticated user
orders/trades after reconnects.

## Goals
- Prefer official CLOB `/book` data over Gamma prices for execution.
- Compute VWAP and available depth from actual ask levels.
- Send the documented `PING` heartbeat every 10 seconds.
- Refresh open orders and recent trades after user-stream reconnects.

## Scope / non-goals
This change adds reusable clients and integration hooks. It does not enable
live trading by default, add maker/rebate accounting, or expose credentials to
the dashboard.

## User flows / UX / design notes
Dry-run uses the same orderbook parser and falls back safely when no token or
network is available. Live mode requires existing server-side CLOB credentials.

## Functional requirements
Market stream subscribes with `assets_ids`; user stream authenticates with
`apiKey`, `secret`, and `passphrase`. Reconnect uses exponential backoff and
authoritative open-order/recent-trade reads before event processing resumes.

## Data model / schema
No new database tables. Normalized stream events carry order ID, token ID,
matched size, price, status, trade ID, and transaction hash.

## API contracts
`CLOBClient.get_orderbook(token_id)`, `CLOBMarketStream`, and
`CLOBUserStream` are testable without network access through injected session
and callbacks.

## Edge cases / failure modes
Malformed events are ignored with a warning; missing credentials disable the
user stream; disconnected streams reconnect; empty books return a safe fallback.

## Acceptance criteria
Best ask is the lowest ask, VWAP consumes levels in ascending order, heartbeat
is emitted, and reconnect invokes authoritative recovery before callbacks.

## Test plan / test cases
Test orderbook normalization, VWAP partial final level, market subscription,
heartbeat, reconnect recovery ordering, and malformed payload handling.

## Implementation notes
Official endpoints are `https://clob.polymarket.com/book`,
`wss://ws-subscriptions-clob.polymarket.com/ws/market`, and `/ws/user`.

## Status / open questions
Status: done. Maker mode remains intentionally out of scope because it
changes fee/rebate accounting and is not required for YES taker execution.