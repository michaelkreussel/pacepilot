# AI Coach Implementation Masterplan

**Status:** Verbindliche Planungsgrundlage  
**Stand:** 20. August 2026  
**Research-Quelle:** `docs/research/garmin-ai-running-coach-consolidated-report.md`  
**Ausgangsbasis:** Repository-Audit und gruene Tests am 20. August 2026

## 1. Ziel

PacePilot wird schrittweise von einem lesenden AI Health Coach zu einem sicheren,
evidenzbewussten Laufcoach erweitert. Das Zielsystem soll zunaechst einzelne Einheiten und
Tagesanpassungen, danach Wochen- und Mehrwochenplaene fuer 5 km, 10 km, Halbmarathon und
Marathon erstellen koennen.

Die Erweiterung wird in die vorhandene Anwendung integriert. Sie ersetzt weder den manuellen
Workout-Builder noch den bestehenden Garmin-Flow und baut keinen parallelen Coach-Stack auf.

Der erste produktive vertikale Slice lautet:

```text
bestehende Athletendaten
-> deterministische Baseline
-> deterministischer Easy-Run-Kandidat
-> bestehende WorkoutDefinition
-> kontextbezogene Validierung
-> immutable WorkoutRevision
-> bestehende Preview und bestehender Editor
-> explizite Annahme genau dieser Revision
-> bestehender Garmin-Compiler
-> idempotente Garmin-Synchronisation
```

## 2. Autoritaet und Arbeitsweise

Der Research-Report ist eine wichtige fachliche und architektonische Quelle, aber nicht die
Runtime-Wahrheit. Fuer die Umsetzung gilt folgende Reihenfolge:

1. Aktueller Anwendungscode und aktuelle Alembic-Migrationen.
2. Automatisierte Tests und explizite Runtime-Invarianten.
3. Dieser an das Repository angepasste Masterplan.
4. Der konsolidierte Research-Report.
5. Weitere Konzeptdokumente.

Wissenschaftliche Aussagen aus dem Report werden vor der Uebersetzung in harte Regeln gegen
die Originalquelle geprueft. Eine Quellenreferenz allein macht einen konkreten numerischen
Produktwert nicht zu wissenschaftlicher Evidenz. Produktdefaults werden als Heuristik
gekennzeichnet und versioniert.

Jede Phase wird in kleinen, reversiblen Aenderungen umgesetzt. Eine Phase darf erst beginnen,
wenn die Exit-Kriterien ihrer Voraussetzungen erfuellt sind. Das Repository muss nach jedem
integrierbaren Schritt deploybar und der bestehende manuelle Workflow funktionsfaehig bleiben.

## 3. Bestehende Bausteine, die verbindlich wiederverwendet werden

### 3.1 Workout-Domain

`app/services/planning/workout_definition.py` ist bereits der kanonische Workout-Kern:

- `WorkoutDefinition`
- `StepBlock` und `RepeatBlock`
- Zeit- und Distanz-Endbedingungen
- Pace-, Herzfrequenz- und Zonen-Ziele
- `validate_definition()`
- `workout_metrics()`
- JSON-Parsing und Serialisierung

Die im Research-Report beschriebene `WorkoutPrescription` wird daher nicht als zweites
Schrittmodell eingefuehrt. `WorkoutDefinition` wird kontrolliert und ueber
`definition_version` weiterentwickelt. Coach, Manual, Daily Adaptation und Plan Generator
erzeugen dasselbe Modell.

### 3.2 Workout-UI

Folgende vorhandene Oberflaechen werden generalisiert, nicht kopiert:

- `app/templates/workouts/form.html`
- `app/templates/workouts/_step_fields.html`
- `app/templates/workouts/detail.html`
- `app/static/js/workout-builder.js`

Die rekursive Workout-Darstellung wird in ein gemeinsames Partial extrahiert. Proposal,
manueller Entwurf und angenommene Einheit erhalten konfigurierbare Aktionen, aber keine
unterschiedlichen Editoren.

### 3.3 Workout-Validierung

`app/services/planning/validator.py` und `validate_definition()` bleiben die erste
Validierungsschicht. Sie pruefen strukturelle Korrektheit und Garmin-nahe Darstellbarkeit.

Athletenbezogene Eignung, Safety, Progression, Belastung und Wochenkonflikte werden als zweite,
deterministische Validierungsschicht ergaenzt. Die zweite Schicht ersetzt die bestehende
Validierung nicht.

### 3.4 Garmin

Folgende vorhandene Implementierungen bleiben der einzige Garmin-Pfad:

- `app/services/garmin/client.py`
- `app/services/garmin/locks.py`
- `app/services/garmin/workout_export.py`
- bestehende Activity-Reconciliation ueber `Activity.workout_id`

Der private Compiler in `workout_export.py` wird zu einer expliziten internen Schnittstelle.
Upload, Schedule, Unschedule, Update, Push und Delete werden hinter einem gemeinsamen
Application Service orchestriert. Es entsteht kein Coach-spezifischer Garmin-Compiler.

### 3.5 Athletendaten

`app/services/analytics/athlete_data.py` bleibt die user-scoped Leseschnittstelle fuer UI,
Coach, Baseline und Generator. Bestehende Tabellen fuer Aktivitaeten, Health, Fitness,
Sync-Abdeckung und Workouts werden erweitert oder ausgewertet. Es entstehen im MVP keine
parallelen Activity-, Wellness- oder Garmin-Snapshot-Stores.

### 3.6 Coach

Der bestehende LangChain-Agent, seine Conversation-Persistenz, das SSE-Streaming und die sieben
read-only Tools werden erweitert. Es entsteht kein zweiter Planungsagent.

Der Agent darf Intent und subjektives Feedback interpretieren, Rueckfragen stellen und
validierte Ergebnisse erklaeren. Er darf keine Trainingsregeln, Revisionen, Zustimmungen oder
Garmin-Payloads selbst erfinden.

## 4. Verbindliche Architekturentscheidungen

### 4.1 `Workout` bleibt die stabile Aggregate-Identitaet

Ein `Workout` bleibt die lokale Identitaet fuer:

- Ownership
- Kalenderbezug
- Workout-Historie
- Garmin-Verknuepfung
- spaetere Zuordnung einer ausgefuehrten `Activity`

Es wird kein permanenter `WorkoutProposal`-Datensatz erzeugt, der bei Annahme in ein zweites
`Workout` kopiert wird. Ein vorgeschlagenes Workout ist ein `Workout` mit Proposal-Status und
immutable Revisionen. Dadurch bleiben Manual und Coach in einem Lifecycle.

### 4.2 Inhalte werden revisioniert

Jede Erstellung oder Bearbeitung erzeugt eine immutable `WorkoutRevision`. Eine Revision
enthaelt den vollstaendigen, reproduzierbaren Kandidaten einschliesslich der vorhandenen
`WorkoutDefinition`.

`Workout.current_revision_id` bezeichnet den aktuell angezeigten Kandidaten.
`Workout.accepted_revision_id` bezeichnet ausschliesslich die konkret angenommene Revision.

`approval_status` beschreibt immer den Status der aktuellen Revision. Deshalb kann ein Workout
gleichzeitig `approval_status=proposed` und eine aeltere `accepted_revision_id` besitzen. In
diesem Fall ist die neue Revision Kandidat, waehrend die aeltere angenommene Revision weiterhin
die einzige ausfuehrbare Version bleibt.

Eine Bearbeitung mutiert keine bestehende Revision. Bei einer Bearbeitung nach Annahme bleibt
die alte angenommene Revision ausfuehrbar, bis die neue Revision erneut explizit angenommen
wurde.

### 4.3 Eindeutige Leseautoritaet fuer Kandidat und Ausfuehrung

Die bestehenden Spalten `name`, `sport`, `description`, `definition_version` und `definition`
bleiben waehrend eines begrenzten Migrationsfensters als Kompatibilitaetsprojektion erhalten.
Neue Domainlogik darf nach Phase 3 nicht mehr implizit aus diesen Feldern lesen.

