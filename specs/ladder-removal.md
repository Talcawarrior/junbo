# Ladder Removal

## Status

Done.

## Policy

Junbo opens and manages every position as one single fill. It does not create
deferred price rungs, add stake when a price falls, or expose ladder metadata
through the API/dashboard.

## Accounting

`Bet.amount` and `Bet.shares` represent the complete filled position. Entry
cash, exposure, unrealized PnL, early-exit proceeds, settlement, and stale-bet
refunds all use those same single-fill values.

## Schema migration

The retired `bets.ladder_data` SQLite column is dropped by the idempotent DB
migration when an existing database is initialized.