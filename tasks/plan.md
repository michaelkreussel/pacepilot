# PacePilot: ein zusammenhängendes Lauferlebnis nach dem Vorbild Runna

Stand: 13. September 2026. Auftrag: Bestand im Agent Browser prüfen und einen
Umsetzungsplan erstellen. Arbeit direkt auf `main`, ausdrücklich vom Nutzer gewünscht.
Dies ist ein Plan, keine bereits umgesetzte Produktänderung.

## Ziel

PacePilot soll beim Öffnen beantworten: Was trainiere ich als Nächstes, warum passt
das zu mir und wie bringt mich diese Woche meinem Ziel näher? Tagesempfehlung,
Mehrwochenplan, Einheiten und Coach müssen dieselbe Trainingsentscheidung darstellen.
Die Oberfläche soll sportlich, ruhig und hochwertig wirken und die Planung verständlich
machen. Design und Trainingsqualität sind gleichwertige Abnahmekriterien.

## Prüfstand und Beleggrenzen

- `main` war zu Beginn sauber; zuletzt sichtbarer Commit: `e4e5d85`.
- Die alte Bestandsaufnahme `docs/refactoring/ai-coach-current-state.md` beschreibt
  den Stand vor dem Upgrade. Aktueller Code enthält bereits Tagesempfehlungen,
  strukturierte Coach-Karten und distanzbasierte Intervalle. Diese Fähigkeiten
  nicht erneut als fehlende Neuentwicklung planen.
- `docs/refactoring/ai-coach-intent.md` bleibt Zielbeschreibung; die früheren zwei
  Umsetzungspakete sind kein Nachweis ausreichender Produktqualität.
- Browserzugriff auf localhost gelang zunächst nur bis zur Anmeldung. Die vorliegende
  Auth-Datei war vom 25. August und wurde abgewiesen. Eine frische Anmeldung ist
  angefordert. Die persönliche Plan-/Chatprüfung ist zum Zeitpunkt dieses Entwurfs offen.
- Bis zur Browserprüfung sind Aussagen zur Oberfläche durch Templates belegt,
  nicht durch eine abgeschlossene visuelle Prüfung. Ursachen einzelner persönlicher
  Pläne bleiben Hypothesen, bis deren gespeicherter Erzeugungskontext geprüft ist.
- Runna wurde anhand öffentlicher Herstellerseiten, App-Abbildungen und Supporttexte
  betrachtet. Kein Zugriff auf ein angemeldetes Runna-Konto und keine Kenntnis der
  proprietären Trainingsberechnung.

## Konkrete Befunde im aktuellen Code

### 1. Wochenplanung verteilt Restbudget statt die Woche insgesamt auszubalancieren

`weekly_planner.py: plan_shadow_week`, `compose_week` und `_personalize_week`:
Das Wochenbudget stammt aus dem Median der letzten 28 Tage; ohne Wert greift ein
Fallback von 60 Minuten. Bereits absolvierte/geplante Arbeit wird abgezogen.
Die Verteilung erfolgt nacheinander; später übrig gebliebene Minuten können einen
kurzen Lauf ergeben oder eine Einheit entfallen lassen. Bei zu wenig Restbudget
kann auch die Rolle eines langen Laufs auf locker zurückfallen.

Prüfen: Welche Historie war vorhanden? Waren Wochen vollständig? Welche Begrenzung
wirkte zuerst? Wurde bereits berücksichtigtes Training doppelt gezählt? Ein Lauf
über 20 Minuten ist nicht grundsätzlich falsch, muss aber einen nachvollziehbaren
Zweck haben und darf kein unbemerkter Rest aus mehreren Begrenzungen sein.

### 2. Die Auswahl kann geeignete Läufer dauerhaft von bestimmten Reizen ausschließen

`daily_recommendation.py: recommend_today`: Qualitätsauswahl hängt unter anderem
von Häufigkeit, sechs konsistenten Wochen, bekannter jüngster Intensität, bisherigen
harten Einheiten und geplantem Training ab. Automatisches VO2-Training verlangt
zusätzlich acht konsistente Wochen, Pace-Evidenz und mehrere verknüpfte, erfolgreich
bewertete PacePilot-Einheiten, darunter bereits VO2-Training. Importiertes Training
ohne diese Verknüpfung genügt dafür nicht.

