# Strategy selection, time gate, rotation, and polling

## Overview

Implement the requested entry policy in the backend scan cycle.

## Goals

- Group all open weather markets by city/date/metric.
- Keep all candidates tied at the highest YES price.
- Make only YES entries eligible.
- Accept YES price in `[0.10, 0.95)`.
- Gate markets two or more days ahead until 13:00 UTC.
- Rotate only when a strictly higher price candidate exists.
- Update market prices on the five-minute scan cadence.

## Scope / non-goals

In scope: candidate selection, calculator side selection, entry gates,
rotation hook, polling interval. Out of scope: live trading credentials,
settlement redesign, and ladder accounting redesign from the previous audit.

## User flows / UX / design notes

The scan fetches and parses markets, selects candidates, analyzes candidates,
rotates inferior open positions, and places eligible candidates. Tied maximum
price candidates are both visible and eligible.

## Functional requirements

1. Group key is normalized city, target-date calendar date, and metric.
2. Candidate maximum is computed from `yes_price` only.
3. Candidates with `yes_price < 0.10` or `yes_price >= 0.95` are rejected.
4. Candidate side is always `YES`.
5. A candidate with `days_ahead >= 2` is blocked before 13:00 UTC.
6. `abs(net_edge)` is replaced by positive-side `net_edge >= effective_min_edge`.
7. Existing group positions are rotated only when candidate price is strictly greater.
8. Ties do not trigger rotation and are not deduplicated at selection time.
9. Scan and price update interval is 300 seconds.

## Data model / schema

No migration is required. Existing `WeatherMarket`, `Analysis`, and `Bet`
fields are reused. Group identity is computed, not persisted.

## API contracts

No new public endpoint is required. `GET /api/status` continues to expose
portfolio and open-position data.

## Edge cases / failure modes

- Missing city/date/metric: candidate is not selectable.
- Missing or invalid YES price: candidate is not selectable.
- Multiple equal maxima: all are returned.
- Existing position with no market row: leave it for settlement/cleanup.
- Rotation failure: do not place replacement until the close action succeeds.

## Acceptance criteria

- HIGH and LOW groups both produce only YES analysis.
- A price of 0.10 is eligible; 0.09 and 0.95 are not.
- At 12:59 UTC a +2-day candidate is blocked; at 13:00 it is allowed.
- A strictly better candidate causes old group positions to close; a tie does not.
- Scan interval defaults to 300 seconds.

## Test plan / test cases

- Unit tests for grouping, ties, side, max price, and time gate.
- Calculator test verifying no NO recommendation.
- Integration test verifying only selected market analyses are placeable.
- Configuration test verifying five-minute polling.

## Implementation notes

Keep helpers pure so tests can use simple dataclass-like market objects.

## Status / open questions

Status: done. The requested 13:00 gate is interpreted as UTC because the
bot's existing scheduling and DB timestamps are UTC-normalized.
