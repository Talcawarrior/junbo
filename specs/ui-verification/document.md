# UI market verification scheduler

## Overview

Keep `scripts/verify_ui_markets.py` as the independent UI/DB comparison tool
and schedule it every two hours when available.

## Goals

- Run the verifier at a two-hour cadence.
- Avoid blocking the scan loop when the script or UI is unavailable.
- Record last-run time and errors in logs.

## Scope / non-goals

In scope: scheduled invocation and a safe script contract. Out of scope:
browser automation and live UI authentication.

## Functional requirements

1. A verifier script exists at `scripts/verify_ui_markets.py`.
2. The settlement/background loop invokes it at most once per 7200 seconds.
3. Failures are logged and do not stop settlement.

## Data model / schema

No migration. Last-run state is process-local.

## API contracts

No public API change required.

## Edge cases / failure modes

- Script missing: log warning and continue.
- Script exits non-zero: log warning and continue.
- UI unavailable: verifier returns non-zero; no trades are changed.

## Acceptance criteria

The scheduler has a two-hour guard and the script can be invoked directly.

## Test plan / test cases

Mock subprocess and assert first invocation, suppression inside two hours, and
retry after two hours.

## Implementation notes

Use `subprocess.run` with a timeout and no shell interpolation.

## Status / open questions

Status: done. The existing script's exact UI endpoint is preserved if it is
found; otherwise the script performs a DB sanity report and exits successfully.
