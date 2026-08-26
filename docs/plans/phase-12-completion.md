# Phase 12 - Mehrwochenpläne - Completion

**Status:** Completed  
**Date:** 26. August 2026  
**Browser validation:** skipped by user request

## Delivered

- Neue versionierte Aggregate `TrainingCycle`, `TrainingCycleRevision` und `TrainingCycleWeek` in
  Migration `20260826_28`; Reparaturmigrationen `20260826_29` und `20260826_30` korrigieren
  historische lokale Phase-11-Schemata additiv. Migration `20260826_31` baut divergente
  Planungstabellen mit den finalen Non-Null-, Unique- und Ownership-Constraints neu auf.
- Zyklusrevisionen speichern Phasenplan, Annahmen, Confidence, Auswirkungen, Validierungsreport,
  Planner-/Knowledge-Version und Input-Fingerprint.
- Revisionen und Memberships sind auf ORM- und SQLite-Ebene immutable. Composite Foreign Keys
  sichern Zyklus-, Wochenplan- und User-Ownership.
- Deterministischer Generator für alle fünf Goal-Typen mit versionierten, zielspezifischen
  Regelprofilen und den Phasen `reentry`, `base`, `build`, `specific`, `taper` und `recovery`.
- Mindesthorizonte verhindern unrealistische Ziele. Volumen-, Long-Run-, Quality-Density- und
  Taper-Bereiche werden aus dem aktiven Zielprofil validiert; Unterbrechungen erzeugen keine
  Nachholstapelung.
- Das Zieldatum wird hart respektiert. Kandidaten nach dem Zieltag werden nicht platziert.
- Replanung erstellt eine neue Revision mit Parent-Referenz und lässt bestehende angenommene
  Workout-Revisionen unverändert.
- Persistenz läuft atomar über die bestehenden Wochenplan- und Workout-Services. Wiederholte
  gleiche Kandidaten sind idempotent. Die Reaktivierung einer historischen Zyklusrevision stellt
  zugleich alle zugehörigen aktuellen Wochenplan-Pointer wieder her.
- Neue flag-gated UI für Zykluserstellung und Zyklusdetail mit expliziter Zyklusannahme; einzelne
  Workout-Annahme bleibt weiterhin erforderlich.
- `TrainingPlanRevision` unterstützt jetzt `commit=False`, damit ein kompletter Mehrwochenzyklus
  in einer Transaktion persistiert werden kann.

## Verification

- Alle fünf Zieltypen, Sparse/Missing-Wearable-Kontext, Unterbrechungen, unrealistische Horizonte,
  Quality Density, Revisionsreaktivierung und Cascade-Integrität sind automatisiert getestet.
- Der HTTP-Ablauf Erstellen, Detail anzeigen und explizit annehmen ist als Routentest abgedeckt.
- Frische und historische Datenbanken bestehen Upgrade, `alembic check`, Integrity Check und
  Foreign-Key Check.
- `uv run pytest -q`: 391 passed.
- `uv run pytest tests/test_migrations.py -q`: 16 passed.
- `uv run ruff check .`, `uv run ruff format --check .` und `uv run ty check`: passed.
- Lokale Datenbank: Alembic `20260826_31`, `alembic check` ohne Abweichungen,
  `PRAGMA foreign_key_check` leer und `PRAGMA integrity_check` = `ok`.

## Explicitly Deferred

- Automatische Replanung aus Feedback oder ausgeführten Einheiten.
- Batch Accept und Batch Garmin Sync.
- Neue Intervall-Templates und Zielzeit-/Pace-Steuerung ohne belastbare Anker.
- Agent-Browser-, visuelle Responsive- und Accessibility-Prüfung.
