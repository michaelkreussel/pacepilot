# Phase 3 Gate Matrix - Immutable Revisions and Exact Acceptance

**Status:** Completed  
**Date:** 22 August 2026  
**Feature flag:** None for manual workouts. Coach proposal flags remain disabled.

| Exit criterion | Verification | Fixture / expected state | Command |
|---|---|---|---|
| Fresh and populated databases migrate without loss | `tests/test_migrations.py::test_initial_migration_matches_models`, `tests/test_migrations.py::test_workout_revision_backfill_matrix` | Full legacy status, remote-ID, and date matrix; revision 1 preserves content and suggested date | `uv run pytest tests/test_migrations.py` |
| Revision pointers cannot cross workouts and replacements cannot cross users | `tests/test_migrations.py::test_workout_revision_constraints` | Synthetic FK violations fail; valid same-owner links succeed | `uv run pytest tests/test_migrations.py::test_workout_revision_constraints` |
| Revisions are immutable and sequential | `tests/test_workout_revisions.py::test_revision_is_immutable`, `tests/test_workout_revisions.py::test_revision_is_immutable_for_direct_sql`, migration backfill test | ORM and direct-SQL updates fail against both test metadata and the Alembic-installed trigger; edits append revision 2 | `uv run pytest tests/test_workout_revisions.py tests/test_migrations.py` |
| Exact stale revision cannot be accepted | `tests/test_workout_revisions.py::test_accept_rejects_stale_revision` | ID, number, hash, and lock version mismatch return conflict with no state change | `uv run pytest tests/test_workout_revisions.py::test_accept_rejects_stale_revision` |
| Editing an accepted workout preserves its accepted execution | `tests/test_workout_revisions.py::test_edit_after_acceptance_keeps_previous_execution` | Revision 2 becomes current/proposed while revision 1 remains accepted and materialized | `uv run pytest tests/test_workout_revisions.py::test_edit_after_acceptance_keeps_previous_execution` |
| Garmin compiles only the exact accepted revision | `tests/test_workout_revisions.py::test_garmin_uses_accepted_revision` and existing Garmin golden tests | New unaccepted candidate content never appears in upload/update payloads | `uv run pytest tests/test_workout_revisions.py tests/test_workouts.py -k garmin` |
| Garmin uncertainty and calendar replacement are mutation-safe | Unknown-state, reacceptance, reschedule, unschedule, pending-delete, and retry tests | Every unknown dimension blocks; device authority resets; old remote dates are removed; failed calendar writes remain retryable and block Push | `uv run pytest tests/test_workout_revisions.py tests/test_routes.py -k "unknown or reschedule or unschedule or publish_retry"` |
| Calendar, dashboard, and athlete data use local schedule plus accepted content | Calendar and analytics tests in `tests/test_routes.py` and `tests/test_athlete_trends.py` | Suggested drafts and tombstones are absent; accepted scheduled revision is shown | `uv run pytest tests/test_routes.py tests/test_athlete_trends.py` |
| Acceptance never silently changes calendar authority | `tests/test_routes.py::test_confirming_does_not_silently_schedule` | Acceptance leaves local schedule unchanged; explicit action names the accepted revision and target date | `uv run pytest tests/test_routes.py::test_confirming_does_not_silently_schedule` |
| Context fingerprint changes create a new append-only validation run | `tests/test_workout_revisions.py::test_changed_context_creates_validation_run` | Same context reuses fresh evidence; changed fingerprint appends a run | `uv run pytest tests/test_workout_revisions.py::test_changed_context_creates_validation_run` |
| Normal deletion preserves revisions and audit history | `tests/test_workout_revisions.py::test_delete_tombstones_workout` | Detail/calendar return no active workout; revisions and events remain | `uv run pytest tests/test_workout_revisions.py::test_delete_tombstones_workout` |
| Candidate and accepted revisions are both understandable in the UI | Isolated `agent-browser` desktop/mobile/keyboard/accessibility checks | Source, revision, warning, diff, and exact action target are visible without color-only meaning | `just check` plus named-session manual flow |
| Repository quality gates pass | Full suite, lint, format, typing, migration schema check | No regressions and no schema drift | `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` |

## Browser Verification

- Auth session created through `just get-session 9222 http://127.0.0.1:8010/` against an isolated
  migrated database.
- Named isolated sessions: `phase3-final-30364969a79f` and `phase3-mobile-30364969a79f`.
- Desktop viewport: 1440 x 1000; mobile viewport: 390 x 844.
- Detail and editor render without page-level horizontal overflow at mobile width; the wide month
  grid remains independently scrollable without moving the page.
- Keyboard focus is visible and the exact revision acceptance action submits with Enter.
- Axe 4.12.1 reports zero violations and zero incomplete checks for workout detail, workout editor,
  and month calendar after the identified landmark and calendar-label issues were corrected.
- Accepting with Enter retained the existing calendar date. A separate accepted-revision action then
  moved the local calendar date to the displayed suggestion.