Die Autoritaet ist verbindlich:

| Consumer | Autoritative Datenquelle |
|---|---|
| Proposal Card und Editor | `current_revision_id` |
| Diff | `accepted_revision_id` gegen `current_revision_id` |
| Garmin-Compiler und Remote Update | ausschliesslich `accepted_revision_id` |
| lokaler Kalender | `Workout.scheduled_for` und `local_schedule_status` |
| Upcoming-Workout-Analytics | lokaler Kalender plus angenommene Revision |
| Activity-Verknuepfung | stabile `Workout.id` plus Remote-Identity-Historie |

Die Kompatibilitaetsspalten projizieren die angenommene Revision, wenn eine existiert, sonst die
aktuelle Revision. `materialized_revision_id` und ein kanonischer Content-Hash machen Drift
erkennbar. Neue Read Models liefern dem Editor trotzdem explizit die aktuelle Revision. Der
Garmin-Compiler erhaelt explizit die angenommene Revision und verlaesst sich nicht auf die
Projektion.

Phase 3 ist erst beendet, wenn Editor, Detail, Kalender, `AthleteDataService` und Garmin-Compiler
ueber explizite Read Models beziehungsweise Revisionen lesen. Direkte Route-Mutationen werden
entfernt.

### 4.4 Statusdimensionen werden getrennt

Der bisherige String `Workout.status` vermischt Zustimmung, Garmin-Upload und Device-Push. Das
Zielmodell trennt:

```text
approval_status:
  draft | proposed | accepted | rejected | expired | superseded

local_schedule_status:
  unscheduled | scheduled | cancelled

garmin_content_status:
  not_requested | pending | synced | retryable | unknown | failed_final | removed

garmin_calendar_status:
  not_requested | pending | synced | retryable | unknown | failed_final | removed

garmin_device_status:
  not_requested | pending | request_accepted | retryable | unknown | failed_final
```

`request_accepted` bedeutet nur, dass Garmin den Push-Request angenommen hat. Die aktuelle API
beweist weder die Zustellung auf ein bestimmtes Geraet noch die Ausfuehrung auf der Uhr.

Die alten Werte `draft`, `confirmed`, `published` und `pushed` bleiben nur waehrend eines
begrenzten Kompatibilitaetsfensters als Projektion erhalten.

### 4.5 Annahme und Scheduling bleiben fachlich getrennt

Annahme bestaetigt eine konkrete Workout-Revision. Scheduling setzt einen lokalen Kalendertag.
Garmin-Sync uebertraegt den angenommenen Inhalt und optional den geplanten Tag.

Scheduling erfordert immer eine explizit angegebene Revision, die exakt
`Workout.accepted_revision_id` entspricht. Existiert gleichzeitig eine neuere aktuelle
Proposal-Revision, muss die UI sichtbar machen, dass sich der Schedule Command auf die aeltere
angenommene Revision bezieht. Eine unangenommene Revision kann nicht lokal terminiert werden.

Die UI darf spaeter eine klar beschriftete kombinierte Aktion anbieten, der Server fuehrt aber
getrennte, auditierbare Commands aus. Eine Chat-Antwort allein ist niemals Zustimmung.

### 4.6 LLM und deterministische Engine

Deterministisch in Python:

- Baseline und Datenqualitaet
- Intensitaetsmodell und Einheitenkonvertierung
- Template-Auswahl und Parametrisierung
- Load Estimate
- Safety Stops
- Progression und Quality Density
- Wochenkonflikte
- State Transitions
- Revisionierung und Annahme
- Garmin-Compilation, Idempotenz und Reconciliation

Aufgabe des LLM:

- Intent und Freitext verstehen
- entscheidende fehlende Angaben erkennen
- gezielt nachfragen
- subjektives Feedback in ein striktes Schema ueberfuehren
- ausschliesslich validierte Kandidaten erklaeren
- Unterschiede und Unsicherheit verstaendlich formulieren

### 4.7 AI-Scope

Die automatische Trainingslogik unterstuetzt zunaechst ausschliesslich Laufen. Manuelle
Workouts fuer Cycling, Walking und Hiking bleiben unveraendert verfuegbar.

### 4.8 Ein-Prozess-Runtime bleibt bestehen

Der Plan fuehrt keine Worker, Microservices, Queue-Plattform oder verteilten Locks ein. Die
dokumentierte Ein-Prozess-Architektur mit SQLite, APScheduler und account-scoped Locks bleibt
erhalten. Persistente Idempotenz wird trotzdem eingefuehrt, weil auch ein Prozess abstuerzen
oder einen externen Timeout erleben kann.

### 4.9 Legacy-Backfill ist explizit und konservativ

Der Backfill wertet immer die Kombination aus Legacy-Status, Remote-ID und Datum aus. Es wird
kein Remote-Erfolg erfunden.

| Legacy-Zustand | Approval | Lokales Datum | Remote-Zustand |
|---|---|---|---|
| `draft`, keine Remote-ID | keine Annahme | nach Revision `suggested_for`, lokal unscheduled | not requested |
| `draft`, Remote-ID | keine Annahme | nach Revision `suggested_for`, lokal unscheduled | unknown, mutation blockiert |
| `confirmed`, keine Remote-ID | Revision 1 angenommen | Datum bleibt scheduled | content not requested |
| `confirmed`, Remote-ID | Revision 1 angenommen | Datum bleibt scheduled | unknown, Review erforderlich |
| `published`, Remote-ID | Revision 1 angenommen | Datum bleibt scheduled | content synced, Calendar bei Datum unknown |
| `pushed`, Remote-ID | Revision 1 angenommen | Datum bleibt scheduled | content synced, Calendar unknown, Push request accepted |
| `published`/`pushed`, keine Remote-ID | Revision 1 angenommen | Datum bleibt scheduled | unknown, mutation blockiert |

Ein datierter Legacy-Draft verliert nicht seine Datumsinformation; sie wird als Vorschlag in
Revision 1 erhalten. Er wird aber nicht als bereits angenommener lokaler Termin interpretiert.
Revisionen und die minimalen Garmin-Bindings werden deshalb in demselben Release migriert.

## 5. Zielarchitektur

```text
Garmin Sync und vorhandene normalisierte Tabellen
                   |
                   v
          AthleteDataService
                   |
                   v
       RunningBaselineService
                   |
          +--------+---------+
          |                  |
          v                  v
  PerformanceModel      SafetyContext
          |                  |
          +--------+---------+
                   v
       Deterministic Generator
                   |
                   v
       Contextual WorkoutValidator
                   |
                   v
 Workout + immutable WorkoutRevision
                   |
       +-----------+-----------+
       |                       |
       v                       v
 Shared Preview/Editor    LangChain explanation
       |
       v
 Exact Revision Acceptance
       |
       v
 Local Scheduling Command
       |
       v
 Existing Garmin Compiler
       |
       v
 Garmin Binding + Sync Operations
       |
       v
 Activity Reconciliation + Feedback
```

Der LangChain-Agent sitzt seitlich an dieser Pipeline. Er ist nicht deren Orchestrator und kann
keine Stufe ueberspringen.

## 6. Ziel-Datenmodell

### 6.1 Erweiterung `Workout`

Geplante additive Felder:

```text
source_type
approval_status
local_schedule_status
current_revision_id
accepted_revision_id
materialized_revision_id
accepted_at
accepted_by_user_id
expires_at
lock_version
replaces_workout_id
originating_conversation_id
originating_user_message_id
originating_assistant_message_id
deleted_at
```

Die vorhandenen Workout-Spalten bleiben zunaechst bestehen. `definition_version` bleibt die
Version des JSON-Schemas und wird nicht als Content-Revision oder Lock-Version missbraucht.
Fuer Same-User-Referenzen erhaelt `Workout` zusaetzlich einen Candidate Key auf `(id, user_id)`.

