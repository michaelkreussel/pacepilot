# Deterministische Tagesempfehlung (Paket 1)

Die Heute-Karte auf `/coach` benötigt keinen Provider. GET liest vorhandene Daten;
erst „Als Vorschlag speichern“ erzeugt über `WorkoutService` eine normale,
unangenommene und ungeplante Revision. Annahme, Planung und Garmin-Freigaben bleiben
separate bestehende Aktionen. Es gibt keine Migration und keine neue Laufzeitabhängigkeit.

## Operationen für Paket 2

- `daily_recommendation.recommend_today(session, user, *, as_of, available_minutes=None)`
  liefert `DailyRecommendation`: Zustand, exakte Definition, Gesamtzeit, zügige
  Arbeitszeit, datierte Gründe, Annahmen, Vertrauensniveau, Referenzen und Fingerprint.
  `summary()` ist die JSON-Darstellung für HTTP und Coach-Werkzeuge.
- `daily_recommendation.save_recommendation(..., expected_fingerprint, ...)` berechnet
  den aktuellen Kontext erneut. Bei Änderung liefert es die neue Vorschau, andernfalls
  ein `Workout`. Wiederholungen verwenden dasselbe Workout; abgelehnte Vorschläge werden
  nicht reaktiviert und angenommene Inhalte nicht geändert.
- `RunningProposalService.build_candidate(..., parameters=TemplateParameters(...))`
  teilt Expansion, Intensitätsziele, Strukturvalidierung und Revisionsmetadaten zwischen
  expliziten Wünschen und automatischer Auswahl. Keine Persistenz in dieser Operation.
- `automatic_interval_parameters(template_id, budget, *, comparable)` erhält die
  Liste erfolgreicher vergleichbarer **angenommener** Revisionen in zeitlicher Reihenfolge.
  Die automatische Auswahl prüft davor Abschluss, Feedback, Laufbasis und Erholung.
  Wochenplanung muss dieselben Eignungsprüfungen durchführen, bevor sie diese Funktion nutzt.
- `running_intensity.workout_pace_guidance()` berechnet formatspezifische Richtbereiche.
- `training_fit.recent_training_facts()` und `supported_adverse_evidence()` liefern
  datierte Belastungsfakten bzw. tatsächlich ungünstige Befunde ohne Abdeckungswarnungen.

HTTP: `GET /coach/today` liefert JSON. `POST /coach/today/save` erwartet
`context_fingerprint` und optional `available_minutes` sowie CSRF-Schutz. Erfolg führt
mit 303 zur Workout-Detailseite; ein veralteter Kontext liefert 409 mit aktueller Heute-Karte.
Der Server bestimmt `as_of`. Der Client und das Sprachmodell können kein Datum oder
numerische Trainingsparameter für die Tagesempfehlung einschleusen.

Coach-Werkzeuge: `get_today_recommendation` liest denselben Standardkontext wie die
Heute-Karte; `save_today_recommendation` speichert nur nach ausdrücklichem Wunsch.
`create_running_workout_proposal` verlangt jetzt ein ausdrücklich angefragtes Format.
Ein allgemeiner Empfehlungswunsch fällt nicht mehr automatisch auf `easy_run` zurück.
Tagesvorschläge benötigen keine künstliche Assistant-Message-Provenienz.

## Auswahl und Dosis: daily-running-v1

Reihenfolge: bereits gelaufene Einheit → exakt angenommene eingeplante Revision mit
Anpassung → fehlende Zeit/Unverfügbarkeit → ernsthafte Beschwerden oder korroborierte
schlechte Erholung → Häufigkeit und Belastung → Ziel/Phase und passendes Format.
Unangenommene Plankinder sind keine geplanten Einheiten. Überschneidende angenommene
Zyklen mit verschiedenen Zielen führen zu einer Klärung.

Die 7/28/56/180-Tage-Baseline bleibt laufbezogen. Radfahren beeinflusst gegebenenfalls
die Erholung, aber niemals Laufkilometer, Laufdauer oder etablierte Lauffrequenz.
Fehlende Gesundheitsdaten führen allein zu keiner Reduktion. Fehlende Synchronisation,
RPE oder Verknüpfung beweisen keine ausgelassene Einheit und keine leichte Intensität.
Historische Auswertungen berücksichtigen manuelles Feedback nur bis zum Stichtag.