Das ist eine belegte Hürde; ob sie die konkreten Nutzerpläne erklärt, ist offen.
Auch die zeitliche Reichweite von `planned_quality` und das Zusammenwirken mit
der Wochenplanung müssen an Beispielen geprüft werden. Fehlende Evidenz darf
nicht pauschal als mangelnde Fähigkeit interpretiert werden.

### 3. Drei-Minuten-Intervalle sind weiterhin ein möglicher Standardpfad

`automatic_interval_parameters` beginnt ohne vergleichbare Ausführung mit
3 × 5 Minuten Schwelle bzw. 4 × 3 Minuten VO2. Explizite Chatentwürfe nutzen eigene
Template-Defaults. `interval_prescription.py` bietet bereits Distanzvarianten,
wählt deren Reihenfolge aber über die Kalenderwoche. Ohne passende Pace-Evidenz
oder bei unpassendem Budget kann es bei zeitbasierten Defaults bleiben.

Die Lösung ist weder ein Verbot von Drei-Minuten-Intervallen noch Zufallsvariation.
Reiz, Wiederholungslänge, Arbeitsumfang und Pause müssen zum Ziel und Leistungsstand
passen. Wiederholung darf bewusst sein; eine verlangte Alternative braucht eine
tatsächliche, begründete Änderung. Auch der gespeicherte Chatentwurf kann älter als
das zuletzt ergänzte Distanz-Feature sein: Erzeugungszeit und Version mitprüfen.

### 4. Die Oberfläche zeigt technische Verwaltung vor Trainingsinhalt

In `plans/cycle.html` stehen unter anderem „Confidence“, „Planrevision“ und
„Materiell“. Die Einheitenübersicht nennt überwiegend Name, Datum und Annahmestatus.
Umfang, Pace und Belastungsstruktur fehlen dort als direkt vergleichbare Information.
`plans/cycle_new.html` erklärt „Re-Entry, Base, Build, Specific und Taper“ und setzt
ein separat angelegtes Ziel voraus. `planning_inputs.html` beginnt mit Leistungsankern.
Der Kalender braucht in Wochen- und Monatsansicht mindestens 64 rem Breite.

Das erzeugt mehrere getrennte Einstiege und stellt interne Abläufe vor die Fragen
eines Läufers. Bereits vorhandene strukturierte Coach-Karten sind ein Ausgangspunkt,
kein ausreichendes Designsystem für den gesamten Bereich.

## Runna als Referenz