### 6.2 `WorkoutRevision`

Eine Revision speichert mindestens:

```text
id
workout_id
revision_number
parent_revision_id
name
sport
suggested_for
description
definition_version
definition
purpose
guidance_json
load_estimate_json
validation_report_json
generation_context_json
source_type
generator_version
template_id
template_version
rule_set_version
knowledge_base_version
model_provider
model_id
prompt_template_version
content_hash
edit_source
created_at
```

`(workout_id, revision_number)` ist eindeutig. Revisionen werden nicht aktualisiert. Der
Content-Hash wird ueber eine kanonische Serialisierung aller zustimmungsrelevanten Inhalte
gebildet.

Fuer zusammengesetzte Same-Workout-Foreign-Keys besitzt die Revision ausserdem einen Candidate
Key auf `(id, workout_id)`.

`generation_context_json` friert die fuer die Erzeugung verwendeten kompakten Inputs ein:
`as_of`, Quellenzeitpunkte, Coverage, Confidence, relevante Feedback-IDs, Einheiten und einen
Input-Fingerprint. Es enthaelt keine Garmin-Rohpayloads.

### 6.3 `WorkoutValidationRun`

Strukturelle Validierung ist Bestandteil der immutable Revision. Zeitabhaengige kontextuelle
Validierung wird als eigener append-only Run gespeichert:

```text
workout_id
revision_id
validation_kind
rule_set_version
context_fingerprint
feedback_ids_json
evaluated_at
expires_at
valid
report_json
```

Kontextuelle Validierung laeuft bei Erzeugung und Annahme erneut. Vor einem verzoegerten Garmin-
Sync werden mindestens Approval, Schedule-Konflikte und neuere Safety Stops geprueft. Fuer
zukuenftige Planworkouts ist kein taegliches subjektives Feedback erforderlich; fuer ein
heutiges Workout blockiert ein neuerer Safety Stop jedoch den Sync. Die genaue Freshness Policy
ist typisiert, getestet und versioniert.

### 6.4 `WorkoutEvent`

Append-only Audit Events speichern:

```text
workout_id
revision_id
owner_user_id
actor_type
actor_user_id
action
request_id
idempotency_key
safe_metadata_json
created_at
```

Aktionen umfassen mindestens Create, Propose, Revise, Accept, Reject, Schedule, Unschedule,
Supersede und Delete. Sensitive Health- oder Freitextdaten gehoeren nicht in Audit-Metadaten.
Nicht-leere Idempotency Keys sind ueber einen partiellen Unique Index auf
`(owner_user_id, action, idempotency_key)` eindeutig. `owner_user_id` ist nicht mit dem optionalen
Actor gleichzusetzen.

Normales Loeschen wird nach dieser Migration als Tombstone ueber `deleted_at` umgesetzt, damit
Audit und Revisionshistorie erhalten bleiben. Nur eine vollstaendige Account-Loeschung entfernt
das Aggregate hart. Optionale Coach-Message-Referenzen verwenden `ON DELETE SET NULL`.

### 6.5 `WorkoutGarminBinding` und Remote-Identity-Historie

Ein Binding pro Workout speichert die aktuelle Remote-Identitaet und getrennte Zustandsachsen:

```text
workout_id
active_remote_identity_id
content_status
calendar_status
device_status
remote_scheduled_for
last_attempt_at
last_success_at
last_error_code
last_error_message
```

`WorkoutGarminRemoteIdentity` erhaelt jede jemals bekannte Garmin-Workout-ID mit Active-/Removed-
Status und Zeitstempeln. Ein Re-Upload ueberschreibt keine alte ID. Activity-Reconciliation
durchsucht die Remote-Identity-Historie des Users, damit spaeter importierte Aktivitaeten auch
nach Update, Delete oder Re-Upload zugeordnet werden koennen.

Es existiert hoechstens ein Binding pro Workout. Eine Garmin-Workout-ID ist innerhalb eines
Garmin-Accounts eindeutig und kann nur einer Remote Identity zugeordnet sein. Deshalb speichert
jede Remote Identity `garmin_account_id` und unterliegt
`UNIQUE(garmin_account_id, garmin_workout_id)`.

### 6.6 `WorkoutGarminOperation` und `WorkoutGarminAttempt`

Jeder logisch mutierende Garmin-Command wird vor dem Netzwerkaufruf persistent erfasst:

```text
binding_id
operation_type
revision_id
remote_identity_id
scheduled_for
idempotency_key
status
remote_reference
completed_at
error_code
```

Eine `WorkoutGarminOperation` besitzt einen Unique Constraint auf dem serverseitig abgeleiteten
Idempotency Key. Jeder echte Netzwerkversuch wird als append-only `WorkoutGarminAttempt` mit
Start, Ende, Ergebnis und sicherem Fehler erfasst. Die Operation wird vor dem Netzaufruf
committed.

Moegliche Operations sind Upload, Update, Schedule, Unschedule, Push und Delete. Startup Repair
markiert verwaiste laufende Attempts als `unknown`, analog zur bestehenden Sync-Reparatur.
Operation-spezifische Reconciliation entscheidet danach ueber einen Retry. Upload oder Push
werden nach unklarem Ergebnis nicht automatisch wiederholt, solange die Abwesenheit der ersten
Remote-Wirkung nicht bewiesen werden kann.

`remote_identity_id` ist nur beim initialen Upload null. Jede andere Operation bindet immutable
die konkrete Remote Identity, die sie pruefen oder mutieren soll; ein spaeterer Wechsel der
aktiven Identity veraendert alte Commands nicht.

### 6.7 Feedback

Spaetere additive Modelle:

- `PreSessionFeedback`
- `PostSessionFeedback`

Sie referenzieren den User und optional Workout, Revision, Activity und Coach Message. Garmin
RPE und Garmin Workout Feel auf `Activity` bleiben separate Quellen und werden nicht in
subjektives PacePilot-Feedback kopiert. Ein gemeinsames Read Model priorisiert pro Feld einen
vorhandenen Garmin-Wert und verwendet manuelle Aktivitaetswerte nur als Fallback.

### 6.8 Planmodelle

Planning Inputs entstehen fuer den Shadow-Wochenplan. Persistente Planmodelle entstehen erst,
wenn dieser Shadow-Plan seine Gates bestanden hat:

- `AthletePlanningProfile`
- mehrere versionierbare `AthleteGoal`-Eintraege
- `AthleteAvailability`
- `PerformanceAnchor`
- `TrainingPlan`
- immutable `TrainingPlanRevision`
- Plan-zu-Workout-Mitgliedschaft

Ein Plan referenziert normale Workouts. Er speichert keine zweite Kopie ihrer Definitionen.

## 7. Kerninvarianten

1. Jede Revision gehoert genau einem user-scoped Workout.
2. Revisionen sind immutable und fortlaufend nummeriert.
3. Nur `accepted_revision_id` darf fuer Garmin kompiliert werden.
4. Annahme nennt Revision-ID, Revisionsnummer, Hash und erwartete `lock_version`.
5. Eine veraltete Annahme endet mit `409 Conflict`.
6. Bearbeitung nach Annahme erzeugt eine neue Revision und keine versteckte Remote-Mutation.
7. Garmin-Fehler machen eine erfolgreiche Annahme nicht rueckgaengig.
8. Lokales Scheduling beweist weder Garmin-Scheduling noch Device-Push.
9. Kein positiver Wearable-Wert kann einen Safety Stop aufheben.
10. Daily Adaptation erhoeht standardmaessig keine geschaetzte Last.
11. Der LLM-Text ist niemals Quelle eines ausfuehrbaren Workouts.
12. Der LLM kann weder Annahme noch Garmin-Erfolg behaupten, wenn der Application Service dies
    nicht bestaetigt hat.
