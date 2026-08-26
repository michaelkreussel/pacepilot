# Phase 2 Completion - Shared Workout Application Service

**Status:** Completed  
**Date:** 22 August 2026  
**Next phase:** Phase 3 - Immutable Revisions and Exact Acceptance

## Delivered

- User-scoped `WorkoutService` under `app/services/planning/`.
- Central commands for get, validate, create, update, confirm, publish, push, and delete.
- Workout status transitions, commits, rollbacks, Garmin account locking, connection setup, and
  remote orchestration removed from the route handlers.
- Existing publish checkpoint preserved: a successful upload is committed before scheduling, so
  a scheduling retry reuses the remote workout ID.
- Existing manual and legacy behavior preserved, including edit-after-confirm, remote-bound draft
  updates, repeated publish, and repeated push.
- Stable machine-readable codes added to workout, definition, form-shape, transition, and Garmin
  precondition errors while retaining the existing German UI messages.
- Existing Garmin compiler exposed as `compile_workout()` with unchanged payloads for running,
  cycling, walking, and hiking.
- Direct service tests added for user isolation, validation-before-persistence, the complete manual
  lifecycle, Garmin orchestration, and transition error codes.
- Phase gate matrix recorded in `docs/plans/phase-2-gate-matrix.md`.

## Verification

```text
uv run pytest                    209 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
git diff --check                 passed
```

The remaining pytest warning is the existing Starlette deprecation warning for the current
`TestClient`/httpx integration.

No database schema, UI, feature flag, or Garmin payload changed in this phase.

## Preserved Compatibility Edges

- Editing a workout with a Garmin remote ID still updates Garmin, pushes to the device, and sets
  the legacy status to `pushed`, independent of its starting status.
- Confirm remains a no-op outside `draft`.
- Publishing remains allowed from `confirmed`, `published`, and `pushed`, and ends as `published`.
- Repeated publish reuses the remote workout and avoids an already visible calendar schedule.
- Repeated push still sends a new Garmin push request.
- Garmin side effects remain non-transactional. Persistent operation idempotency and unknown-
  outcome reconciliation remain Phase 4 work.

## Phase 3 Entry Point

Add immutable workout revisions, exact revision acceptance, audit events, minimal Garmin bindings,
and the conservative legacy backfill from the master plan. Route adapters and future coach tools
must call the shared application service rather than reintroducing direct workout mutations.
