# AI Coach Phase 10 Gate Matrix

Status: abgeschlossen (26. August 2026)  
Feature Flag: `COACH_DAILY_ADAPTATION_ENABLED=false` im committed Default; Freigabe nach gruenem
UI-Pass ist dokumentiert.

## MVP-Grenze

Der erste freigabefaehige Umfang enthaelt ausschliesslich `KEEP`, `REDUCE_VOLUME`,
`REPLACE_WITH_EASY` und `REST`. `REDUCE_INTENSITY`, `SIMPLIFY_QUALITY`,
`DEFER_KEY_SESSION` und `REPLAN_WEEK` bleiben gesperrt, bis ein eigener
Week-Context-Validator harte Tage, Long Runs, Key Sessions und die Wochenlast
deterministisch klassifiziert und validiert.

Das LLM darf spaeter validierte Optionen erklaeren, aber keine Kandidaten,
Revisionen, Annahmen, Termine oder Garmin-Ergebnisse erfinden.

## Gate Matrix

| Gate | Status | Exakter Nachweis | Fixture / Athlet | Erwarteter Zustand und Negativbedingung | Command | Flag |
| --- | --- | --- | --- | --- | --- | --- |
| Deterministische Klassen | Kern gruen | `tests/test_daily_adaptation.py::test_identical_inputs_generate_identical_candidates` | Fester Easy Run mit festem Feedback | Gleicher Input erzeugt identische Klassen, Dimensionen und Reason Codes; kein Zufall und kein LLM | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Keine Laststeigerung | Kern gruen | `tests/test_daily_adaptation.py::test_generated_candidates_never_increase_load`, `tests/test_daily_adaptation.py::test_distance_workout_uses_estimated_duration_for_budget_and_comparison` | Hypothesis-Definitionen sowie Distanz-Workout mit Load Estimate | Jede Dimension ist kleiner oder gleich dem Original; keine negative oder Null-Dauer in Laufkandidaten | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Safety Stop dominiert | Kern gruen | `tests/test_daily_adaptation.py::test_safety_stop_only_allows_rest`, `tests/test_daily_adaptation.py::test_warn_without_provably_easy_replacement_recommends_rest` | Gangveraendernder Schmerz, Fieber, kurzer harter und nicht vergleichbarer Lauf | Ausschliesslich beziehungsweise konservativ empfohlenes `REST`; positive Signale koennen den Stop nicht ueberstimmen | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Unklare Safety-Angabe | Kern gruen | `tests/test_daily_adaptation.py::test_clarification_produces_no_executable_candidate` | Unvollstaendige Schmerz- oder Krankheitseingabe | Kein ausfuehrbarer Kandidat; gezielte Klaerung erforderlich | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Volumenreduktion | Kern gruen | `tests/test_daily_adaptation.py::test_reduce_volume_scales_time_distance_and_repeats_without_changing_targets` | V1/V2, gemischte Endbedingungen, Repeat-Block | Dauer und Distanz sinken deterministisch; Targets, Reihenfolge, IDs und Dichte bleiben gleich | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Easy-Ersatz | Kern gruen | `tests/test_daily_adaptation.py::test_easy_replacement_requires_comparable_load_and_keeps_low_intensity` | Vergleichbarer zeitbasierter Lauf mit RPE | 20-45 Minuten, RPE 2-3, Sprechtest, keine Pace-Erfindung; bei unbekannter Vergleichslast kein Ersatz | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Heutiges angenommenes Workout | Kern gruen | `tests/test_daily_adaptation.py::test_only_owned_accepted_scheduled_running_workout_today_is_eligible` | Heute, Zukunft und fremder User | Nur user-scoped, angenommen, lokal fuer heute eingeplant und Running ist zulaessig | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Kontext-Freshness | Gruen inklusive Apply-Conflict | `tests/test_daily_adaptation.py::test_feedback_and_week_changes_invalidate_adaptation_context`, `tests/test_daily_adaptation.py::test_availability_uses_only_target_workout_feedback_from_today`, `tests/test_daily_adaptation.py::test_apply_rejects_stale_context` | Geaendertes Feedback/Woche, Feedback zu fremdem Workout und veralteter Apply-Kontext | Preview-Fingerprint aendert sich; Zeitbudget stammt nur aus heutigem Feedback zum Ziel-Workout; Apply mit altem Kontext schlaegt mit `adaptation.context_stale` fehl | `uv run pytest tests/test_daily_adaptation.py tests/test_feedback.py` | aus |
| Immutable Revision und Audit | Gruen | `tests/test_daily_adaptation.py::test_content_adaptation_appends_revision_and_preserves_original` | Angenommene Revision plus Reduce/Replay/Accept | Neue Revision mit Parent und Provenienz; Original unveraendert; Replay erzeugt keine zweite Revision; exakte erneute Annahme erforderlich | `uv run pytest tests/test_daily_adaptation.py` | aus |
| KEEP und REST | Gruen | `tests/test_daily_adaptation.py::test_keep_preserves_execution_and_rest_cancels_only_today` | Angenommener lokaler Termin | KEEP veraendert keine Revision; REST bewahrt Original/Audit und entfernt nur den lokalen Termin ohne Fake-Workout | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Wochenwirkung | Gruen | `tests/test_daily_adaptation.py::test_week_impact_is_explicit_and_non_increasing` | Woche mit heutigem Lauf | Before/After und Wochendelta sind explizit; kein Kandidat erhoeht Wochenlast | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Garmin Update in place | Gruen | `tests/test_daily_adaptation.py::test_synced_adaptation_updates_known_remote_identity_without_upload` | Bereits synchronisierte angenommene Revision | Nach erneuter Annahme genau ein `update`, nie ein zweites `upload`; Remote Identity bleibt identisch | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Echte Ersetzung | Gruen | `tests/test_daily_adaptation.py::test_easy_replacement_is_separate_workout_and_acceptance_swaps_schedule`, `tests/test_daily_adaptation.py::test_discarded_replacement_preserves_original_and_allows_another` | Angenommenes heute eingeplantes Workout | `REPLACE_WITH_EASY` erzeugt separates Workout mit `replaces_workout_id`, Revision 1 ohne Cross-Workout-Parent; Annahme tauscht den Termin atomar (Original `cancelled`, Ersatz `scheduled`); Verwerfen bewahrt das Original und erlaubt einen neuen Versuch | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Ersatz-Sync ohne Doppel-Kopie | Gruen | `tests/test_daily_adaptation.py::test_synced_replacement_retires_old_calendar_before_new_upload` | Synchronisiertes und eingeplantes Original | Alter Garmin-Kalendereintrag wird nachweislich `unschedule`d, bevor der Ersatz als neues Upload hochgeladen wird; zwei Bindings, zwei Remote-Identities, Original bleibt in der Bibliothek | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Safety-Rest mit Garmin-Termin | Gruen | `tests/test_daily_adaptation.py::test_safety_stop_rest_can_remove_existing_garmin_calendar_entry` | Synchronisiertes Workout mit aktuellem Safety Stop | REST entfernt den Remote-Kalendereintrag ohne Safety-Validierung des Inhalts; kein Upload, kein zweites Artefakt | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Original-Schutz | Gruen | `tests/test_daily_adaptation.py::test_original_cannot_be_deleted_while_replacement_is_active` | Offener und angenommener Ersatz | Loeschen des Originals scheitert mit `adaptation.replacement_active`, solange ein Ersatz lebt | `uv run pytest tests/test_daily_adaptation.py` | aus |
| Unklarer Garmin-Ausgang | Gruen | `tests/test_daily_adaptation.py::test_unknown_remote_state_blocks_adaptation_acceptance`, `tests/test_workout_service.py` | `unknown` Binding-Zustand vor der Annahme | Annahme scheitert mit `garmin.state_unknown`; keine zweite Remote-Kopie, kein Blind Retry | `uv run pytest tests/test_daily_adaptation.py tests/test_workout_service.py` | aus |
| Autorisierung und CSRF | Gruen | `tests/test_daily_adaptation.py::test_adaptation_routes_are_user_scoped_and_flagged`, `tests/test_http_security.py` | Zwei User, Flag an/aus, CSRF-Pflicht aller POSTs, leerer Idempotency-Key | Fremde IDs liefern 404; Flag aus blockiert Apply mit Fehler-Redirect; leere Keys liefern 422; CSRF wird zentral erzwungen | `uv run pytest tests/test_daily_adaptation.py tests/test_http_security.py` | aus |
| UI und Accessibility | Gruen | Agent-Browser-Pass 26.08.2026, Session `phase-10-audit`, Screenshots `%TEMP%\opencode\phase10-replacement-desktop.png` und `phase10-replacement-mobile.png` | Authentifizierter Desktop- und Mobile-Viewport (390x844) | Echter 60-Minuten-Chatvorschlag blieb 60 Minuten; alle vier Klassen mit eindeutigen Button-Labels; Ersatz-Flow inkl. Banner, „Ersatz annehmen“, atomarem Terminwechsel und blockiertem Original-Loeschen; axe WCAG 2.x A/AA: 0 Verstoesse auf Coach- und Detailseite (Desktop und Mobile) | `agent-browser --state pacepilot-auth.json --session phase-10-audit ...` | aus |

## Freigabe

Alle Tabellenzeilen sind implementiert und gruen: die fokussierten Tests, die vollstaendigen
Quality Gates (`uv run pytest` 339 Tests, `uv run ruff check .`, `uv run ruff format --check .`,
`uv run ty check`) und der finale UI-Pass vom 26. August 2026. Das Feature bleibt im committed
Default deaktiviert und wird pro Umgebung ueber `COACH_DAILY_ADAPTATION_ENABLED` freigeschaltet.