13. `Activity.workout_id` bleibt ueber Revisionen hinweg stabil.
14. Alle Workout-Lese- und Schreiboperationen bleiben user-scoped.
15. Keine Mutation schreibt neue `WorkoutStep`-Zeilen.
16. Revisionspointer und Parent-Revisionen muessen per zusammengesetztem Foreign Key zum selben
    Workout gehoeren.
17. `replaces_workout_id` darf per zusammengesetztem User-/Workout-Key keinen fremden User
    referenzieren.
18. Kontextuelle Validierung wird bei Annahme und bei relevanter Veraltung erneut ausgefuehrt.
19. Lokales Scheduling nennt und verwendet ausschliesslich die exakt angenommene Revision.

## 8. Implementierungsphasen

### Phase 0 - Ausgangsbasis und Dokumentation

**Ziel:** Reproduzierbare, dokumentierte Basis vor funktionalen Aenderungen.

Arbeiten:

- Research-Report unveraendert unter `docs/research/` versionieren.
- Diesen Masterplan versionieren.
- Aktuellen Test-, Lint-, Format- und Type-Status dokumentieren.
- Research-Rohdokumente vom automatischen Codeblock-Formatter ausnehmen.
- Widersprueche zwischen Research und Runtime als ADRs dokumentieren.

Exit-Kriterien:

- Vollstaendige bestehende Suite ist gruen.
- Research-Report und Plan brechen keine Quality Gates.
- Keine Runtime-Aenderung.

### Phase 1 - Sicherheitsbasis und Characterization Tests

**Ziel:** Bestehende Mutationen absichern und Verhalten vor Refactoring einfrieren.

Arbeiten:

- Session-gebundene CSRF-Tokens fuer alle unsafe HTTP-Methoden einfuehren.
- JavaScript-/SSE-POSTs senden denselben CSRF-Schutz.
- Optional Origin/Referer-Pruefung als Defense in Depth.
- Request-ID fuer mutierende Workflows einfuehren.
- Grobe, serverseitige Feature Flags mit sicheren Defaults einfuehren:
  `COACH_WORKOUT_PROPOSALS_ENABLED`, `COACH_GARMIN_SYNC_ENABLED`,
  `COACH_DAILY_ADAPTATION_ENABLED`, `COACH_PLAN_GENERATION_ENABLED`.
- Coach-Flags gelten nur fuer Coach-Quellen und schalten den bestehenden manuellen Garmin-Flow
  nicht ab.
- Characterization Tests fuer WorkoutDefinition V1 und alle Status-/Action-Kombinationen.
- Cross-User-Tests fuer Workout-Details und Mutationen.
- Garmin-Golden-Tests fuer alle vier manuell unterstuetzten Sportarten.
- Bestehende problematische Kanten explizit erfassen: Edit nach Confirm, wiederholtes Publish,
  wiederholtes Push, inkonsistenter Remote-ID-/Status-Zustand.

Wiederverwendung:

- Bestehende Formulare, Session Middleware und Testclients.
- Bestehende Garmin-Fakes.

Exit-Kriterien:

- Fehlende oder ungueltige CSRF-Tokens werden vor jeder DB-/Garmin-Wirkung abgewiesen.
- Legitime bestehende Formulare funktionieren unveraendert.
- Unsicheres Bestandsverhalten ist charakterisiert, aber nicht als kuenftige Soll-Invariante
  festgeschrieben.
- Alle Quality Gates sind gruen.

### Phase 2 - Gemeinsamer Workout Application Service

**Ziel:** Route-eigene Businesslogik zentralisieren, bevor der Coach sie nutzen kann.

Arbeiten:

- User-scoped `WorkoutService` unter `app/services/planning/` einfuehren.
- Create, Update, Validate, Confirm, Delete und spaeter Accept als Commands modellieren.
- Garmin-Orchestrierung aus `app/routes/workouts.py` in einen gemeinsamen Service verschieben.
- Formularparsing bleibt HTTP-Adapter; es wird nicht vom Coach importiert.
- Den vorhandenen Garmin-Compiler als explizite Funktion freigeben.
- Strukturelle Validierungsfehler erhalten stabile Codes und behalten deutschen UI-Text.
- Bestehende Routen werden duenne Adapter auf denselben Service.

Nicht enthalten:

- AI-Generation
- neues Workout-Schema
- neue UI
- Aenderung der Garmin-Payloads

Exit-Kriterien:

- Manual Create/Edit/Confirm/Publish/Push/Delete verhalten sich wie zuvor, ausser gezielt
  behobenen und dokumentierten Sicherheitskanten.
- Routen enthalten keine duplizierte Status- oder Garmin-Orchestrierung.
- Garmin-Golden-Payloads sind unveraendert.

### Phase 3 - Immutable Revisionen und exakte Annahme

**Ziel:** Manual und kuenftige Coach-Vorschlaege erhalten denselben sicheren Lifecycle.

Migration:

- `WorkoutRevision`
- `WorkoutValidationRun`
- `WorkoutEvent`
- `WorkoutGarminBinding` und minimale `WorkoutGarminRemoteIdentity`
- additive Revisions-, Source-, Approval-, Schedule- und Lock-Felder auf `Workout`

Arbeiten:

- Fuer jedes bestehende Workout Revision 1 aus den aktuellen Feldern erzeugen.
- Vollstaendige Backfill-Matrix aus Abschnitt 4.9 fuer Status, Remote-ID und Datum anwenden.
- Datierte Drafts behalten das Datum als `suggested_for`, werden aber lokal unscheduled.
- Widerspruechliche Legacy-Zustaende werden als `unknown` gespeichert und fuer Mutationen
  blockiert, nicht nur geloggt.
- Zusammengesetzte Foreign Keys erzwingen, dass Current-, Accepted-, Materialized- und
  Parent-Revision zum selben Workout gehoeren.
- Zusammengesetzter User-/Workout-Key verhindert Cross-User-`replaces_workout_id`.
- Neue Erstellung und jede Bearbeitung laufen ueber den Revision Service.
- Annahme wird transaktional mit Compare-and-Swap auf `lock_version` umgesetzt.
- Bestehendes Confirm wird zum Manual-Adapter auf denselben Accept Command.
- Nach Bearbeitung eines angenommenen Workouts bleibt die alte Revision aktiv, bis die neue
  angenommen wurde.
- Strukturelle Validierung einfrieren und die Validation-Run-/Freshness-Infrastruktur mit den zu
  diesem Zeitpunkt vorhandenen Validatoren aufbauen. Safety Triage wird erst in Phase 6
  angeschlossen.
- Workout-Delete auf Tombstone umstellen; Account-Erasure bleibt der harte Delete-Pfad.
- Gemeinsame Preview als Jinja-Partial extrahieren.
- Bestehenden Editor mit variablen Save-/Accept-Aktionen wiederverwenden.
- Source Badge, Revisionsnummer, Warnungen und Diff anzeigen.
- Editor, Detail, Kalender, `AthleteDataService` und Compiler auf explizite Revision Read Models
  umstellen.

Wichtige Soll-Aenderung:

- Ein Edit eines angenommenen oder synchronisierten Workouts aktualisiert Garmin nicht mehr
  sofort. Es erzeugt eine neue, erneut anzunehmende Revision.

Exit-Kriterien:

- Veraltete Revision kann nicht angenommen werden.
- Revisionen koennen nicht mutiert werden.
- Nicht angenommene Revision kann nicht publiziert oder gepusht werden.
- Bestehende Workouts werden verlustfrei migriert.
- Kandidat und angenommene Ausfuehrung koennen gleichzeitig korrekt angezeigt werden.
- Ein geaenderter synthetischer Context Fingerprint erzwingt nachweisbar einen neuen Validation
  Run; die echte Safety-Semantik folgt in Phase 6.
- Desktop-, Mobile-, Tastatur- und Accessibility-Checks fuer die geaenderten Screens bestehen.
- `tests/test_migrations.py` prueft frische und befuellte Upgrade-Pfade.

