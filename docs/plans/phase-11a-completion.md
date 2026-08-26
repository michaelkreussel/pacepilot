# Phase 11A - Planning Inputs und Shadow-Wochenplan - Completion

**Status:** Completed  
**Date:** 26. August 2026  
**Feature flag:** `COACH_PLAN_GENERATION_ENABLED` (committed default: off)

## Delivered

- Additives, user-scoped Schema fuer Profil, mehrere Ziele, wiederkehrende
  Wochenverfuegbarkeit und historisierte Performance-Anker.
- Deterministischer, nicht persistierender Wochenplaner mit eingefrorenem Input-Fingerprint,
  Generation Context und Validierungsreport.
- Platzierung ausschliesslich aktiver Registry-Templates (`easy_run`, `long_run`, `strides`) ueber
  den bestehenden Template-Expander und die kanonische Workout-Definition.
- Harte Gates fuer Mindestdaten, Safety, Verfuegbarkeit, Tagesbudgets, Baseline-Frequenz,
  Re-Entry, Long-Run-Historie, Quality Density und No-Catch-Up.
- Interne, user-scoped Shadow-Ansicht unter `/coach/planning-shadow`; das LLM ist weder an
  Planung noch Erklaertext beteiligt.

## Verification

- Unit-, Integrations- und Hypothesis-Tests in `tests/test_weekly_planner.py`,
  `tests/test_athlete_planning_inputs.py`, `tests/test_routes.py` und
  `tests/test_migrations.py`.
- Desktop 1280x800 und Mobile 390x844 mit authentifizierter Agent-Browser-Session geprueft.
- axe WCAG 2 A/AA: 0 Verstosse in befuelltem und abgelehntem Zustand.
- Vollstaendiger Abschlussstand vor 11B: 362 Tests gruen; Ruff Check/Format, `ty check` und
  Migrationstests gruen.

## Deferred

- `threshold_cruise`, `vo2_intervals` und phasenbasierte Mehrwochenlogik bleiben Phase 12.
- `recovery_run` bleibt ungenutzt, bis ein versionierter Post-Quality-Wochenkontext existiert.
- Planning-Input-Formulare sind nicht Teil von 11A.
