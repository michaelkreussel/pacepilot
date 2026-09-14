# Aufgaben: Runna-orientierter Trainingsbereich

Plan: [plan.md](plan.md). Status: geplant, noch nicht implementiert.
Bestehende Nutzeränderungen erhalten. Direkt auf `main` arbeiten.

## 1. Persönliche Beispiele reproduzieren — S

- [ ] Agent Browser: Heute, Kalender, Mehrwochenplan, Erstellung, Laufprofil und
  bisherige Intervall-Chats ansehen; Desktop/Mobil mit Screenshots dokumentieren.
- [ ] Je einen kurzen Lauf, eine ausschließlich lockere Woche und einen wiederholten
  Intervallvorschlag mit Datum, Version und tatsächlich begrenzendem Faktor erklären.
- [ ] Minimale anonymisierte Fälle an stabilen Servicegrenzen reproduzieren.

Dateien: `tests/test_package2_planning.py`, `tests/test_daily_recommendation.py`,
`tests/test_workout_proposals.py`, Prüfbericht unter `tasks/`.
Prüfung: passende einzelne pytest-Fälle, keine Live-Garmin-/LLM-Aufrufe.
Abhängigkeiten: frische lokale Browseranmeldung für persönliche Beispiele.

## 2. Klickbares Zielbild — S

- [ ] Heute, Woche und Trainingsdetail sowie Planerstellung als durchgehenden
  klickbaren Entwurf darstellen; alle Beispieldaten als solche kennzeichnen.
- [ ] Desktop/Mobil, Typografie, Farben, Zustände und Hauptaktionen sichtbar machen.
- [ ] Designrichtung mit dem Nutzer am Entwurf abstimmen.

Dateien: eigenständiger lokaler Prototyp und `tasks/plan.md`; keine Produktlogik ändern.
Prüfung: Browser bei 1440/390 px, Navigation und Tastaturbedienung.
Abhängigkeiten: 1 für reale Problemstellen; Vorbereitung anhand der Templates möglich.

## 3. Woche sinnvoll dosieren — M

- [ ] Wochenbudget, bereits absolvierte Arbeit und Tageslimits gemeinsam berücksichtigen;
  keine unbegründete Resteinheit und kein doppelt gezähltes Training.
- [ ] Vollständige Historie, Beobachtungslücke, Wiedereinstieg und vorhandener Plan
  ergeben unterscheidbare, erklärte Entscheidungen.
- [ ] Wochenübersicht zeigt Gesamtumfang und Begründung von Reduktionen.

Dateien: `weekly_planner.py`, `multiweek_planner.py` unter `app/services/planning/`,
`app/templates/plans/cycle.html`, `tests/test_package2_planning.py`.
Prüfung: fokussierte Wochen-/Mehrwochenfälle; Vorschau entspricht gespeicherter Dauer.
Abhängigkeiten: 1; sichtbare Darstellung orientiert sich an 2.

## Kontrollpunkt A

- [ ] Die persönlichen Fehlfälle sind erklärt und Dosierungsfälle geschützt.
- [ ] Der Designentwurf ist anhand konkreter Ansichten beurteilt.

## 4. Eignung für Qualitätsreize — M

- [ ] Importiertes geeignetes Training kann zur Eignung beitragen, auch ohne frühere
  PacePilot-VO2-Einheit; keine zirkuläre Voraussetzung.
- [ ] Tatsächliche Belastung und unvollständige Beobachtung bleiben unterscheidbar.
- [ ] Ablehnung/Auswahl nennt den wirksamen Grund; Belastungsabstände gelten auch
  über Wochengrenzen und für bereits geplante Arbeit.

Dateien: `daily_recommendation.py`, `training_fit.py`, `weekly_planner.py` unter
`app/services/planning/`, `tests/test_daily_recommendation.py`.
Prüfung: gleiches Profil mit/ohne lokale Workout-Verknüpfung, frische Belastung,
spätere Trainingswoche und Wiedereinstieg; Regeln fachlich begründen.
Abhängigkeiten: 1 und 3.

## 5. Begründete Intervallalternativen — M

- [ ] Auswahl richtet sich nach Zweck und Belastungsrahmen; Kalenderrotation allein
  gilt nicht als Personalisierung. Explizite Alternativen sind tatsächlich anders.
- [ ] Fehlende Pace hat einen verständlichen Fallback; Arbeit, Pausen und gesamte
  Dauer passen auch bei Distanzintervallen in den verfügbaren Rahmen.
- [ ] Vorschau, Speicherung und Export erhalten dieselben Schritte und Ziele.

Dateien: `interval_prescription.py`, `workout_proposals.py` unter `app/services/planning/`,
`tests/test_workout_proposals.py`, `tests/test_workout_templates.py`.
Prüfung: Zeit-/Distanzvarianten, gleiche Eingaben stabil, Budgetgrenzen und Persistenz.
Abhängigkeiten: 4. Weitergabe der Alternativabsicht im Coach in Aufgabe 9 prüfen.

## 6. Gemeinsame Trainingsdarstellung — M

- [ ] Heute und Chat zeigen dieselben verständlichen Trainingsblöcke, Kennzahlen
  und kurzen persönlichen Gründe.
- [ ] Klare Hauptaktion und gestaltete Zustände für Empfehlung, eingeplant,
  erledigt, Ruhe und fehlende Daten.