### Phase 4 - Getrennte und idempotente Garmin-Zustaende

**Ziel:** Remote-Wirkungen werden nachvollziehbar, wiederholbar und von Zustimmung getrennt.

Migration:

- `WorkoutGarminOperation`
- `WorkoutGarminAttempt`

Arbeiten:

- In Phase 3 angelegte Bindings und Legacy Remote-IDs vervollstaendigen.
- Content-, Calendar- und Device-Zustand separat fuehren.
- Logische Operation vor dem Netzaufruf committen; jeden Netzwerkversuch separat erfassen.
- Doppelklick und wiederholter Request liefern vorhandenes Ergebnis.
- Account Lock und bestehende Garmin-Verbindung unveraendert wiederverwenden.
- Workout-Writes in das bestehende Garmin-Pacing/Cooldown-Konzept integrieren.
- `unknown` nach Timeout oder Prozessabbruch reconciliieren.
- Startup-/Scheduler-Repair fuer verwaiste `pending` Attempts einfuehren.
- Reconciliation-Capability pro Operation dokumentieren und testen.
- Wenn Garmin keine verlaessliche Reconciliation ermoeglicht, automatische Wiederholung
  blockieren und manuellen Review verlangen.
- Bestehenden Compiler ausschliesslich mit der angenommenen Revision aufrufen.
- Vor verzoegertem Sync Approval und die zu diesem Zeitpunkt vorhandenen Schedule-/Context-
  Validatoren erneut pruefen. Phase 6 schliesst echte Safety Stops an denselben Hook an.
- `activity_backfill.py` auf die Remote-Identity-Historie umstellen.
- Disconnect, Garmin-Datenloeschung und Settings-UI auf die neue Binding-Semantik umstellen;
  keine Datenloeschung darf still einen spaeteren Doppel-Upload ermoeglichen.
- Push-Erfolg als `request_accepted`, nicht als Geraetezustellung darstellen.

Exit-Kriterien:

- Kein Sync ohne exakte angenommene Revision.
- Kein zweiter automatischer Netzwerkversuch, solange die Abwesenheit der ersten Remote-Wirkung
  nicht bewiesen ist.
- Doppelklick mit gleichem Idempotency Key erzeugt nur eine logische Operation.
- Upload-Erfolg plus Schedule-Fehler bleibt getrennt sichtbar.
- Unklarer Remote-Ausgang fuehrt nicht zu blindem Retry.
- Bestehende manuelle Garmin-Funktionen nutzen denselben neuen Service.

### Phase 5 - Running Baseline und Intensitaetsmodell im Shadow Mode

**Ziel:** Reproduzierbare, datenqualitaetsbewusste Inputs ohne Workout-Erzeugung.

Arbeiten:

- Gemeinsame Activity-Klassifikation und Query-Primitiven aus den vorhandenen
  `training_trends.py`-Berechnungen extrahieren, statt sie zu duplizieren.
- `AthleteDataService` um eine laufbezogene Baseline auf diesen Primitiven erweitern.
- Exakte Fenster fuer 7, 28, 56 und 180 Tage berechnen.
- Robuste Lage/Streuung, Frequenz, Dauer, Distanz, Long Run, Unterbrechungen, harte Tage,
  Quality Density und vorhandenes sRPE ableiten.
- Running von anderen Sportarten trennen.
- Abdeckung, Alter, Quellen und Confidence als First-Class-Felder liefern.
- Performance-/Intensity-Service einfuehren.
- Quellenprioritaet: verlaessliche manuelle/Race-Anker, frische Schwelle, konservative
  RPE-/Talk-Test-Fallbacks.
- Garmin Race Predictions, VO2max und Wearable Scores nur als sekundaeren Kontext markieren.
- Noch keine Critical-Speed-Berechnung ohne geeignete Leistungsanker.
- Shadow-Ergebnisse nur in Tests und optionaler Debug-Ansicht ausgeben.

Persistenz:

- Keine Baseline-Snapshot-Tabelle im ersten Schritt.
- Die fuer eine Revision verwendete kompakte Baseline-Zusammenfassung wird als
  `generation_context_json` mit Input-Fingerprint eingefroren.

Exit-Kriterien:

- Ein benannter Sparse-Data-Test beweist, dass fehlende oder veraltete Daten kein Pace-Ziel
  erzeugen.
- Niedrige Confidence fuehrt zu RPE/Talk Test oder Rueckfrage.
- Einzeldistanz-Spike und Unterbrechungen sind aus bestehenden Daten reproduzierbar.
- Tests decken Sparse Data, fehlende HRV, andere Sportarten und Re-Entry ab.

### Phase 6 - Subjektives Feedback und Safety Triage

**Ziel:** Safety-kritische Informationen werden strukturiert und deterministisch behandelt.

Migration:

- `PreSessionFeedback`
- `PostSessionFeedback`

Arbeiten:

- Das aktuelle subjektive Tagesbefinden kommt primaer aus der laufenden Coach-Nachricht und wird
  nicht zusaetzlich als Daily Check-in persistiert. Vorhandene Health-Signale werden automatisch
  geladen; lokalisierter Schmerz, Gait Change und Illness koennen bei Bedarf freiwillig am Workout
  strukturiert erfasst oder spaeter im Coach gezielt geklaert werden.
- Nach einer Aktivitaet werden ausschliesslich Anstrengung 1-10 und Workout Feel 1-5 erfasst.
  Garmin-Werte haben je Feld Vorrang, manuelle Eingabe dient nur als Fallback.
- Deterministische Triage mit `allow`, `clarify`, `warn` und `safety_stop`.
- Red-Flag-Texte ohne Diagnose und mit angemessener professioneller Eskalation.
- Datenexport und Loeschverhalten gleichzeitig implementieren.
- LLM-Extraktion erst danach; unsichere oder mehrdeutige Extraktion wird nicht als Tatsache
  persistiert.
- Wearable-Werte duerfen ausschliesslich nach der Safety Triage wirken.
- Safety Triage an die in Phase 3/4 vorbereiteten Acceptance- und Pre-Sync-Validation-Hooks
  anschliessen. Neueres Feedback invalidiert einen aelteren passenden Context Fingerprint.

Exit-Kriterien:

- Gait-aendernder Schmerz, Fieber und kardiopulmonale Warnsignale blockieren Laufvorschlaege.
- Positive Readiness kann keinen Stop ueberschreiben.
- Unklares "Knie komisch" fordert Rueckfrage.
- Keine Diagnose wird ausgegeben oder gespeichert.
- Neuerer Safety Stop blockiert Annahme beziehungsweise Same-Day-Sync einer aelteren
  kontextuellen Freigabe.

### Phase 7 - Evidence-, Template- und Constraint Registry

**Status:** Abgeschlossen am 24. August 2026. Details und Gates stehen in
`phase-7-completion.md` und `ai-coach-phase-7-gate-matrix.md`.

**Ziel:** Versionierte Fachlogik wird von Prompttext getrennt.

Geplante Struktur:

```text
knowledge/evidence/index.yaml
knowledge/workouts/easy_run.yaml
knowledge/workouts/recovery_run.yaml
knowledge/workouts/long_run.yaml
knowledge/workouts/strides.yaml
knowledge/workouts/threshold_cruise.yaml
knowledge/workouts/vo2_intervals.yaml
knowledge/constraints/safety.yaml
knowledge/constraints/progression.yaml
knowledge/constraints/quality_density.yaml
knowledge/constraints/daily_adaptation.yaml
```

Arbeiten:

- Relevante Claims gegen Originalquellen pruefen.
- Evidence Level, Population, Grenzen und erlaubte Verwendung dokumentieren.
- YAML enthaelt Daten und Parameter, keine ausfuehrbare Ausdruckssprache.
- Einen gepinnten YAML-Parser verwenden, ausschliesslich Safe Load erlauben und jedes Dokument
  unmittelbar gegen ein typisiertes Pydantic-Schema validieren.
