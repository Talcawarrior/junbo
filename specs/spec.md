# Junbo Strategy Update Specification

## Project overview

Junbo is a dry-run-first Polymarket weather market bot. This change aligns the
entry strategy, rotation, polling, accounting KPIs, and UI verification with
the requested operating rules.

## Goals

- Select only the highest `yes_price` market in each `(city, target_date, metric)` group.
- Always enter the YES side, for both HIGH and LOW temperature markets.
- Reject entry prices `>= 0.99`.
- Apply the 13:00 UTC gate to markets at least two calendar days ahead.
- Rotate out of an existing group position when a strictly better candidate appears.
- Preserve ties: every candidate at the group maximum price remains eligible.
- Poll prices every five minutes.
- Keep `net_edge >= effective_min_edge` as a mandatory entry gate.
- Expose the requested 11 KPI values and calculate available cash consistently.
- Run UI/DB verification every two hours.

## Design direction

The existing dashboard remains the operational view. Labels and values should
be explicit about realized versus unrealized values and should avoid silently
mixing planned exposure with cash.

## Technical stack decisions

- Python 3.12+ for the backend runtime (existing repository requirement).
- FastAPI, SQLAlchemy, SQLite, pytest.
- Next.js/TypeScript dashboard under `src/`.
- No new database provider and no live trading credentials.
- Polymarket Gamma market metadata remains the market discovery source; the
  API exposes active/closed market metadata and binary YES/NO outcomes.

## Architecture rules

1. Strategy gates belong in backend code, not only in the dashboard.
2. The executor re-checks all safety gates before placing a bet.
3. Group identity is `(normalized city, target date, metric)`.
4. YES is the only allowed `recommended_side` for this strategy.
5. Exposure and max-bet caps remain hard limits.
6. Available cash is a display and safety quantity, never a substitute for
   the hard exposure cap.
7. Rotation must never close a position merely because of a tie.

## Feature list

| Feature | Status | Spec |
|---|---|---|
| Group candidate selection and YES-only entry | done | [specs/strategy-selection/document.md](specs/strategy-selection/document.md) |
| Time gate, rotation, and polling | done | [specs/strategy-selection/document.md](specs/strategy-selection/document.md) |
| Cash accounting and 11 KPI dashboard | done | [specs/cash-and-kpis/document.md](specs/cash-and-kpis/document.md) |
| UI market verification scheduler | done | [specs/ui-verification/document.md](specs/ui-verification/document.md) |

## Research note

Polymarket's current developer documentation describes Gamma API market
discovery and binary YES/NO market metadata. This project continues to use
the repository's existing Gamma scraper and does not change authentication or
live order submission behavior.