Konservative **Produktgrenzen, keine Verletzungssicherheitsregeln**:

- Übliche Sitzung: Median der letzten 28 Tage; bei fehlender Historie ein editierbares
  vorläufiges Budget von 30 Minuten. Freie Zeit ist eine Obergrenze.
- Kein zusätzlicher Lauftag über die beobachtete übliche Häufigkeit hinaus. Beobachtete
  und angenommene geplante Tage werden ohne Doppelzählung berücksichtigt.
- Neue Qualität: mindestens etwa drei Läufe pro Woche und sechs konsistente Wochen;
  höchstens ein anstrengender Dauerreiz in sieben Tagen. Auch geplante Qualität zählt.
  Kurze bekannte Steigerungen sind kein voller Schwellen-/VO₂-Reiz.
- Harte oder lange Belastung in den letzten zwei Kalendertagen unterdrückt Qualität.
  Dies setzt die bestehende 48-Stunden-Heuristik vorsichtig mit Datumsangaben um und
  behauptet keine genaue Erholungszeit in Stunden.
- Auffälliges beobachtetes Wochenvolumen: mehr als 1,2× den jüngsten Wochenmedian;
  dann keine zusätzliche Qualität. Kein ACWR-Bereich und keine starre Intensitätsquote.
- Wiedereinstieg/Beobachtungslücke: höchstens 30 Minuten und ungefähr 0,7× übliche Dauer,
  bei mindestens 20 Minuten. Unterstützte leichte Ermüdung reduziert auf etwa 0,8×.
- Langer Lauf am bevorzugten Tag: höchstens der jüngste Median der wöchentlichen
  längsten Läufe und 120 Minuten. Unter 60 Minuten Historie bleibt es ein passender
  Easy Run. Kein Sprung auf die Mindestlänge eines langen Laufs.
- Schwelle beginnt mit 3×5 Minuten. Erfolgreiche vergleichbare Abschlüsse können ihren
  bisherigen Umfang erhalten, etwa 5×6 Minuten. Zeit und Arbeit müssen beide passen;
  die Gesamtzeit bleibt höchstens 1,3× der üblichen Sitzung. Diese Grenze ist keine
  vorgesehene Steigerungsrate. Ohne belastbaren Abschluss keine behauptete Progression.
- VO₂ setzt 5-/10-km-Kontext, acht konsistente Wochen, geeignete Leistungsdaten und
  vergleichbare Qualitätserfahrung voraus. Der letzte bekannte Reiz beeinflusst die Wahl.
- Die letzte Woche vor einem hinterlegten Wettkampf sowie Taper-/Erholungsphasen
  erhalten keine neue Qualität und keinen zusätzlichen langen Lauf.

Nur `work_minutes` ergänzt die bisherigen `TemplateParameters`. Registry-Grenzen für
Wiederholungen, Arbeitsdauer und Gesamtarbeit bleiben verbindlich. Ein-/Auslaufen und
Erholung werden bei der Erzeugung nicht verkürzt, um Qualität in ein zu kleines Budget
zu pressen. Bei zu wenig Zeit wird ein lockerer Lauf gewählt.

## Intensitätsprovenienz

Geeignete verlässliche Rennen/Tests zwischen 3 km und Halbmarathon haben Vorrang vor
frischer plausibler Garmin-Schwellenpace. Danach folgen aktuelle Garmin-Bestzeiten
(höchstens 56 Tage, ausdrücklich kein bestätigter unabhängiger Maximaltest).
Ein 1-km-PR, Easy-Run-Mittelwerte, VO₂max und Garmin-Rennprognosen sind keine
Schwellenpace-Autorität. Die vorhandene Baseline-Konfidenz begrenzt Pace-Ziele weiter.

