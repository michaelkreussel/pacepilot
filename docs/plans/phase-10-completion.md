# Phase 10 - Daily Adaptation - Completion

Stand: 26. August 2026
Feature Flag: `COACH_DAILY_ADAPTATION_ENABLED` (Standard: aus; Freigabe pro Umgebung nach
dokumentiertem UI-Pass in `docs/plans/ai-coach-phase-10-gate-matrix.md`).

## Umfang

Umgesetzt sind die vier initialen Adaptationsklassen aus dem Masterplan:

- `KEEP`: bestaetigt die heutige Ausfuehrung ohne neue Revision (Audit-Event `adapt_keep`,
  atomarer CAS auf `lock_version` plus Termin- und Revisionszeiger).
- `REDUCE_VOLUME`: gleichmaessige Reduktion aller Zeit-/Distanz-Endbedingungen (Registry-Regel
  `ADAPT-VOLUME-REDUCTION-001`, Faktor 0.75, begrenzt durch das verfuegbare Zeitbudget) als neue
  immutable Revision desselben Workouts.
- `REPLACE_WITH_EASY`: zeitbasierter Easy Run (RPE 2–3, Sprechtest, 20–45 Minuten, nur bei
  belastbar vergleichbarer Originallast) als **echte Ersetzung**: separates `Workout` mit
  `replaces_workout_id`, eigener Revision 1 ohne Cross-Workout-Parent und eigenem Garmin-Binding.
- `REST`: entfernt nur die lokale Kalenderausfuehrung (`cancelled`), bewahrt angenommene Revision,
  Audit und Garmin-Remote-Inhalt; kein Fake-Workout.

`REDUCE_INTENSITY`, `SIMPLIFY_QUALITY`, `DEFER_KEY_SESSION` und `REPLAN_WEEK` bleiben gesperrt,
bis der Week-Context-Validator harte Tage, Long Runs und Key Sessions klassifiziert.

## Architektur

- Deterministischer Kern: `app/services/planning/daily_adaptation.py`
  (`generate_daily_adaptation_candidates`, `reduce_volume`, `adaptation_load`,
  `DailyAdaptationService.assess_today/apply`). Kein LLM beteiligt.
- Same-Workout-Kandidaten sind normale `WorkoutRevision`-Eintraege mit
  `source_type=coach_daily_adaptation`, Parent-Zeiger auf die akzeptierte Revision und
  Provenienz (Baseline-, Safety-, Recovery-, Wochen-Fingerprints, Before/After, Week-Impact) in
  `generation_context_json`.
- Ersetzungen sind eigene `Workout`-Aggregate: `propose_adaptation_replacement()` erstellt das
  Ersatz-Workout mit `replaces_workout_id`, CAS auf das Original und Audit-Event
  `adapt_replace_propose` inklusive `replacement_workout_id`. Verwerfen setzt nur den Ersatz auf
  `rejected` und erlaubt einen neuen Versuch; das Original bleibt unveraendert eingeplant.
- Annahme einer Ersetzung prueft den eingefrorenen Original-Kontext (Workout-ID, Revisionsidentitaet,
  Termin) und tauscht den Termin in einer Transaktion: Original `cancelled`/`superseded`,
  Ersatz `accepted` und fuer den Originaltermin eingeplant. Audit: `unschedule`, `supersede`,
  `accept`.
- Commands liegen im bestehenden `WorkoutService`
  (`propose_adaptation_revision`, `propose_adaptation_replacement`, `record_adaptation_keep`,
  `apply_adaptation_rest`, `discard_adaptation_revision`) mit CAS auf `lock_version` plus
  Revisionsidentitaet und Replay-sicherer Idempotenz (auch nach verlorener CAS entscheidet das
  Audit-Event, nicht der Fehler).
- Verworfene Kandidaten blockieren keine neuen Revisionsnummern mehr: die naechste Revision
  wird aus `max(revision_number)` abgeleitet, Replay prueft zusaetzlich `adaptation_class`.
- Erneute exakte Annahme: `accept()` verlangt bei Adaptationsrevisionen den eingefrorenen
  Trainingstag und einen frischen Adaptations-Kontextfingerprint (`adaptation.context_stale`)
  und schreibt eine eigene Validation Run (`daily_adaptation_acceptance`).
- Zeitbudgets (`available_minutes`) stammen ausschliesslich aus heutigem Feedback zum
  Ziel-Workout; Feedback zu anderen oder zukuenftigen Workouts fliesst nicht mehr ein.
