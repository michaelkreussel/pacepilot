# Phase 3 Completion - Immutable Revisions and Exact Acceptance

**Status:** Completed  
**Date:** 22 August 2026  
**Next phase:** Phase 4 - Separate and Idempotent Garmin State

## Delivered

- Immutable, sequential `WorkoutRevision` records with explicit current, accepted, and materialized
  pointers on the stable `Workout` aggregate.
- Compare-and-swap acceptance using revision ID, revision number, content hash, context fingerprint,
  and aggregate lock version.
- Append-only contextual validation runs and workout audit events.
- Tombstone deletion that preserves workout revisions and audit history.
- Additive Garmin bindings and remote identity history with separated content, calendar, and device
  status projections.
- Conservative legacy backfill covering status, remote-ID, and date combinations without inventing
  remote success.
- Same-workout revision foreign keys and same-user replacement constraints.
- SQLite trigger enforcement prevents revision updates even through direct SQL; migration coverage
  exercises the Alembic-installed trigger.
- Explicit candidate, accepted-execution, and calendar read models for routes, Garmin compilation,
  dashboard, calendar, and athlete analytics.
- Editing an accepted workout now creates a proposed candidate while the accepted revision remains
  the only calendar and Garmin execution authority.
- Exact revision fields in the acceptance form, stale submission conflict handling, source/revision
  badges, accepted-versus-candidate comparison, and accepted-edit warnings.
- Acceptance and calendar placement are separate revision-specific actions. Rescheduling,
  unscheduling, and deletion remove the previously synchronized Garmin calendar entry rather than
  assuming the new local date is already remote.
- Unknown Garmin content, calendar, or device outcomes block mutation. Accepting new content resets
  device delivery authority, and calendar failures persist a retryable state that prevents Push.
- Garmin data deletion removes remote identity history and clears binding dates, timestamps, and
  error metadata while preserving the local workout.
- Shared Jinja workout preview and responsive editor/detail behavior.
- Mobile sidebar accessibility correction: closed navigation is inert and hidden from assistive
  technology, and complementary landmarks have unique names.
- Tailwind 4.3.3 output rebuilt and stylesheet cache key advanced to `20260822-4`.
- `just get-session` recipes can target an alternate local app URL without changing the default.

## Verification

```text
uv run pytest                    220 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
git diff --check                 passed
```

Migration coverage includes fresh databases, populated upgrades, the full legacy backfill matrix,
schema comparison, direct-SQL immutability, and cross-workout/cross-user constraint failures.

Browser verification used named `phase3-final-30364969a79f` and
`phase3-mobile-30364969a79f` sessions at 1440 x 1000 and 390 x 844. Workout detail, editor, and month
calendar passed Axe 4.12.1 with zero violations and zero incomplete checks. Exact acceptance was
completed using keyboard focus and Enter, retained the existing calendar date, and exposed a
separate action that moved the accepted revision to its suggested date.

The remaining warnings are the existing Starlette `TestClient`/httpx deprecation and Python 3.12
SQLite datetime-adapter deprecations exercised by migration tests.

## Phase 4 Entry Point

Add persistent `WorkoutGarminOperation` and `WorkoutGarminAttempt` records, derive idempotency keys
before network calls, prevent ambiguous automatic retries, reconcile unknown outcomes, and move
activity backfill fully onto remote identity history.
