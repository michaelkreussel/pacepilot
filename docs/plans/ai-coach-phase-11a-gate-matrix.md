# AI Coach Phase 11A Gate Matrix

Status: abgeschlossen (26. August 2026)  
Feature Flag: `COACH_PLAN_GENERATION_ENABLED=false` im committed Default.

## MVP-Grenze

Der deterministische Shadow-Planer platziert ausschliesslich aktive Registry-Templates:
`easy_run`, `long_run` und `strides`. Er erzeugt typisierte Kandidaten, aber keine Workouts oder
Planzeilen. Phase 11B ergaenzt spaeter die explizite Persistenzaktion.

## Gate Matrix

| Gate | Status | Nachweis |
| --- | --- | --- |
| Migration D und user-scoped Inputs | gruen | `tests/test_migrations.py::test_athlete_planning_inputs_fresh_and_filled_upgrade`, `tests/test_athlete_planning_inputs.py` |
| Persistierte Anker speisen Intensitaetsmodell | gruen | `tests/test_athlete_planning_inputs.py::test_persisted_anchors_feed_intensity_guidance` |
| Nur aktive Templates | gruen | `tests/test_weekly_planner.py::test_planner_places_only_active_supported_templates` |
| Verfuegbarkeit und Zeitbudgets | gruen | `tests/test_weekly_planner.py::test_sessions_respect_availability_and_budgets` |
| Frequenz, Re-Entry und Mindestdaten | gruen | `tests/test_weekly_planner.py::test_target_frequency_is_capped_by_baseline_and_availability`, `::test_insufficient_baseline_refuses_to_plan` |
| Long-Run-Historie und Eligibility | gruen | `tests/test_weekly_planner.py::test_long_run_requires_consistent_running_weeks`, `::test_long_run_is_bounded_by_recent_longest_run` |
| Quality Density und No-Catch-Up | gruen | `tests/test_weekly_planner.py::test_quality_sessions_keep_minimum_spacing`, `::test_plan_never_stacks_missed_sessions` |
| Safety Stop dominiert | gruen | `tests/test_weekly_planner.py::test_safety_stop_blocks_the_whole_shadow_week` |
| Determinismus und keine Persistenz | gruen | `tests/test_weekly_planner.py::test_identical_inputs_produce_identical_candidates`, `::test_planning_writes_no_rows` |
| Property Tests | gruen | `tests/test_weekly_planner.py::test_property_plan_respects_hard_invariants` |
| Flag, Auth und User Scope | gruen | `tests/test_routes.py::test_planning_shadow_view_is_404_while_flag_disabled`, `::test_planning_shadow_view_redirects_unauthenticated`, `::test_planning_shadow_view_renders_deterministic_week` |
| Desktop, Mobile und Accessibility | gruen | Agent-Browser Session `phase-11a-shadow`; axe WCAG 2 A/AA: 0 Verstosse |

## Ergebnis

Alle Gates sind gruen. `threshold_cruise`, `vo2_intervals`, `recovery_run`, Zielphasenlogik und
Planning-Input-Formulare bleiben ausserhalb des 11A-Umfangs.