- Garmin bleibt der einzige Sync-Pfad: Annahme setzt bekannten Remote-Inhalt auf `pending`;
  `publish()` aktualisiert die bekannte Remote-Identity per Update, erzeugt nie eine zweite Kopie;
  unbekannte Zustaende blockieren (`garmin.state_unknown`).
- Ersetzungs-Sync: `publish()` entfernt zuerst den alten Garmin-Kalendereintrag des Originals
  (mit Reconciliation) und laedt danach den Ersatz als neues Upload hoch. Original-Content bleibt
  in der Garmin-Bibliothek, zwei Bindings/Remote-Identities bleiben getrennt.
  Ein Safety Stop blockiert nur Content-Uploads, nie das Entfernen eines alten Kalendereintrags.
- Loeschen des Originals ist blockiert (`adaptation.replacement_active`), solange ein Ersatz
  `proposed` oder `accepted` ist; die UI zeigt „Löschen blockiert“.
- Routen: `POST /workouts/{id}/adaptation/apply` und `/adaptation/discard`; user-scoped,
  CSRF-pflichtig, nur bei aktivem Flag, nicht-leere Idempotency-Keys (422 bei Verstoss).
  Detailseite zeigt Kandidatenkarten mit Vorher/Nachher, Wocheneffekt, eindeutigen
  Button-Labels (`role`-spezifisch), Empfehlung, Verwerfen offener Anpassungen, Ersatz-Banner
  mit Link zum Original und `role="alert"`-Fehlerausgabe. Bearbeiten ist fuer
  Anpassungsrevisionen und Original mit aktivem Ersatz ausgeblendet.
- Alle Adaptations-Events tragen die HTTP-Request-ID (`request_id` wird durch
  `DailyAdaptationService` an `WorkoutService` weitergereicht).

## Safety

- Safety Stop erlaubt nur `REST`; CLARIFY liefert keinen ausfuehrbaren Kandidaten.
- Positive Wearable-Werte oder Motivation koennen Stop oder Klaerung nicht ueberstimmen.
- Bei Warnungen (leichte Krankheit, Schmerz) werden nicht vergleichbare oder weiterhin harte
  Kandidaten nicht empfohlen; konservativer Fallback ist `REST`.
- Kein Kandidat erhoeht irgendeine Lastdimension (`ADAPT-NO-ESCALATION-001`).

## Tests

`tests/test_daily_adaptation.py` deckt Determinismus, No-Escalation (Hypothesis), V1/V2-Reduktion,
Distanz-Workouts mit Load Estimate, Easy-Ersatz-Grenzen, Eligibility, Kontext-Freshness inklusive
Apply-Conflict, Ziel-Workout-Feedback-Grenzen, Revision/Audit/Replay, Verwerfen und erneute
Adaptation (Revisionsnummer 3 nach Verwerfen), KEEP/REST mit CAS, echte Ersetzung
(separates Aggregate, atomarer Terminwechsel, Replay, Verwerfen), Ersatz-Sync ohne Doppel-Kopie,
Safety-Rest mit Remote-Termin, Request-ID-Weitergabe, Original-Schutz, Garmin-Update-in-place,
unbekannten Remote-Zustand und Routen-/Flag-/Cross-User-Verhalten ab.

Ergebnis bei Abschluss: 339 Tests gruen, Ruff Check/Format und `ty check` gruen.

## Zugeordnete Korrektur ausserhalb von Phase 10

- Easy-Run-Chatvorschlaege respektieren die gewuenschte Dauer bis 90 Minuten statt sie auf
  45 Minuten zu kappen (`easy-run-candidate-v2`, Regressionstests auf Service-, Chat-Tool- und
  Template-Ebene). Phase-8-Dokumentation wurde entsprechend aktualisiert.

## Bewusst nicht Teil von Phase 10

- Verschieben von Key Sessions und Wochen-Replanung (erfordert Week-Context-Validator).
- Kilometerbasierte Easy Runs im Chat, Coach-Tool fuer Adaptationen, LLM-Erklaerschicht.
- Garantiertes Entfernen einer bereits auf die Uhr gepushten Kopie (Garmin bietet keinen
  Unpush; REST/Ersatz dokumentieren `device_delivery_may_persist`).
- Datenbankseitige Eindeutigkeit von Ersetzungen (Service-CAS erzwingt sie; eine Migration mit
  partiellem Unique Index bleibt Phase 13 vorbehalten).
