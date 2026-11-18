# Realistic orderbook backtest

## Overview
Replay timestamped market/orderbook snapshots with latency, partial fills,
depth-aware VWAP, fees, and mark-to-market settlement.

## Goals
Avoid the prior oracle-like single-price fill assumption and produce an audit
trail of fills, costs, and rejected/unfilled capital.

## Scope / non-goals
Pure deterministic simulator; no strategy optimisation or live execution.

## User flows / UX / design notes
Caller supplies snapshots and a strategy callback/order request. The simulator
returns fills and portfolio metrics for offline analysis.

## Functional requirements
Orders execute at the first snapshot at or after `submitted_at + latency`;
asks are consumed in price order; partial fills are allowed; fee and gas are
deducted; unresolved inventory is marked to the next available best bid.

## Data model / schema
`OrderBookSnapshot(timestamp, bids, asks)` and `SimulatedFill` dataclasses.

## API contracts
`simulate_order(order, snapshots, fee_rate, gas_cost_usd, latency_seconds)`
returns fills, unfilled amount, VWAP, fees, gas, and mark price.

## Edge cases / failure modes
Empty books, invalid prices, zero-size levels, no post-latency snapshot, and
insufficient depth must never create synthetic fills.

## Acceptance criteria
Given two ask levels and a latency shift, the simulator returns a partial or
multi-level VWAP with exact fee/gas totals and no fill before latency expires.

## Test plan / test cases
Test latency, full depth fill, partial fill, empty book, invalid levels, fee,
gas, and mark-to-market behavior.

## Implementation notes
The simulator is independent of SQLAlchemy and can consume rows exported by
`UnifiedDatastore` or `ResolvedMarketsClient`.

## Status / open questions
Status: done. Walk-forward orchestration remains a follow-up.