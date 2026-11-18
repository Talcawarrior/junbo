# Backup retention and Git exclusion

## Overview
Keep the newest seven backups per label and automatically remove older local
and mirrored copies.

## Goals
- Retain exactly the newest seven complete, integrity-checked backups per label.
- Never track runtime backup files in Git.

## Scope / non-goals
Scope is `db_backup.py`, offsite pruning, tests, and ignore rules. No backup
upload service is added.

## User flows / UX / design notes
Backup creation is safe to call repeatedly; pruning happens after a verified
backup is created. Restore selects the newest backup.

## Functional requirements
`MAX_BACKUPS` is 7 by default. `.db`, `.db.enc`, salt, WAL and SHM artifacts
belong to the same backup and are deleted together.

## Data model / schema
Filesystem-only; filenames are `bot_<label>_<timestamp>.db`.

## API contracts
Existing `create_backup`, `_prune_backups`, `list_backups`, and restore APIs
remain compatible.

## Edge cases / failure modes
Failed integrity checks are deleted; missing DB returns `None`; offsite errors
are best-effort and do not crash the bot.

## Acceptance criteria
Creating 12 backups for one label leaves 7 primary files and no stale sidecars.
`git check-ignore data/backups/...` reports ignored.

## Test plan / test cases
Unit-test local retention, all-label retention, encrypted sidecar cleanup, and
Git ignore policy.

## Implementation notes
Retention is per label to preserve startup, scheduled, and manual recovery
paths without allowing unbounded growth.

## Status / open questions
Status: done. Retention count is fixed at 7 unless an explicit config
override is added later.