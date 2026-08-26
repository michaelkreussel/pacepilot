# Phase 1 Completion - Security and Characterization

**Status:** Completed  
**Date:** 20 August 2026  
**Next phase:** Phase 2 - Shared Workout Application Service

## Delivered

- Global session-bound CSRF protection for every unsafe HTTP method.
- Shared `csrf_field()` helper in every POST form.
- CSRF header support for coach multipart/SSE requests and future unsafe HTMX requests.
- One MiB request-size guard before CSRF body parsing, including header-token requests.
- OAuth login initiation changed from session-mutating GET to CSRF-protected POST.
- UUID `X-Request-ID` on successful, handled-error, redirect, streaming, and unhandled-error
  responses.
- Request ID propagated into coach runtime and privacy-safe stream logs.
- Strict coach SSE validation for content type, redirects, and terminal events.
- Safe-default feature flags for future coach proposals, Garmin sync, daily adaptation, and plan
  generation. These flags do not affect the existing read-only coach or manual Garmin flows.
- WorkoutDefinition V1 round-trip, strict-field, validation-contract, and Garmin golden tests.
- Characterization tests for user isolation, legacy status/remote-ID combinations, edit-after-
  confirm, inconsistent draft updates, repeated publish, and repeated push.
- Documentation and deployment examples updated.

## Verification

```text
uv run pytest                    215 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
git diff --check                 passed
```

The remaining pytest warning is the existing Starlette deprecation warning for the current
`TestClient`/httpx integration.

Browser verification used an isolated temporary database and `agent-browser`:

- native workout create and confirm flow passed;
- native coach conversation creation passed;
- tokenless POST returned 403 and a valid request ID;
- OAuth provider controls render as POST forms with non-empty CSRF tokens;
- desktop and 390 x 844 mobile layouts passed visual inspection;
- WCAG 2 A/AA scans reported zero violations on coach and login screens.

## Phase 2 Entry Point

Extract the existing non-HTTP workout behavior from `app/routes/workouts.py` into one user-scoped
application service while preserving the now-characterized behavior. Form parsing stays in the
route adapter, and the existing `WorkoutDefinition`, validator, editor, preview, Garmin compiler,
account lock, and Garmin client remain the only implementations.