Die distanzabhängige Umrechnung verwendet eine Riegel-Potenzkurve mit dem
Produktparameter 1,06: `T₂ = T₁ × (D₂/D₁)^1,06`. Die Potenzmodell-Familie geht auf
[Riegel, Athletic Records and Human Endurance (1981)](https://www.nku.edu/~longa/classes/mat375/days/docs/CrossCountry/riegel.pdf)
zurück. Der Exponent, die folgenden Bänder und deren Verwendung zur Trainingssteuerung
sind konservative Produktannahmen, keine individuell gemessenen Schwellen.

Für Schwelle wird die äquivalente Ein-Stunden-Leistung geschätzt; VO₂-Wiederholungen
verwenden eine 5-km-Entsprechung nur aus 3–10-km-Leistungen. Richtbereiche entsprechen
95–100 % dieser Geschwindigkeit, bei PRs vorsichtig 93–99 %, nach außen auf fünf
Sekunden/km gerundet. Quelle, Datum, Alter, Konfidenz und Berechnung werden gespeichert.
RPE, Sprechtest und der Hinweis auf Hitze/Hügel bleiben an den Schritten erhalten.

Ohne geeignete Pace kann der Easy Run weiterhin validierte persönliche Garmin-HF-Zone 2
nutzen, einschließlich Bindung an den verbundenen Account. Das ist eine konfigurierte
Gerätezone und keine nachgewiesene physiologische Schwelle. Kurze Steigerungen und
VO₂-Wiederholungen erhalten keine präzisen HF-Ziele. Sonst gelten RPE/Sprechtest.

Critical Speed wird **nie zur Verschreibung verwendet**. Die Analyse meldet sie nur
bei geeigneten unabhängigen Rennen/Tests: 3–20 Minuten Dauer, mindestens Faktor zwei
Dauerunterschied, verschiedene Tage innerhalb von 28 Tagen, nicht älter als 42 Tage,
plausible Geschwindigkeit und D′ von 50–500 m. Ein einzelnes Rennen, gleiche Ereignistage,
PRs, lange Rennen und ungeeignete/stale Paare genügen nicht.

## Anpassung und Persistenz

`DailyAdaptationService` verwendet datierte absolvierte Belastung auch ohne Planverknüpfung.
Nur Abdeckungswarnungen erhalten passende Arbeit. Gestützte leichte Ermüdung reduziert
Umfang; kürzlich harte/lange Arbeit macht geplante Qualität lockerer; ernsthafte oder
korroborierte Befunde empfehlen Ruhe. Explizite bestehende Anpassungsaktionen bleiben
erhalten. Generierte Intervalle reduzieren zuerst Arbeitsdauer/Wiederholungen und
behalten Vorbereitung und Erholung bei. Unterhalb gültiger Mindestarbeit gibt es nur
Easy-/Ruhe-Alternativen. Anpassungsmetadaten behalten exakte Arbeits-/Gesamtzeit.

Die bestehenden JSON-Felder speichern ausgewählte Parameter, Quellen, Gründe und
Kontext. Alte Revisionen bleiben lesbar. Empfehlungen sind absichtlich keine
automatisch angenommenen Pläne und holen keine vermeintlich verpasste Arbeit nach.

## Distanzintervalle und Trainingsansicht (13. September 2026)

Diese Erweiterung ersetzt die oben beschriebenen festen Vorbereitungszeiten bei
neu ausgewählten Intervallvarianten. Bestehende Revisionen bleiben unverändert.

- `interval_prescription.interval_parameters()` wählt innerhalb des vorhandenen
  Arbeitsumfangs: VO₂ 400/600/800/1000 m, Schwelle 1000/1200/1600/2000 m oder
  Zeitintervalle. Die Kalenderwoche bestimmt die Startvariante; ungeeignete Distanzen
  werden übersprungen. Das ist eine nachvollziehbare Produktrotation, keine KI-Auswahl
  und kein Nachweis einer individuell optimalen Periodisierung.
- Distanzschritte benötigen einen belegten Pace-Bereich. Geschätzte Belastungsdauer,
  Wiederholungszahl, Gesamtarbeit und Zeitbudget müssen zusammenpassen. Eine Distanz
  ist nicht für jedes Leistungsniveau und jeden Reiz geeignet. Der Coach kann
  explizite Distanzwünsche über `work_distance_meters` weitergeben; Zielpaces und
  Wiederholungen berechnet weiterhin der Server.
- Einlaufen dauert 10 oder 15 Minuten abhängig von Reiz und Arbeitsumfang, Auslaufen
  5 oder 10 Minuten. Trabpausen werden aus der Belastungsdauer abgeleitet und auf
  30 Sekunden gerundet (Schwelle etwa ein Viertel, VO₂ etwa drei Viertel; 60–180 s).
  Diese Werte sind Produktannahmen, keine wissenschaftlich validierte Individualdosierung.
- Die vorhandene Pace-Schätzung wird für lockere/lange Läufe um breite Richtbereiche
  von 72–82 % der Schwellen-Entsprechung ergänzt, für Erholung um 65–75 %. Eine
  vorhandene persönliche Garmin-HF-Zone 2 hat beim Easy Run weiterhin Vorrang.
  Ein-/Auslaufen kann dieselbe lockere Pace nutzen; Trabpausen bleiben ohne Tempodruck.
  Quelle und Berechnung bleiben in den Revisionsmetadaten. Ohne geeignete Leistungsdaten
  werden keine Zielpaces erfunden.
- Distanzbasierte Zeiten werden am langsamen Ende des Zielbereichs geschätzt. Die
  exakten Distanz-/Pace-Schritte bleiben in Tagesvorschau, Wochenplan, Revision und
  Garmin-Payload erhalten. Die Uhr beendet einen solchen Schritt nach Distanz.
  Anpassungen entfernen ganze Wiederholungen, ohne Vorbereitung, Pausen oder Pace
  zu verschärfen. Unterhalb der Mindestarbeit bleibt eine Easy-/Ruhe-Alternative.
- Der Coach zeigt den Ablauf offen mit Dauer/Distanz und Zielbereich. Begründungen
  sind aufklappbar. Ein zugeordneter abgeschlossener Lauf erscheint in der Woche
  einmal mit Links zu Aktivität und Training. Offene Entwürfe bleiben unter
  „Alternativen“ zugänglich; nichts wird gelöscht oder automatisch angenommen.

Entwicklungsreferenzen, recherchiert am 12. September 2026:

- [Runna: Trainingsaufbau](https://support.runna.com/en/articles/15690947-understand-your-runna-workouts)
  als Referenz für verständliche Abschnitte und Ziele.
- [Runna: Pace Insights](https://support.runna.com/en/articles/14656203-what-are-pace-insights-and-how-do-they-work)
  als Produktvergleich. Ein entsprechender automatischer Lernmechanismus wurde hier
  nicht eingeführt; PacePilot verwendet seine vorhandenen Leistungsanker.
- [Furman FIRST: Intervallvarianten](https://www.furman.edu/first/wp-content/uploads/sites/168/2023/01/January-April-2023-Tuesday-Track-Workouts.pdf)
  belegt variable Zeit-/Distanzwiederholungen und Vorbereitungszeiten. Die konkreten
  PacePilot-Faktoren sind keine aus dieser Quelle abgeleiteten universellen Regeln.

## Manuelle Prüfung

1. `/coach` ohne LLM-Key öffnen: Heute-Karte mit Empfehlung/Erledigt/Geplant/Ruhe sehen.
2. Der Ablauf ist direkt sichtbar: Einlaufen, Belastung, Trabpause und Auslaufen.
   „Warum diese Einheit?“ öffnet die Begründung und Datengrundlage.
3. Zeitbudget verkleinern und aktualisieren: Gesamtdauer bleibt im Budget; zu knappe
   Qualitätsfenster werden locker, unter 20 Minuten wird Ruhe vorgeschlagen.
4. „Als Vorschlag speichern“: Workout-Detailseite zeigt einen unangenommenen,
   ungeplanten Entwurf. Dieselbe unveränderte Vorschau erneut speichern: dieselbe ID.
5. Zwischen Vorschau und Speichern relevante Daten ändern: aktuelle Vorschau statt
   stillschweigend abweichendem Entwurf. Bei angenommener Einheit bleibt deren Revision
   sichtbar; „Einheit prüfen und anpassen“ führt zu den bestehenden expliziten Aktionen.
6. Bei vorhandenem geeigneten Leistungsanker im Coach einen Vorschlag mit 400-m-
   VO₂-Intervallen oder 1000-m-Schwellenintervallen anfordern. Passendes Zeitbudget
   angeben, z. B. 60 Minuten. Vorschau und gespeichertes Workout zeigen identische
   Distanzen und persönliche Zielpaces. Bei ungeeigneten Daten/Umfang erklärt der
   Coach, warum der gewünschte Entwurf nicht erstellt werden kann.
7. Nach Annahme über die bestehenden Garmin-Aktionen übertragen: auf der Uhr Distanz
   und Zielpace der Belastungsschritte prüfen. Das ist die noch notwendige manuelle
   Geräteprüfung; automatisierte Tests verwenden keine Live-Garmin-Verbindung.