- Regelberechnung und Invarianten bleiben typisierter Python-Code.
- `WorkoutDefinition` V2 vor dem ersten Proposal minimal um RPE-Range und lokale
  Schrittanweisungen erweitern; bestehende V1-Definitionen bleiben lesbar.
- RPE/Talk-Test-Inhalte sind Bestandteil des Content-Hash, der gemeinsamen Preview und des
  Editors. Der Garmin-Compiler degradiert sie explizit und mit sichtbarer Warnung auf kein
  Device-Target, solange Garmin sie nicht abbilden kann.
- Zuerst zeitbasierter Easy Run mit RPE/Talk Test.
- Danach Recovery, konservativer Long Run und Strides.
- Threshold und VO2 erst nach belastbarem Intensitaets- und Quality-Density-Testset.
- Load Estimate mit Unsicherheit einfuehren.
- Hypothesis fuer kombinatorische Invarianten als Dev-Dependency aufnehmen.

Exit-Kriterien:

- Gleiche Inputs und Versionen erzeugen deterministisch dasselbe Ergebnis.
- Jede aktive Regel verweist auf Evidenz oder ist als Product Heuristic markiert.
- Daily-Adaptation-Regeln koennen keine Laststeigerung erzeugen.
- Generator und Validator benoetigen kein LLM.
- V1-/V2-Roundtrip, UI-Edit und explizite Garmin-Degradation sind getestet.

### Phase 8 - Erster Proposal-Vertical-Slice ohne LangChain

**Status:** Abgeschlossen am 24. August 2026. Details und Gates stehen in
`phase-8-completion.md` und
`ai-coach-phase-8-gate-matrix.md`.

**Ziel:** Die vollstaendige sichere Pipeline funktioniert vor Agent-Integration.

Arbeiten:

- Typisierten Request fuer Easy Run mit Datumsvorschlag und verfuegbarer Zeit anbieten.
- Baseline, Safety Context, Template, Generator und Validator verbinden.
- Ein valides persoenliches Garmin-Running-HF-Profil als konkreten aeroben BPM-Geraetebereich
  verwenden; Default-Profil nur als Fallback, RPE/Sprechtest immer als lokale Leitplanken.
- Ergebnis als `Workout` plus `WorkoutRevision` im Status `proposed` speichern.
- Initiale strukturelle und kontextuelle Validation Runs sowie Generation Context persistieren.
- Vorschlagsdatum bleibt `suggested_for` und veraendert nicht versteckt den Kalender.
- Proposal Card, Begruendung, Datenqualitaet, Warnungen und Actions anzeigen.
- Bearbeitung nutzt den bestehenden Editor und erzeugt eine neue Revision.
- Accept, Reject und explizites lokales Schedule implementieren. Schedule nennt die Revision und
  wird nur ausgefuehrt, wenn sie exakt der angenommenen Revision entspricht.
- Nach Annahme kann derselbe Garmin-Service verwendet werden.

Exit-Kriterien:

- End-to-End-Flow funktioniert ohne LLM und ohne direkten DB-Testhack.
- Vorschlag erscheint nicht unbemerkt als angenommener Kalendertermin.
- User Edit wird erneut vollstaendig validiert.
- Annahme fuehrt eine frische kontextuelle Validierung aus und speichert deren Context-
  Fingerprint.
- Exakte Annahme, Doppelklick und Garmin-Idempotenz sind getestet.
- Persoenlicher HF-Bereich, Profilquelle und Synchronisationsdatum sind sichtbar und in der
  Revision eingefroren; ohne valides Profil bleibt das Geraete-Ziel explizit offen.

### Phase 9 - LangChain-Integration als strukturiertes Artefakt

**Status:** Abgeschlossen am 24. August 2026. Details und Gates stehen in
`phase-9-completion.md` und `ai-coach-phase-9-gate-matrix.md`.

**Ziel:** Der bestehende Coach kann deterministische Vorschlaege anfordern und erklaeren.

Arbeiten:

- Bestehende read-only Tools behalten.
- Genau ein neues idempotentes Tool fuer den MVP:
  `create_running_workout_proposal`.
- Tool akzeptiert Intent und strukturierte Inputs, keine frei erfundene WorkoutDefinition.
- `CoachRuntimeContext` um Conversation-, User-Message- und Assistant-Run-ID erweitern.
- Proposal mit ausloesender User-Message und dem erzeugenden Assistant Run verknuepfen.
- SSE-Vertrag um serverseitiges `proposal.created`-Artefakt erweitern.
- Browser laedt eine user-scoped serverseitige Proposal Card; kein LLM-HTML rendern.
- Prompt und UI erklaeren die neue Proposal-Faehigkeit korrekt.
- Accept, Schedule und Garmin Sync bleiben zunaechst ausschliesslich UI-Commands.
- Der Server leitet genau einen stabilen Mutation Slot pro Assistant Run ab. Provider-
  Tool-Call-IDs sind niemals Business-Idempotency-Keys.
- Command Ledger/Event und Proposal-Erstellung erfolgen atomar. Ein Retry mit neuer Tool-Call-ID
  liefert dasselbe Proposal.
- Ein nach der Proposal-Erstellung fehlgeschlagener oder unterbrochener Stream behaelt das
  gueltige Artefakt und macht es in der Conversation wieder auffindbar.
- Evals fuer Prompt Injection, erfundene Pace, fehlende Safety-Angaben, direkte Sync-Forderung
  und Provider-Fehler einfuehren.

Exit-Kriterien:

- Modell kann kein nicht existentes Artefakt vortaeuschen.
- Chat-Phrase kann weder annehmen noch synchronisieren.
- Modell sieht nur minimale, normalisierte Athletendaten.
- Bestehende Health-Coach-Fragen funktionieren weiter.
- Retry mit geaenderter Tool-Call-ID, Disconnect nach Commit und Provider-Fehler nach Erstellung
  erzeugen kein zweites Proposal.

### Phase 10 - Daily Adaptation

**Ziel:** Heutige Einheiten koennen sicher reduziert, ersetzt oder verschoben werden.

**Umsetzungsstand 26. August 2026:** Abgeschlossen. Die Gate-Matrix liegt unter
`docs/plans/ai-coach-phase-10-gate-matrix.md`, die Abschlussdokumentation unter
`docs/plans/phase-10-completion.md`. Implementiert sind der deterministische Kandidatenkern,
user-scoped Kontextaufbau fuer ein heute angenommenes und eingeplantes Lauftraining,
Baseline-/Recovery-/Feedback-/Wochen-Fingerprints, die vier initialen Klassen, Persistenz mit
CAS/Idempotenz/Replay, erneute Annahme mit frischem Kontext, Garmin-Update-in-place,
echte Ersetzung als separates Workout mit `replaces_workout_id` inklusive atomarem Terminwechsel
und Kalender-Retirement sowie die Detailseiten-UI mit dokumentiertem Accessibility-Pass.

Erste Adaptationsklassen:

- `KEEP`
- `REDUCE_VOLUME`
- `REPLACE_WITH_EASY`
- `REST`

Spaeter nach Week-Context-Validierung:

- `REDUCE_INTENSITY`
- `SIMPLIFY_QUALITY`
- `DEFER_KEY_SESSION`
- `REPLAN_WEEK`

Arbeiten:

- Heutige angenommene Revision, Wochenkontext, Baseline und Feedback laden.
- Kandidaten ausschliesslich deterministisch erzeugen.
- Aenderung als neue Revision desselben Workouts speichern, wenn die Workout-Identitaet bleibt.
- Echte Ersetzung ueber `replaces_workout_id` modellieren.
- Vorher/Nachher-Diff und Wochenauswirkung anzeigen.
- Bereits synchronisierte Workouts nach erneuter Annahme in place aktualisieren und ggf.
  reschedulen; der bestehende Remote-Link bleibt erhalten.
- Bei unklarem Garmin-Ausgang keine zweite Remote-Kopie erzeugen.

