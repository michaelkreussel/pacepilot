# Phase 2 Gate Matrix - Shared Workout Application Service

**Status:** Completed  
**Date:** 22 August 2026  
**Feature flag:** None. This phase preserves the existing manual workout flow.

| Exit criterion | Verification | Fixture / expected state | Command |
|---|---|---|---|
| Manual create, edit, confirm, publish, push, and delete preserve behavior | `tests/test_routes.py::test_create_and_confirm_workout`, workout route characterization matrix, direct service tests | Authenticated user; legacy status and remote-ID combinations remain unchanged | `uv run pytest tests/test_routes.py tests/test_workout_service.py` |
| Routes contain no status or Garmin orchestration | Source review of `app/routes/workouts.py`; direct service command tests | Routes only parse forms, render responses, and translate service errors | `uv run ruff check app/routes/workouts.py app/services/planning/workout_service.py` |
| Every command is user-scoped | `tests/test_workout_service.py::test_service_is_user_scoped`, `tests/test_routes.py::test_other_users_workout_routes_return_404_without_side_effects` | A second user's workout remains unchanged and no Garmin call occurs | `uv run pytest tests/test_workout_service.py::test_service_is_user_scoped tests/test_routes.py::test_other_users_workout_routes_return_404_without_side_effects` |
| Structural validation errors have stable codes and unchanged German text | `tests/test_workouts.py::test_validation_errors_have_stable_codes` | Invalid workout and definition inputs expose a code while `str(error)` remains German UI copy | `uv run pytest tests/test_workouts.py::test_validation_errors_have_stable_codes` |
| Publish keeps the upload checkpoint and retry reuses the remote ID | `tests/test_routes.py::test_publish_retry_reuses_uploaded_garmin_workout` | First schedule fails after upload; retry does not upload again | `uv run pytest tests/test_routes.py::test_publish_retry_reuses_uploaded_garmin_workout` |
| Garmin payloads are unchanged and the compiler is public | Garmin V1 golden tests in `tests/test_workouts.py` | All four supported sports compile to the existing payloads | `uv run pytest tests/test_workouts.py -k garmin` |
| Repository quality gates pass | Full suite, lint, format, and type checks | No regressions | `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run ty check` |