- [ ] Darstellung funktioniert mobil und mit Tastatur ohne abgeschnittene Inhalte.

Dateien: `app/templates/coach/_today.html`, `app/templates/coach/_session.html`,
`app/templates/workouts/_coach_proposal_card.html`, `app/static/css/tailwind.input.css`.
Prüfung: bestehende Coach-Darstellungstests, CSS-Build, visuelle Prüfung.
Abhängigkeiten: 2 und 5. Generiertes CSS und Cache-Schlüssel zusätzlich aktualisieren.

## 7. Mein Plan als zusammenhängende Ansicht — M

- [ ] Aktives Ziel, Phase, Wochenumfang und Trainingsmix stehen im Vordergrund;
  Varianten/alte Entwürfe überlagern den aktiven Plan nicht.
- [ ] Kalender und Mehrwochenübersicht verwenden denselben Einheitenaufbau;
  Mobilansicht bietet eine Tagesagenda.
- [ ] Deutsche Trainingssprache ersetzt interne Revisionsbegriffe im Hauptablauf.

Dateien: `app/templates/plans/index.html`, `app/templates/plans/cycle.html`,
`app/routes/plans.py`, `tests/test_package2_planning.py`.
Prüfung: aktive/offene Pläne, nächste Woche, erledigte/geplante Einheiten;
CSS-Build und Browserprüfung. Abhängigkeiten: 3 und 6.

## Kontrollpunkt B

- [ ] Dieselbe Einheit stimmt in Heute, Plan, Chat und gespeichertem Inhalt überein.
- [ ] Trainingsmix und Dosierung funktionieren für mehrere reale Profiltypen.

## 8. Geführte Planerstellung und Laufprofil — M

- [ ] Ziel, Ausgangslage, Tage und Vorschau bilden einen zusammenhängenden Ablauf.
- [ ] Importierte Angaben sind mit Zeitraum/Quelle sichtbar und gezielt korrigierbar;
  fehlendes Ziel führt zur Eingabe statt zur Sackgasse.
- [ ] Geänderte Eingaben erzeugen eine nachvollziehbare Vorschau; bestehende
  angenommene Inhalte werden nicht automatisch ersetzt.

Dateien: `app/templates/plans/cycle_new.html`, `app/templates/planning_inputs.html`,
`app/routes/plans.py`, `app/routes/planning_inputs.py`, `tests/test_athlete_planning_inputs.py`.
Prüfung: erstmalige Erstellung, bestehende Daten, ungültige/fehlende Angaben,
CSRF und Nutzertrennung; CSS-Build. Abhängigkeiten: 7.

## 9. Einheiten auswählen und Coach im Kontext nutzen — M

- [ ] Passende Vorschläge und eigene Einheiten sind auffindbar; der manuelle Editor
  bleibt über eine sekundäre Aktion erreichbar.
- [ ] „Coach fragen“ übernimmt die ausgewählte Einheit/den Plan eindeutig.
- [ ] „Andere Intervalle“ erzeugt die passende Alternative aus Aufgabe 5 und zeigt
  deren Änderung; frühere Gespräche bleiben zugänglich.

Dateien: `app/routes/workouts.py`, `app/templates/workouts/form.html`,
`app/services/coach/tools.py`, `app/templates/base.html`, `tests/test_training_agent.py`.
Prüfung: Einstieg aus Plan/Einheit, Variantenabsicht am Tool-Vertrag, vorhandene Chats.
Abhängigkeiten: 5, 7 und 8. Falls mehr Dateien nötig sind, Bibliothek und Coach separat liefern.

## 10. Plan übernehmen und anpassen — M

- [ ] Eine explizite Auswahl kann eine nachvollziehbare lokale Übernahme auslösen;
  Bestätigung und Einplanung bleiben klar, Garmin-Versand getrennt.
- [ ] Vorhandene angenommene Revisionen werden nicht still verändert; Konflikte
  und Teilfehler haben einen verständlichen, wiederholbaren Ablauf.
- [ ] Anpassung zeigt vorher/nachher und erhält die beabsichtigte Trainingswoche.

Dateien: `app/routes/plans.py`, `app/services/planning/weekly_plan_service.py`,
`app/services/planning/workout_service.py`, `app/templates/plans/cycle.html`,
`tests/test_weekly_plan_service.py`.
Prüfung: Mehrfachabsendung, konkurrierende Revision, Teilfehler, keine unbestätigte
Garmin-Übertragung. Abhängigkeiten: 7 und 8. Bewusste Verhaltensänderung, kein reines Redesign.

## Abschluss

- [ ] Resultierenden Diff einmal prüfen, keine sachfremde Bereinigung.
- [ ] Nach allen Codeänderungen: `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run ty check`.
- [ ] Bei Templates/CSS: `npm run build:css`, Cache-Schlüssel aktualisieren.
- [ ] Browser: Heute → Detail → Alternative; Plan → nächste Woche;
  Planerstellung → Vorschau → ausdrückliche Übernahme; Laufprofil → Änderung.
- [ ] Desktop/Mobil, Hell/Dunkel, Fehler-/Leerzustände prüfen.
- [ ] Abschlussbericht nennt sichtbare Änderungen, Backend/Frontend und genaue Testschritte.