Exit-Kriterien:

- Kein Kandidat erhoeht standardmaessig die Last.
- Keine Verschiebung erzeugt verdeckt zwei harte Tage oder Long-Run-Konflikte.
- Originalrevision und Audit bleiben erhalten.
- Bereits synchronisierte Anpassung nutzt die bekannte Remote-Identity; bei `unknown` wird keine
  automatische zweite Remote-Kopie erzeugt.

### Phase 11A - Athlete Planning Inputs und Wochenplan im Shadow Mode

**Status:** Abgeschlossen am 26. August 2026. Details und Gates stehen in
`phase-11a-completion.md` und `ai-coach-phase-11a-gate-matrix.md`.

**Ziel:** Eine deterministische Woche aus bestehenden Workout-Templates planen.

Migration:

- `AthletePlanningProfile`
- `AthleteGoal`
- `AthleteAvailability`
- `PerformanceAnchor`

Arbeiten:

- Revertierte Migrationen 11-15 nicht wiederherstellen oder cherry-picken.
- Neues additives Schema mit mehreren Goals und historisierten Leistungsankern entwerfen.
- Verfuegbare Tage, Zeitbudgets, bevorzugten Long-Run-Tag und Erfahrungs-/Re-Entry-Kontext
  erfassen.
- Planner darf nur bereits unterstuetzte und validierte Templates platzieren.
- Verfuegbarkeit, Baseline, Quality Density, Long-Run-Historie und No-Catch-Up durchsetzen.
- Planner-Ergebnisse zunaechst nur als typisierte, nicht persistierte Kandidaten in Tests und
  einer internen Shadow-Ansicht erzeugen.
- Eine versionierte Gate-Matrix ordnet jedes Exit-Kriterium einem Test-Node, Fixture und
  erwarteten Zustand zu.

Exit-Kriterien:

- Keine Einheit liegt an einem nicht verfuegbaren Tag.
- Keine verbotene Quality Density oder Catch-Up-Stapelung.
- Synthetische Athleten und Property Tests pruefen Verteilungen, nicht nur Snapshots.
- Shadow-Planung ist stabil, bevor ein persistentes Planmodell eingefuehrt wird.

### Phase 11B - Persistierter Wochenplan

**Status:** Abgeschlossen am 26. August 2026. Details und Gates stehen in
`phase-11b-completion.md` und `ai-coach-phase-11b-gate-matrix.md`. Der vorgesehene
Agent-Browser-Pass wurde auf ausdruecklichen Nutzerwunsch ausgelassen; das Restrisiko ist in der
Abschlussdokumentation festgehalten.

**Ziel:** Den validierten Shadow-Plan als immutable Planrevision und normale Workouts ablegen.

Migration:

- `TrainingPlan`
- `TrainingPlanRevision`
- Plan-zu-Workout-Mitgliedschaft

Arbeiten:

- Workouts bleiben normale `Workout`-Aggregate mit Revisionen.
- Zunaechst Annahme pro Workout; kein Batch Accept und kein Batch Garmin Sync.
- Bestehenden `/plans`-Kalender erweitern, nicht ersetzen.

Exit-Kriterien:

- Unangenommene Plan-Workouts sind klar als Vorschlag sichtbar.
- Planrevisionen enthalten nur Referenzen auf Workouts und keine zweite Definition.
- Desktop-, Mobile-, Tastatur- und Accessibility-Checks des erweiterten Kalenders bestehen.

### Phase 12 - Mehrwochenplaene

**Ziel:** Versionierte, phasenbasierte Plaene mit kontrollierter Replanung.

Reihenfolge:

1. Re-Entry und Base.
2. 5 km und 10 km.
3. Halbmarathon.
4. Marathon zuletzt.

Arbeiten:

- Base, Build, Specific, Taper und Recovery als versionierte Planlogik.
- Goal-Plausibilitaet und Mindestdaten pruefen.
- Planrevisionen immutable halten.
- Verpasste Einheiten nicht nachholen oder stapeln.
- Ausfuehrung und Feedback duerfen neue Vorschlaege erzeugen, aber keine angenommenen
  Workouts automatisch ueberschreiben.
- Jede Replanung zeigt Diff, Annahmen, Confidence und Auswirkungen.
- Taper und Progression als konfigurierbare Bereiche statt universeller Zahlen.

Exit-Kriterien:

- Simulationen decken alle Zieltypen, Sparse Data, Unterbrechungen, unrealistische Ziele und
  fehlende Wearables ab.
- Volumen, Long Run, Quality Density und Taper bleiben innerhalb der aktiven Regeln.
- Jede materielle Planaenderung benoetigt explizite Annahme.

### Phase 13 - Produktionshaertung

**Ziel:** Kontrollierter Rollout, Betrieb und Datenschutz.

Arbeiten:

- Privacy-safe Decision Traces mit Rule-, Template-, Evidence- und Generator-Versionen.
- Metriken fuer Proposal, Validation, Edit, Accept, Reject, Sync und Adaptation.
- Rate Limits fuer Coach und mutierende Endpunkte.
- Security Headers und Cache-Control fuer sensible Seiten.
- Vollstaendiger User-Export und Account-/Datenloeschung.
- Drift-Erkennung fuer Garmin-Responses, Templates, Regeln und LLM-Tool-Contracts.
- Contract Fixtures ohne echte Health-, GPS- oder Token-Daten.
- Browser-E2E fuer Desktop und Mobile sowie Accessibility-Pruefung.
- Prompt-Injection-, Authorization- und Mutation-Red-Team-Suite.
- Feature-Flag-Rollout zuerst intern, danach kleine Nutzergruppe.

Exit-Kriterien:

- Beobachtbarkeit ohne sensible Rohdaten.
- Rollback deaktiviert Features ohne Datenverlust.
- Stuck/unknown Garmin-Operationen sind sichtbar und behandelbar.
- Vollstaendige CI- und Browser-Gates sind gruen.

## 9. Geplante Migrationsreihenfolge

Die exakten Revision IDs werden bei Implementierung gegen den dann aktuellen Alembic-Head
vergeben. Bestehende Migrationen werden nicht veraendert.

```text
Migration A:
  WorkoutRevision
  WorkoutValidationRun
  WorkoutEvent
  WorkoutGarminBinding
  WorkoutGarminRemoteIdentity
  additive Workout-Revisions-/Status-/Provenance-Felder

Migration B:
  WorkoutGarminOperation
  WorkoutGarminAttempt

Migration C:
  PreSessionFeedback
  PostSessionFeedback

Migration D:
  AthletePlanningProfile
  AthleteGoal
  AthleteAvailability
  PerformanceAnchor

Migration E:
  TrainingPlan
  TrainingPlanRevision
  Plan-Workout-Mitgliedschaft
```

Jede Migration benoetigt:

- Upgrade einer frischen Datenbank.
- Upgrade von aktuellem Head mit befuellten Legacy-Workouts.
- `alembic check` gegen die SQLAlchemy-Metadaten.
- Downgrade, soweit ohne Datenverlust sinnvoll.
- Tests fuer Backfill und Widerspruchsbehandlung.
- Zusammengesetzte Same-Workout-/Same-User-Foreign-Keys fuer Revisions- und Replacement-Pointer.
- Export aller neuen Modelle aus `app/models/__init__.py`.

## 10. Teststrategie

### 10.1 Bestehende Gates

Nach jedem integrierbaren Schritt:

