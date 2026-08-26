# AI Coach Phase 11B Gate Matrix

Status: abgeschlossen am 26. August 2026; Browser-Gate auf Nutzerwunsch ausgelassen  
Feature Flag: `COACH_PLAN_GENERATION_ENABLED=false` im committed Default.

## MVP-Grenze

Ein validierter Shadow-Wochenkandidat kann als immutable Planrevision und als normale
Workout-Vorschlaege persistiert werden. Jedes Workout bleibt einzeln anzunehmen; Batch-Accept,
Batch-Sync und automatische Replanung sind nicht enthalten.

## Gate Matrix

| Gate | Status | Nachweis | Erwartung |
| --- | --- | --- | --- |
| Planmodelle und Migration | gruen | `tests/test_migrations.py::test_training_plan_migration_creates_revision_and_membership_tables` | `TrainingPlan`, immutable `TrainingPlanRevision` und Membership; keine Definition im Planmodell |
| Atomare Persistenz | gruen | `tests/test_weekly_plan_service.py::test_persist_week_creates_plan_revision_and_normal_workout_proposals`, `::test_persist_week_rolls_back_everything_when_one_workout_fails` | Plan, Revision, Memberships und Workouts entstehen gemeinsam oder gar nicht |
| Idempotenz | gruen | `tests/test_weekly_plan_service.py::test_persist_week_is_idempotent_and_calendar_is_user_scoped` | Gleicher Fingerprint erzeugt weder zweite Revision noch zweite Workouts |
| Immutable Revision und aktuelle Sicht | gruen | `tests/test_weekly_plan_service.py::test_new_revision_is_current_without_mutating_previous_revision` | Historie bleibt erhalten; Kalender zeigt nur die aktuelle Revision |
| Einzelannahme und kein Sync | gruen | `tests/test_weekly_plan_service.py::test_persist_week_creates_plan_revision_and_normal_workout_proposals` | Workouts bleiben unangenommene, ungeplante Einzelvorschlaege; keine Garmin-Operation |
| Kalenderintegration | gruen | `tests/test_routes.py::test_plan_persistence_is_flagged_idempotent_and_visible_in_calendar` | Bestehender `/plans`-Kalender zeigt Vorschlaege getrennt von angenommenen Einheiten |
| User Scope, Flag und CSRF | gruen | `tests/test_weekly_plan_service.py::test_persist_week_is_idempotent_and_calendar_is_user_scoped`, `tests/test_routes.py::test_plan_persistence_requires_flag_and_csrf` | Fremde Plaene unsichtbar; POST nur mit Flag, Auth und CSRF |
| UI-Integration | gruen | `tests/test_routes.py::test_plan_persistence_is_flagged_idempotent_and_visible_in_calendar` | Persistenzaktion, Kalenderkarte, Detailstatus und gesperrte Bearbeitung werden serverseitig gerendert |
| Browser/A11y | ausgelassen | Nutzerentscheidung am 26. August 2026 | Kein Phase-11B-Agent-Browser-Pass; verbleibendes visuelles und interaktives Restrisiko ist dokumentiert |

## Freigabe

Automatisierte Freigabe abgeschlossen: 371 Tests, 15 Migrationstests, Ruff Check/Format und
`ty check` gruen. Der Browser-Pass wurde auf ausdruecklichen Nutzerwunsch nicht ausgefuehrt.
