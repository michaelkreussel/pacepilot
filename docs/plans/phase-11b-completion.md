# Phase 11B - Persistierter Wochenplan - Completion

**Status:** Completed with browser gate explicitly waived  
**Date:** 26. August 2026  
**Feature flag:** `COACH_PLAN_GENERATION_ENABLED` (committed default: off)

## Delivered

- `TrainingPlan`, immutable `TrainingPlanRevision` und immutable Plan-zu-Workout-Membership in
  Migration `20260826_27`.
- Planrevisionen speichern Versionen, Fingerprint, Generation Context und Validierungsreport,
  aber keine zweite Workout-Definition. Memberships referenzieren normale `Workout`-Aggregate.
- Composite Foreign Keys sichern Plan-/Workout-Ownership. ORM-Guards und SQLite-Trigger
  verhindern Updates an Revisionen und Memberships; ein weiterer Trigger validiert den
  `current_revision_id` gegen den zugehoerigen Plan.
- `persist_week_candidate()` persistiert Plan, Revision, Workouts, Revisionen, Audit und
  Memberships atomar. Gleiche Fingerprints sind idempotent; A/B/A reaktiviert die vorhandene
  immutable Revision; konkurrierende Unique-Konflikte werden einmal kontrolliert neu geladen
  beziehungsweise wiederholt.
- Jedes erzeugte Workout bleibt `proposed`, unangenommen und lokal ungeplant. Annahme, Ablehnung,
  Terminierung und Garmin bleiben Einzelcommands des bestehenden `WorkoutService`.
- Weekly-Proposals respektieren Plan- und Garmin-Feature-Flags. Direkte Bearbeitung ist im
  Service, in der Route und in der UI gesperrt, damit kein manueller Parallel-Lifecycle entsteht.
- `POST /plans/generate-week` ist authentifiziert, user-scoped, CSRF-pflichtig und Flag-gated.
  Die Aktion berechnet den validierten Shadow-Kandidaten erneut und leitet zum bestehenden
  Wochenkalender weiter.
- Der bestehende `/plans`-Wochen- und Monatskalender zeigt nur unangenommene Vorschlaege der
  aktuellen Planrevision, klar getrennt von angenommenen Einheiten. Akzeptierte, terminierte,
  abgelehnte oder supersedierte Vorschlaege werden dort nicht doppelt angezeigt.
- Deutsche Source- und Rollenlabels sowie ein eindeutiger Workout-Detailstatus wurden ergaenzt.

## Verification

- `uv run pytest -q`: 371 passed.
- `uv run pytest tests/test_migrations.py -q`: 15 passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 183 files already formatted.
- `uv run ty check`: passed.
- Tailwind CSS wurde nach den Template-Aenderungen gebaut; Cache-Key `20260826-22`.

## Browser Gate

Der Phase-11B-Agent-Browser-Pass fuer Desktop, Mobile, Tastatur und axe wurde auf ausdruecklichen
Nutzerwunsch nicht ausgefuehrt. Die serverseitige UI-Integration ist automatisiert getestet;
visuelle, responsive oder rein clientseitige Regressionen bleiben als Restrisiko bestehen.

## Deferred

- Batch Accept und Batch Garmin Sync.
- Automatische Replanung und Diff-basierte Planannahme.
- Mehrwochen-, Phasen- und Zielwettkampflogik aus Phase 12.
- Planning-Input-Formulare.
