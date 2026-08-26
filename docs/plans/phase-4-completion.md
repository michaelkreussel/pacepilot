# Phase 4 Completion - Separate and Idempotent Garmin State

**Status:** Completed  
**Date:** 22 August 2026  
**Next phase:** Phase 5 - Running Baseline and Intensity Model in Shadow Mode

## Delivered

- Durable `WorkoutGarminOperation` and `WorkoutGarminAttempt` records committed before each Garmin
  network mutation.
- Stable idempotency keys that make repeated requests reuse the original logical operation.
- Separate content, calendar, and device outcomes, including `request_accepted` wording that does
  not claim delivery to a watch.
- Conservative ambiguous-outcome handling: unreconciled upload, update, push, and delete operations
  require manual review instead of automatic retry.
- Calendar reconciliation that checks Garmin before retrying schedule or unschedule operations.
- Startup repair that converts interrupted pending attempts to `unknown` and blocks unsafe mutation.
- Existing account locking and Garmin pacing/cooldown behavior reused for workout writes.
- Exact accepted-revision compilation and repeated validation before delayed Garmin synchronization.
- Activity matching through account-scoped remote identity history, including removed identities.
- Garmin principal fingerprinting and quarantine of remote IDs after a different account reconnects.
- Duplicate-safe disconnect and Garmin-data deletion semantics that preserve the identity ledger.
- Manual-review UI that explains ambiguous outcomes, suppresses mutation forms, disables deletion,
  and does not display successful device delivery.
- Forward-only Alembic follow-up migration for principal fingerprints, including regression coverage
  for databases that had already applied the original Phase 4 migration.

## Verification

The Phase 4 gate matrix is documented in `docs/plans/ai-coach-phase-4-gate-matrix.md`.

```text
uv run pytest                    229 passed
uv run pytest tests/test_migrations.py
                                      9 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
git diff --check                 passed
```

Agent Browser verified the authenticated ambiguous-outcome detail screen in the named
`phase4-final` session. It showed the manual-review warning and a disabled deletion control, exposed
no confirm, publish, push, schedule, unschedule, or delete form, and described the device request as
not accepted. The temporary local binding state used for this check was restored afterward.

The remaining warnings are the existing Starlette `TestClient`/httpx deprecation and Python 3.12
SQLite datetime-adapter deprecations exercised by migration tests.

## Phase 5 Entry Point

Build reproducible, data-quality-aware running baselines for the 7, 28, 56, and 180 day windows,
then expose conservative intensity guidance in shadow mode without generating workouts.
