# AI Coach Phase 12 Gate Matrix

**Phase:** Mehrwochenpläne  
**Status:** abgeschlossen am 26. August 2026  
**Feature flag:** `COACH_PLAN_GENERATION_ENABLED` bleibt bis zum Rollout deaktiviert.

| Gate | Nachweis | Erwarteter Zustand |
|---|---|---|
| Zielhorizont | `tests/test_multiweek_planner.py::test_cycle_rejects_unrealistic_horizons_and_incomplete_inputs` | Mindestwochen je Zieltyp und maximal 52 Wochen; unvollständige Kandidaten werden abgelehnt |
| Phasen | `tests/test_multiweek_planner.py::test_cycle_has_versioned_phases_and_taper` | Base, Build, Specific und Taper sind deterministisch versioniert |
| Re-Entry/Unterbrechung | `tests/test_multiweek_planner.py::test_cycle_reentry_and_interruptions_never_catch_up` | Recovery-Woche bleibt leer; verpasste Einheiten werden nicht gestapelt |
| Zielprofile | `tests/test_multiweek_planner.py::test_cycle_rule_profiles_are_goal_specific` | Alle fünf Zieltypen verwenden versionierte, zielspezifische Regelbereiche |
| Sparse/Missing Wearables | `tests/test_multiweek_planner.py::test_cycle_handles_low_confidence_without_wearable_metrics` | Niedrige Confidence bleibt sichtbar; fehlende Wearable-Metriken werden nicht erfunden |
| Progression | `tests/test_multiweek_planner.py::test_cycle_has_versioned_phases_and_taper` | Wochenumfang, Long Run und Taper bleiben innerhalb des aktiven Zielprofils |
| Quality Density | `tests/test_multiweek_planner.py::test_cycle_rejects_excess_quality_density` | Zu viele oder zu eng platzierte Quality-Einheiten invalidieren den Kandidaten |
| Zielgrenze | `tests/test_multiweek_planner.py::test_cycle_target_boundary_does_not_place_after_goal` | Keine Einheit liegt nach dem Zieltag |
| Persistenz/Idempotenz | `tests/test_multiweek_planner.py::test_persist_cycle_is_idempotent_and_keeps_revisions_immutable`, `test_reactivating_cycle_revision_restores_weekly_revision_pointers` | Revisionen sind immutable; Wiederholung erzeugt keine Duplikate und restauriert Wochenpointer |
| Ownership/Annahme | `tests/test_multiweek_planner.py::test_cycle_acceptance_is_explicit_and_user_scoped` | Zyklusannahme ist user-scoped und lässt einzelne Workouts weiterhin vorgeschlagen |
| Migration/Schema | `tests/test_migrations.py`, `test_referenced_goal_cannot_be_deleted_from_cycle`, `test_cycle_delete_cascades_across_parent_revisions` | Frische und historische Datenbanken, Ownership-FKs, Non-Null-Spalten, Cascades und `alembic check` bestehen |
| Routen/Flag/UI | `tests/test_routes.py::test_multiweek_plan_page_is_flagged_and_lists_active_goals`, `test_multiweek_plan_generate_detail_and_accept` | Oberfläche ist geschützt und flag-gated; Erstellen, Detail und explizite Annahme funktionieren |

## Scope

- Unterstützte Ziele: allgemeine Fitness, 5 km, 10 km, Halbmarathon und Marathon.
- Phasenlogik nutzt ausschließlich aktive, bereits validierte Templates.
- Marathon ist im selben sicheren Generatorpfad enthalten, erhält aber keine zusätzlichen
  deferred Intervall-Templates.
- Wearable-Daten sind kontextuelle Evidenz; fehlende Wearables werden nicht durch erfundene
  Messwerte ersetzt.
- Replanung erzeugt eine neue immutable Zyklusrevision mit Parent-Revision, Annahmen, Confidence,
  Impact und Validierungsreport.
- Angenommene Workout-Revisionen werden nicht automatisch überschrieben; jede neue Workout-
  Revision bleibt ein separater Vorschlag.

## Not Run

Der Agent-Browser-Pass wurde auf ausdrücklichen Nutzerwunsch nicht ausgeführt. Visuelle,
responsive und rein clientseitige Regressionen bleiben daher ein dokumentiertes Restrisiko.

## Deferred

- Automatische Replanung aus Ausführung oder Feedback.
- Batch-Annahme und Batch-Garmin-Sync.
- Zielzeit-/Pace-Spezifikation ohne belastbare Performance-Anker.
- Neue spezifische Intervall-Templates außerhalb der aktiven Registry.