```text
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Bei Modell- oder Migrationsaenderungen zusaetzlich:

```text
uv run pytest tests/test_migrations.py
```

### 10.2 Neue Testarten

Unit Tests:

- Baseline und Confidence
- Intensity Source Priority
- Load Estimate
- Template Expansion
- Validation Codes
- Safety Triage
- State Transitions
- Content Hash
- Idempotency Key
- Validation Freshness und Context Fingerprint

Property Tests:

- keine negativen Zeiten oder Distanzen
- `low <= high`
- korrekte Repeat-Summen
- Daily Adaptation erhoeht Last nicht
- Safety Stop wird nicht ueberstimmt
- nicht angenommene Revision wird nicht synchronisiert
- stale Revision wird nicht angenommen
- stale kontextuelle Freigabe wird bei Annahme erneut bewertet
- kein verbotener Quality-Abstand
- kein Catch-Up-Stacking

Migration Tests:

- Legacy-Status-Mapping
- Revision-Backfill
- inkonsistente Remote-Zustaende
- Matrix aus Status, Remote-ID und Datum
- Same-Workout- und Same-User-FK-Verletzungen
- befuellte Upgrade-Pfade
- Schema-Check

Garmin Contract Tests:

- vorhandene Compiler-Payloads
- Upload-/Schedule-/Push-Doppelklick
- Remote-Erfolg mit verlorenem Response
- Update-Erfolg plus Push-Fehler
- Unschedule-Erfolg plus Delete-Fehler
- Reconciliation und `unknown`
- Startup Repair fuer verwaiste Attempts
- Remote-Identity-Historie und spaete Activity-Zuordnung
- keine automatische Wiederholung ohne bewiesene Abwesenheit

Scenario Tests:

- Easy Run erstellen, editieren, annehmen und synchronisieren
- fehlende Pace-Basis
- nur geringe Motivation
- hohe Fatigue
- lokalisierter Schmerz
- Fieber
- Intervallverschiebung vor Long Run
- doppelter Sync
- Provider-Fehler nach Proposal-Erstellung
- Disconnect nach persistierter Proposal-Erstellung

LLM Evals:

- keine erfundene Pace
- Rueckfrage bei unklarem Schmerz
- keine Diagnose
- nur serverseitige Artefakte praesentieren
- keine implizite Annahme
- kein behaupteter Sync ohne Tool-Erfolg
- Prompt Injection veraendert keine Regeln
- neue Provider-Tool-Call-ID erzeugt im selben Assistant Run kein zweites Proposal

UI/E2E:

- Manual und Coach nutzen denselben Editor
- Revision-Diff und Source Badge
- exakte aktuelle Revision annehmen
- Sync erst nach Annahme
- responsive Desktop-/Mobile-Ansicht
- Tastatur, Fokus, Live Regions, Dark Mode und Accessibility

### 10.3 Gate-Matrix

Jede Implementierungsphase legt vor dem ersten Produktivcode eine kleine Gate-Matrix an. Fuer
jedes Exit-Kriterium nennt sie:

- exakten Test-Node oder Browser-Check;
- Fixture beziehungsweise synthetischen Athleten;
- erwarteten Zustand und relevante Negativbedingung;
- auszufuehrenden Command;
- Feature Flag, die bis zum Bestehen deaktiviert bleibt.

Qualitative Formulierungen allein reichen nicht als Freigabe. Jede UI-aendernde Phase fuehrt
Desktop-, Mobile-, Tastatur- und Accessibility-Pruefungen aus; diese werden nicht bis zur
Produktionshaertung aufgeschoben.

## 11. Rollout und Feature Flags

Empfohlene Freigabereihenfolge:

```text
1. Revisionen fuer manuelle Workouts
2. neuer Garmin-Sync-Service fuer manuelle Workouts
3. Baseline Shadow Mode
4. Proposal-UI ohne LangChain
5. Easy-Run-Proposals im Coach
6. weitere Einzelworkouts
7. Daily Adaptation
8. Wochenplan
9. 5-km-/10-km-Mehrwochenplan
10. Halbmarathon
11. Marathon
```

Ein spaeteres Feature kann abgeschaltet werden, ohne Manual, Analytics, Garmin-Sync oder den
lesenden Health Coach zu deaktivieren.

## 12. Bewusst verschobene Arbeiten

Nicht im ersten vertikalen Slice:

- vollstaendige Umbenennung von `WorkoutDefinition`
- zweites kanonisches Workout-Modell
- Entfernen der Legacy-Tabelle `WorkoutStep`
- Critical Speed ohne belastbare Race-/Time-Trial-Anker
- Power-, Lap-Button- und Open-Step-Support
- Wetter- oder Luftqualitaetsintegration
- Vector Database oder RAG als Regeldurchsetzung
- autonomer Planungsagent
- Chat-basierte Annahme oder Garmin-Synchronisation
- Batch-Accept und Batch-Sync
- automatische Mehrwochen-Replanung
- Microservices, Worker oder Distributed Locks
- Speicherung jedes Garmin-Rohpayloads

Diese Punkte werden nur aufgenommen, wenn ein konkreter Produktbedarf den zusaetzlichen
Komplexitaetspreis rechtfertigt.

## 13. Hauptrisiken und Gegenmassnahmen

### Externe Garmin-Wirkung ist nicht transaktional

Gegenmassnahme: Persistente Operation vor Netzaufruf, getrennte Remote-Zustaende,
Reconciliation und kein blindes Retry bei `unknown`.

### Alte Statussemantik ist inkonsistent

Gegenmassnahme: Legacy-Mapping mit explizitem `unknown`, Compatibility Projection und spaeterer
separater Cleanup-Migration.

### LLM halluziniert sichere Trainingsparameter

Gegenmassnahme: LLM liefert nur Intent; Parameter kommen aus Generator und Validator. UI laedt
serverseitige Artefakte statt LLM-JSON.

### Zu wenig subjektive Safety-Daten

Gegenmassnahme: Safety-Form und Clarification Gate vor Generator; Wearables sind sekundaer.

### Falsche Praezision durch Garmin-Scores

Gegenmassnahme: Source Priority, Alter, Coverage und Confidence; RPE/Talk Test als Fallback.

### Funktionsduplikation waehrend Migration

Gegenmassnahme: Erst Application Service extrahieren, danach Coach anbinden. Routen und Tools
rufen dieselben Commands auf; keine internen HTTP-Selbstaufrufe.

### Grosse Migration destabilisiert bestehende App

Gegenmassnahme: Additive Migrationen, Backfill-Tests, zeitlich begrenztes Dual Read/Write,
Feature Flags und kleine Integrationsschritte.

## 14. Definition of Done des Gesamtvorhabens

Das Gesamtvorhaben ist abgeschlossen, wenn:

- Manual, Coach, Daily Adaptation und Plaene dasselbe Workout-Modell nutzen.
- Nur ein Editor, eine Preview, ein Validator-Stack und ein Garmin-Compiler existieren.
- Jeder generierte und editierte Inhalt immutable revisioniert wird.
- Garmin nur die exakt angenommene Revision erhalten kann.
- Approval, lokales Scheduling, Garmin Content, Garmin Calendar und Device Push Request
  Acceptance getrennt nachvollziehbar sind.
- Safety Stops deterministisch wirken.
- Garmin- und Wearable-Scores niemals allein entscheiden.
- Einzel-, Tages-, Wochen- und Mehrwochenlogik versioniert und reproduzierbar sind.
- 5 km, 10 km, Halbmarathon und Marathon durch Simulationen und Szenarien abgesichert sind.
- User Daten exportieren und vollstaendig loeschen koennen.
- Keine echten Garmin-Daten, Tokens, GPS-Traces oder Health-Payloads in Tests oder Logs landen.
- Unit-, Property-, Migration-, Contract-, Scenario-, LLM- und UI-E2E-Tests gruen sind.

## 15. Unmittelbar naechster Schritt

Die Implementierung beginnt mit Phase 1, nicht mit dem Generator:

```text
CSRF und Characterization Tests
-> gemeinsamer Workout Application Service
-> Revisionen und exakte Annahme
-> idempotente Garmin-Zustaende
```

Erst wenn diese Vertrauenskette steht, werden Baseline, Safety, Generator und LangChain daran
angeschlossen. So entsteht keine zweite Funktionalitaet neben der bestehenden App und kein
nicht kontrollierbarer Agentenpfad.
