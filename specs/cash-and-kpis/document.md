# Cash accounting and 11 KPI dashboard

## Overview

Expose the requested cash and PnL metrics without changing hard risk controls.

## Goals

- Calculate `availableCash = initial + realized_pnl + unrealized_pnl - exposure`.
- Show 11 KPI metrics: capital, closed net PnL, total capital, open bet total,
  available cash, open bet PnL, closed+open PnL, today's PnL, win %, ROI, and
  max currently openable amount.

## Scope / non-goals

In scope: backend status response and dashboard mapping/labels. Out of scope:
changing settlement economics or using unrealized PnL to increase hard caps.

## User flows / UX / design notes

Dashboard values must distinguish `realized`, `unrealized`, and `available`.
The available value can be negative as an alert; it must not be clamped for
reporting.

## Functional requirements

1. Backend returns `available_cash` and `max_openable_now`.
2. `max_openable_now = max(0, max_exposure - exposure)`.
3. Total capital is `initial + realized + unrealized`.
4. Closed net PnL is realized PnL from closed bets.
5. Closed+open PnL is realized plus unrealized.
6. ROI remains closed realized PnL divided by closed stake.

## Data model / schema

No migration. Values are derived from `Portfolio` and `Bet` rows.

## API contracts

`GET /api/status.portfolio` adds:

- `available_cash: number`
- `max_openable_now: number`

## Edge cases / failure modes

- No bets: exposure and unrealized are zero.
- No closed stake: ROI is zero.
- Negative available cash is reported and should be visually flagged.

## Acceptance criteria

All 11 requested metrics appear in the overview and map to backend values.

## Test plan / test cases

Use fixtures with initial 1000, realized 50, unrealized -10, exposure 200:
total capital 1040 and available cash 840.

## Implementation notes

Do not use `Portfolio.total_value` as a replacement for derived available cash.

## Compounded exposure policy

- Maximum total open exposure is `60%` of the previous day's capital basis.
- `capital_basis = initial_capital + realized_pnl_from_bets_closed_before_today`.
- Today's realized PnL changes the next day's cap, not the current day's cap.
- A daily loss circuit breaker is disabled; drawdown remains observable but does not block entries.
- Closed ROI is reported by YES entry-price bands: `0.10–0.20` through `0.90–0.95`.

## Status / open questions

Status: done. KPI labels can remain Turkish while API fields stay snake_case.
