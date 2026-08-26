# AI Coach Phase 4 Gate Matrix

Phase 4 introduces the shared Garmin mutation ledger for manual and future coach workouts. The
coach Garmin feature flag remains disabled; manual workout routes exercise the same service.

| Gate | Test or check | Fixture | Expected result |
|---|---|---|---|
| Exact accepted revision | `tests/test_workout_revisions.py::test_garmin_uses_accepted_revision` | Accepted revision plus newer candidate | Only the accepted revision is compiled |
| Durable operation boundary | `tests/test_workout_service.py::test_ambiguous_upload_is_recorded_and_not_retried` | Upload with lost response | Pending operation and attempt exist before the call; outcome becomes `unknown` |
| Idempotent push | `tests/test_routes.py::test_repeated_push_reuses_idempotent_operation` | Two identical push requests | One operation, attempt, and Garmin call |
| Split upload/calendar result | `tests/test_routes.py::test_publish_retry_reuses_uploaded_garmin_workout` | Successful upload plus failed schedule | Content stays synced while calendar is unknown |
| Calendar reconciliation | `tests/test_routes.py::test_publish_retry_reuses_uploaded_garmin_workout` | Unknown schedule followed by retry | Calendar is read first; mutation retries only after absence is proven |
| Startup repair | `tests/test_scheduler.py::test_interrupted_workout_attempt_becomes_unknown_after_restart` | Pending operation and attempt | Both become `unknown`; the affected binding axis is blocked |
| Historical activity matching | `tests/test_activity_backfill.py::test_activity_matches_removed_remote_identity_for_own_account` | Removed identity shared by two local users | Activity links only to its owner's stable workout |
| Duplicate-safe data deletion | `tests/test_routes.py::test_delete_garmin_data_preserves_connection_user_and_local_workout` | Synced workout and imported Garmin data | Imported data is deleted; identity ledger and remote state remain |
| Garmin principal isolation | `tests/test_routes.py::test_reconnecting_different_garmin_principal_quarantines_remote_ids` | Existing remote ID plus different reconnect email | All old binding axes become `unknown`; no old remote ID is mutated |
| Fresh and populated schema | `tests/test_migrations.py` | Fresh DB and populated Phase 3 DB | Migration and Alembic schema check pass |
| Manual-review UI | Workout route tests and final browser check | Unknown remote outcome | UI blocks unsafe mutation and does not claim device delivery |

## Reconciliation Capabilities

| Operation | Reliable automatic reconciliation | Policy after ambiguous outcome |
|---|---|---|
| Upload | No | Block automatic retry; manual review |
| Update | No | Block automatic retry; manual review |
| Schedule | Yes, calendar lookup | Settle success when present; retry only when absence is proven |
| Unschedule | Yes, calendar lookup | Settle success when absent; retry only when presence is proven |
| Push | No | Block automatic retry; `request_accepted` never means device delivery |
| Delete | No | Block automatic retry; manual review |