- [Navigation](https://support.runna.com/en/articles/10473504-your-quick-guide-to-navigating-the-runna-app):
  Tagestraining, Plan, Kalender und Fortschritt haben erkennbare Aufgaben.
- [Personalisierung](https://support.runna.com/en/articles/15231838-how-does-runna-build-your-training-plan-around-your-current-fitness):
  aktueller Umfang, längster Lauf, Erfahrung, Leistungen und Präferenzen prägen den Start.
- [Einheiten](https://support.runna.com/en/articles/15690947-understand-your-runna-workouts):
  Vorbereitung, Arbeitsblöcke, Pausen und Abschluss sind vor dem Training sichtbar.
- [Soforttraining](https://support.runna.com/en/articles/10116460-how-to-use-instant-workouts):
  einzelne passende Einheiten sind neben dem Hauptplan auffindbar.
- [Öffentliche Gestaltung](https://www.runna.com/): klare Typografie, starke Hierarchie,
  großzügige Flächen, erkennbare Trainingskarten und sparsame Akzente.

Übernehmen: Informationshierarchie, Übersichtlichkeit und konkrete Trainingsführung.
Eine eigene PacePilot-Gestaltung entwickeln; keine Markenassets kopieren und keine
Gleichwertigkeit mit Runnas Trainingssystem behaupten.

## Vorgeschlagene Produktstruktur

| Einstieg | Inhalt | Bestehende Bereiche |
| --- | --- | --- |
| Heute | Nächste Einheit/Ruhetag, persönliche Gründe, Wochenfortschritt, Anpassung | Coach-Übersicht weiterentwickeln |
| Mein Plan | Aktives Ziel, Phase, aktuelle Woche, weitere Wochen, Anpassungen | Kalender und Mehrwochenplan zusammenführen |
| Einheiten | Geeignete Vorschläge, Alternativen, eigene Einheiten, manueller Editor | Workout-Erstellung in sinnvollen Einstieg einbetten |
| Coach fragen | Gespräch zur ausgewählten Einheit oder zum Plan, frühere Chats | Bestehenden Chat kontextbezogen öffnen |

„Trainingsgrundlagen“ wird zu „Laufprofil“ innerhalb der Planeinstellungen.
Planerstellung führt in wenigen Schritten durch Ziel → Ausgangslage → Trainingstage
→ Vorschau. Vorhandene Garmin-Daten werden mit Quelle und Zeitraum vorausgefüllt;
der Nutzer kann falsche Annahmen gezielt korrigieren. Keine erneute Vollerfassung.
Das bestehende allgemeine Dashboard, Analyse und Aktivitäten bleiben erhalten.

Einheiten sind eine kuratierte Auswahl mit erkennbarem Zweck, keine neue öffentliche
Trainingsplanbörse. Teilen, Community und Marketplace gehören nicht zu diesem Auftrag.

## Gestaltung, die konkret geprüft werden kann

1. Oberhalb des ersten Scrollens: Ziel/aktuelle Woche, nächstes Training, Hauptaktion.
2. Einheitentitel beschreibt den Inhalt; darunter Distanz, geschätzte Gesamtdauer,
   Zielintensität und ein kurzer persönlicher Grund. Keine erfundenen Kilometer bei
   unbekannter Pace; Schätzungen kennzeichnen.
3. Ein gemeinsamer Kartenstil in Heute, Plan, Einheiten und Chat; ein gemeinsamer
   Detailaufbau für Aufwärmen, Belastung, Pause und Auslaufen.
4. Ruhiger neutraler Hintergrund, großzügige Abstände, markante gut lesbare Titel,
   wenige Rahmen und eine klare Akzentfarbe. Trainingsarten zusätzlich über Text/Icon
   unterscheiden; Statusfarben nicht mit Belastungsarten verwechseln.
5. Desktop: aktuelle Woche und Trainingsdetail nebeneinander. Mobil: vertikale
   Tagesagenda, erreichbare Hauptaktion; keine erzwungene breite Wochenmatrix.
6. Technische Versionen nur in optionalen Details. Deutsche Alltagssprache:
   „Dein Plan“, „Plan übernehmen“, „Wettkampfvorbereitung“, „Deine aktuellen Laufzeiten“.
7. Jeder Zustand ist gestaltet: kein Plan, Vorschau, aktiv, erledigt, Ruhetag,
   Daten fehlen, Änderung angeboten, Fehler. Bestehende Entwürfe bleiben auffindbar.

Vor der breiten Umsetzung ein klickbarer Entwurf für Heute → Woche → Trainingsdetail
und Planerstellung, jeweils Desktop und Mobil. Beispieldaten eindeutig kennzeichnen.
Damit wird die Designrichtung an echten Ansichten beurteilt, bevor alle Seiten umgebaut werden.

## Trainingslogik: gewünschtes Verhalten

- Wochen zuerst als zusammenhängenden Rhythmus aus Ziel, Umfang, Lauftagen, längerem
  Lauf, passenden Reizen und Erholung planen. Das Budget über alle Einheiten verteilen.
- Historische Ausgangslage, gewünschte Entwicklung, Zeitlimit und Erholungsanpassung
  getrennt behandeln. Keine Kaskade stiller Reduktionen ohne sichtbaren Grund.
- Importierte Läufe und belastbare Leistungsdaten zur Eignung nutzen; erfolgreiche
  PacePilot-Ausführung ist zusätzliche Evidenz und keine Eintrittskarte für Training.
- Die konkrete Trainingspolitik (Dosierung, Progression, Belastungsabstände) anhand
  bestehender Projektforschung und geeigneter Primärquellen prüfen. Keine universelle
  Intensitätsquote, starre Mindestlauflänge oder automatisch höhere Belastung festlegen.
- Innerhalb vorhandener Formate zielgerichtete Zeit-/Distanzvarianten wählen;
  identischer Kontext bleibt stabil, explizite Alternativen verändern etwas Sinnvolles.
- Vorschau, gespeicherte Einheit, Chatdarstellung und Garmin-Export müssen denselben
  Trainingsinhalt enthalten. Numerische Ausführung bleibt im Code, Erklärung im Coach.
- Jede Entscheidung benennt die wichtigsten verwendeten Daten und den tatsächlichen
  begrenzenden Faktor. Lücken offenlegen, ohne Erfahrung pauschal herabzustufen.
- Fortschritt berücksichtigt absolvierte Arbeit und Rückmeldung. Eine Woche mit
  Veränderungen wird nachvollziehbar angepasst; nichts wird automatisch nachgestapelt.

## Umsetzung in überprüfbaren Schritten

Die vollständigen Aufgaben mit Dateien, Abhängigkeiten und Abnahme stehen in
[todo.md](todo.md). Reihenfolge:

1. Persönliche Browserfälle dokumentieren und Ursachen reproduzierbar machen.
2. Klickbaren Entwurf für den zusammenhängenden Trainingsbereich erstellen.
3. Wochenumfang und Verteilung korrigieren; Ergebnis in der Wochenübersicht zeigen.
4. Eignung und Intervallauswahl verbessern; persönliche Begründung mitliefern.
5. Einheitliche Trainingsdarstellung auf Heute, Plan und Chat ausrollen.
6. Geführte Planerstellung und Laufprofil verbinden.
7. Passende Einheiten und kontextbezogenen Coach in den Ablauf integrieren.
8. Plananwendung und Anpassung vereinfachen, abschließend visuell und fachlich prüfen.

Keine parallele neue Planungsengine, keine neuen Laufzeitabhängigkeiten und kein
Frameworkwechsel. Bestehende Revision-Metadaten und Services nutzen. Neue persistente
Felder nur bei nachgewiesenem Bedarf; dann Modell und Migration zusammen planen.
Planübernahme und einzelne Workout-Freigabe sind heute getrennt. Eine gemeinsame
explizite Aktion kann später die Auswahl von Einheiten bestätigen und einplanen,
muss aber als bewusste Verhaltensänderung umgesetzt und geprüft werden. Garmin bleibt
explizit; akzeptierte Revisionen werden niemals still überschrieben.

## Abnahme aus Nutzersicht

- Ein geeigneter regelmäßig laufender Testathlet erhält in einer Aufbauphase eine
  nachvollziehbare Mischung und Entwicklung; ein reiner Easy-Plan braucht einen
  konkreten Grund. Der Wiedereinsteiger bleibt als eigener Fall geschützt.
- Kurze Läufe haben einen erkennbaren Zweck; das Restbudget erzeugt keine unbemerkten
  Minieinheiten. Ein unpassender Zeitrahmen führt zu einer verständlichen Alternative.
- „Andere Intervalle“ bietet eine strukturell andere geeignete Einheit oder erklärt
  den Hinderungsgrund. Drei-Minuten-Intervalle dürfen weiterhin begründet vorkommen.
- Öffnen → Heute → Einheit: Umfang, Intensität, Schritte und Grund sofort verständlich.
- Mein Plan → nächste Woche: Trainingsmix und Wochenumfang ohne Öffnen jeder Karte sichtbar.
- Plan erstellen: Ziel und Laufprofil können ohne Sackgasse ergänzt werden.
- Übernahme, Kalender und Trainingsdetail widersprechen sich nicht; kein Garmin-Versand
  durch bloßes Anzeigen oder Generieren einer Vorschau.

## Verifikation und offene Punkte

Für die Planungsdokumentation sind keine Anwendungstests erforderlich. Spätere
Implementierungen nutzen fokussierte Verhaltenstests und am Ende die vollständigen
Prüfungen aus `AGENTS.md`: pytest, Ruff lint/format und ty. Nach Templates/CSS die
Styles bauen und Cache-Schlüssel aktualisieren. Browserprüfung dieses Auftrags:
Desktop 1440 px und Mobil 390 px, Tastaturbedienung und verständliche Zustände.

Offen: persönliche Browserbeispiele samt Erzeugungskontext; Bewertung des klickbaren
Designentwurfs. Die bestätigte Referenz ist Runna. Die Arbeit findet auf `main` statt.
