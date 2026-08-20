# Garmin AI Running Coach
## Konsolidierter Research-, Produkt- und Implementierungsreport

**Stand:** 20. August 2026  
**Zielsystem:** serverseitige Python-Webapplikation mit `cyberjunky/python-garminconnect`, bestehendem manuellen Workout-Builder, Garmin-Synchronisation und LangChain-basiertem AI Coach  
**Primäres Ziel:** sichere, nachvollziehbare und schrittweise individualisierte Lauftrainingsvorschläge – zunächst einzelne Einheiten und Tagesanpassungen, später vollständige adaptive Pläne für 5 km, 10 km, Halbmarathon und Marathon

> Dieser Report führt die beiden Research-Stränge zu einem einheitlichen Handoff zusammen: Trainingswissenschaft und Garmin-Daten auf der einen Seite, Produkt-UX, Human-in-the-loop und Softwarearchitektur auf der anderen. Er ist bewusst keine bloße Aneinanderreihung, sondern eine konsolidierte Zielarchitektur mit klaren Entscheidungen, Datenmodellen, Regeln, Prompts und Arbeitspaketen.

---

## Inhaltsverzeichnis

1. Executive Summary
2. Leitentscheidungen
3. Evidenzrahmen und wichtige Unsicherheiten
4. Wissenschaftlich begründete Trainingsprinzipien
5. Zielabhängige Struktur für 5 km, 10 km, Halbmarathon und Marathon
6. Workout-Taxonomie und parametrisierbare Einheiten
7. Individualisierung der Intensität
8. Garmin-Daten: Relevanz, Priorisierung und technische Verfügbarkeit
9. Athlete Baseline und Mindestdaten
10. Guardrails und Sicherheitslogik
11. Produkt- und UX-Konzept
12. Gemeinsames Workout-Domainmodell
13. Proposal-, Revision-, Acceptance- und Sync-State-Machine
14. Daily Adaptation im Chat
15. Deterministische Engine versus LLM
16. Zielarchitektur und Datenfluss
17. Pydantic-/JSON-/YAML-Beispiele
18. API- und Tool-Calling-Design
19. Knowledge-Base- und Repository-Struktur
20. Direkt nutzbare OpenCode-Instructions
21. Schrittweise OpenCode-Arbeitsaufträge
22. Teststrategie
23. MVP-Roadmap und Arbeitspakete
24. Definition of Done
25. Offene Entscheidungen
26. Quellen und weiterführende Literatur

---

# 1. Executive Summary

Der Coach sollte **nicht** als autonomer LLM-Planer gebaut werden, der aus Garmin-Rohdaten frei Trainings erfindet. Die robuste Lösung ist ein hybrides System:

1. Eine deterministische Domain- und Constraint-Engine berechnet Intensitätsbereiche, wählt zulässige Workout-Templates, prüft Progression, Abstände, Umfang, Datenqualität und Sicherheitsregeln.
2. Ein kanonisches `WorkoutPrescription`-Modell wird von allen Erstellungswegen genutzt: manueller Builder, Einzelvorschlag des Coaches, Tagesanpassung und später Plan-Generator.
3. Das LLM interpretiert Ziele und Freitext, stellt gezielte Rückfragen, erklärt Optionen und formuliert Begründungen. Es darf keine kritische Mutation direkt ausführen.
4. Coach-Ergebnisse sind strukturierte `WorkoutProposal`-Objekte. Der Nutzer kann sie in derselben UI ansehen und bearbeiten, die heute für manuelle Workouts existiert.
5. Erst die ausdrückliche Annahme einer konkreten Revision erlaubt Scheduling oder Garmin-Sync. Eine Chat-Antwort allein darf niemals als Zustimmung gelten.
6. Garmin-Scores wie Training Readiness, Body Battery, Stress, Training Load oder VO2max sind nützliche sekundäre Signale, aber keine alleinige Entscheidungsinstanz. Tatsächliche Laufhistorie, Trainingskonsistenz, aktuelle Leistungsdaten, subjektives Befinden und Schmerz-/Krankheitssignale haben Vorrang.
7. Die bekannten populären Regeln „80/20“, „jede Woche maximal 10 % mehr“ und „ACWR sagt Verletzungen voraus“ dürfen nicht als starre Naturgesetze codiert werden. Sie werden als Kontext oder weiche Heuristik behandelt; harte Grenzen müssen transparent, baseline-relativ und konfigurierbar sein.
8. Der MVP sollte keine komplette adaptive Marathonplanung beginnen. Die richtige Reihenfolge lautet:
   - vorhandenen Workout-Builder analysieren,
   - gemeinsames Domainmodell extrahieren,
   - einen validierten Coach-Vorschlag erzeugen,
   - Preview/Edit/Accept in der bestehenden UI ermöglichen,
   - über die bestehende Garmin-Pipeline synchronisieren,
   - anschließend Tagesanpassungen im Chat implementieren,
   - erst danach Wochen- und Mehrwochenpläne.

Die wissenschaftliche Grundlage spricht für hohe Anteile lockeren Trainings, regelmäßige aber begrenzte Qualitätsreize, harte Tage mit ausreichender Erholung, schrittweise Spezifität in Richtung Zielwettkampf und einen Taper vor wichtigen Rennen. Die Forschung liefert jedoch selten eine universell optimale exakte Einheit. Deshalb gehören konkrete Bereiche in versionierte Templates mit Evidenzgrad und nicht als vermeintlich exakte, unveränderliche Zahlen in den Prompt.

---

# 2. Leitentscheidungen

## 2.1 Produktentscheidungen

| Thema | Entscheidung |
|---|---|
| Workout-Editor | Bestehenden manuellen Editor generalisieren; keinen Coach-spezifischen zweiten Editor bauen |
| Workout-Modell | Ein kanonisches `WorkoutPrescription` für `manual`, `coach`, `plan` und `daily_adaptation` |
| Coach-Ausgabe | Immer strukturiertes Proposal plus verständliche Erklärung; nie nur Chattext |
| Nutzerkontrolle | Preview, Edit, Accept, Reject; Sync nur nach expliziter Bestätigung |
| Tagesanpassung | Regel-Engine erzeugt/validiert Alternativen; LLM moderiert die Auswahl |
| Mehrwochenplanung | Erst nach stabilen Einzelworkout- und Adaptation-Flows |
| Daten | Normalisierte Domain-Snapshots statt Garmin-Rohdicts in der Fachlogik |
| Integrationsgrenze | Eigener Garmin-Adapter/Anti-Corruption-Layer um `python-garminconnect` |
| Evidenz | Regeln und Templates tragen Quellen-ID, Stärke, Version und Begründung |
| Sicherheit | Schmerz, Krankheit und Warnsymptome überstimmen Wearable-Readiness |

## 2.2 Trainingsentscheidungen

| Thema | Entscheidung |
|---|---|
| Intensitätsmodell | Intern primär Drei-Domänen-Modell; Darstellung optional als 5 Zonen |
| Primäre Steuerung | Aktuelle Wettkampf-/Time-Trial-/Critical-Speed-Daten und Laktatschwelle, sofern belastbar |
| Fallback | RPE und Talk Test; Herzfrequenz als zweites Signal, nicht als alleinige Wahrheit |
| Wochenstruktur | Überwiegend locker; typischerweise 1–2 Qualitätsreize für Freizeitläufer, abhängig von Frequenz und Erfahrung |
| Progression | Baseline-relatives Änderungsbudget; keine starre 10-%-Regel |
| Belastung | Umfang, Intensität und Dichte getrennt überwachen; nicht gleichzeitig aggressiv erhöhen |
| Daily Adaptation | Standardmäßig nur gleich belastend oder entlastend; keine spontane Eskalation |
| Garmin-Scores | Sekundäre Modifikatoren; nie alleinige Freigabe oder Sperre |

---

# 3. Evidenzrahmen und wichtige Unsicherheiten

## 3.1 Evidenzhierarchie

Für die Regelbasis sollte jede Aussage einer Kategorie zugeordnet werden:

- **A – starke Evidenz:** systematische Reviews, Meta-Analysen, Konsensuspapiere oder robuste, mehrfach bestätigte Befunde.
- **B – moderate Evidenz:** mehrere Studien oder große Beobachtungsdatensätze mit plausibler Übereinstimmung.
- **C – begrenzte Evidenz:** kleine Studien, spezifische Populationen, indirekte Übertragbarkeit.
- **D – Coaching-Heuristik:** praxiserprobte Defaultwerte ohne Nachweis einer universellen Optimalität.

Zusätzlich benötigt jede maschinenlesbare Regel eine Ausprägung:

- `hard_constraint`: darf ohne explizite fachliche Regeländerung nicht verletzt werden;
- `soft_constraint`: erzeugt Warnung oder verlangt Begründung;
- `heuristic`: dient der Rangfolge, ist aber kein Ausschlusskriterium;
- `explanatory_only`: darf erklärt, aber nicht als Steuergröße verwendet werden.

## 3.2 Zentrale Unsicherheiten

1. **Elite versus Freizeitläufer:** Viele Trainingsverteilungsstudien untersuchen hochtrainierte Athleten. Deren Volumen und doppelte Qualitätstage dürfen nicht auf Anfänger übertragen werden.
2. **Beobachtung versus Ursache:** Schnellere Läufer trainieren meist mehr und lockerer. Daraus folgt nicht, dass jeder Läufer durch dasselbe Volumen sicher schneller wird.
3. **Wearable-Algorithmen:** Garmin-Kompositwerte sind proprietär und geräteabhängig. Sie sind nützlich, aber keine medizinisch oder trainingswissenschaftlich vollständigen Messungen.
4. **Zonenbegriffe:** „Zone 2“ kann je nach 3-, 5- oder 7-Zonen-Modell etwas anderes bedeuten. Die Engine muss die zugrunde liegenden physiologischen Grenzen speichern, nicht nur eine Zonen-Nummer.
5. **Verletzungsrisiko:** Laufverletzungen sind multifaktoriell. Es gibt keine einzelne Wochenkilometer-, ACWR- oder 10-%-Schwelle, die Verletzungen zuverlässig vorhersagt.
6. **Workout-Details:** Für exakte Kombinationen wie „6 × 800 m mit 400 m Trab“ existiert selten ein Beleg, dass genau diese Variante optimal ist. Solche Angaben sind Templates innerhalb plausibler Belastungsbereiche.

## 3.3 Konsequenz für die Implementierung

Die Knowledge Base muss zwischen **wissenschaftlichem Prinzip** und **konkreter Produktkonfiguration** unterscheiden. Beispiel:

- Prinzip: Ein großer Teil des Ausdauertrainings sollte unterhalb der ersten Schwelle stattfinden.
- Produktkonfiguration: Für einen konsistent trainierenden Freizeitläufer werden initial 75–90 % der Laufzeit als locker geplant.
- Status: erster Satz Evidenz B/A; zweiter Satz Heuristik D mit konfigurierbarem Bereich.

---

# 4. Wissenschaftlich begründete Trainingsprinzipien

## 4.1 Spezifität

Training sollte im Verlauf eines Zyklus zunehmend den Anforderungen des Zielwettkampfs ähneln. Das bedeutet nicht, ständig im Wettkampftempo zu laufen. Vielmehr werden Umfang, Dauer, Intensitätsdomäne, Pausenstruktur, Ermüdungsresistenz und Verpflegung schrittweise spezifischer.

- 5 km: stärkere Betonung von VO2max-nahen Intervallen, Laufökonomie, Geschwindigkeit und 5-km-spezifischem Tempo.
- 10 km: Balance aus Schwelle, 10-km-Tempo, aerober Basis und etwas VO2max-Arbeit.
- Halbmarathon: Schwelle, längere kontrollierte Tempoabschnitte, lange Läufe und Ermüdungsresistenz.
- Marathon: robuste Wochenkonsistenz, hoher lockerer Anteil, lange Läufe, Marathonpace unter Vorermüdung, Energie-/Flüssigkeitsstrategie und Taper.

## 4.2 Überlastung und Anpassung

Leistungsfortschritt benötigt einen ausreichenden Reiz, aber Anpassung entsteht in der Erholung. Die Engine sollte nicht nur Wochenkilometer betrachten, sondern mindestens:

- Gesamtdauer und Distanz,
- Anzahl Lauftage,
- längste Einheit,
- Zeit oberhalb LT1 und LT2,
- Dichte harter Tage,
- subjektive Belastung `session_RPE × duration`,
- tatsächliche Ausführung versus Planung,
- Trend der letzten 4–8 Wochen,
- Krankheit, Schmerz und Unterbrechungen.

## 4.3 Überwiegend lockeres Training

Reviews zu trainierten Distanzläufern zeigen typischerweise pyramidenförmige oder polarisierte Verteilungen mit dem größten Volumen unterhalb der ersten Schwelle. Eine sinnvolle Produktregel ist daher nicht „exakt 80/20“, sondern:

- Der überwiegende Anteil wird locker absolviert.
- Qualitätsvolumen wird begrenzt und gezielt eingesetzt.
- Die Verteilung hängt von Trainingsalter, Wochenfrequenz, Ziel und Phase ab.
- Zeit in Zonen ist aussagekräftiger als die bloße Anzahl „harter Einheiten“.

Für Freizeitläufer ist als initialer Planungsbereich häufig **75–90 % der Laufzeit unterhalb LT1** plausibel. Das ist ein konfigurierbarer Default, kein universelles Gesetz.

## 4.4 Hard-day/easy-day

Qualitätsreize werden durch lockere Tage oder Ruhetage getrennt. Für typische Freizeitläufer ist ein Default von mindestens etwa 48 Stunden zwischen zwei anspruchsvollen Laufeinheiten vernünftig; dies ist eine konservative Coaching-Heuristik, keine harte biologische Konstante. Fortgeschrittene Athleten können andere Mikrozyklen vertragen, Anfänger benötigen oft mehr Erholung.

## 4.5 Progression

Die starre 10-%-Regel reduziert Verletzungen nicht zuverlässig und darf daher nicht als universeller Algorithmus dienen. Stattdessen:

1. Baseline aus mehreren Wochen robust bestimmen.
2. Änderungsbudget nach Konsistenz, Erfahrung, Unterbrechungen und Zielphase festlegen.
3. Pro Woche möglichst nur eine Hauptdimension deutlich verändern: Umfang, Intensität oder Dichte.
4. Entlastungswochen oder stabile Wochen einplanen.
5. Nach Krankheit, Schmerz oder längerer Pause Baseline neu kalibrieren.

Ein mögliches Produkt-Default ist ein **kleines, variables Änderungsbudget von etwa 5–15 %** für gut verträgliche Wochenvolumina. Dieser Bereich ist ausdrücklich eine Heuristik. Die Engine muss ihn anhand der Historie reduzieren, auf null setzen oder eine stabile Woche wählen können.

### 4.5.1 Wichtige Ergänzung: Distanzspitzen in einer einzelnen Einheit

Eine große prospektive Kohortenstudie aus 2025 mit 5.205 erwachsenen Läufern und 588.071 aufgezeichneten Einheiten fand höhere Raten selbstberichteter Überlastungsverletzungen, wenn die Distanz **einer einzelnen Einheit** die längste Distanz der vorausgegangenen 30 Tage um mehr als 10 % überschritt. Für Sprünge von >10–30 %, >30–100 % und >100 % wurden erhöhte Hazard-Rate-Ratios berichtet. Dagegen zeigte sich kein entsprechender positiver Zusammenhang für reine Woche-zu-Woche-Änderungen; beim ACWR war der Zusammenhang sogar invers.

Das ist **keine Wiederbelebung einer universellen wöchentlichen 10-%-Regel** und auch kein Beleg, dass eine Steigerung bis 10 % sicher ist. Die Studie ist beobachtend, Verletzungen wurden selbst berichtet, und die Stichprobe bestand überwiegend aus erfahrenen, mittelalten männlichen Läufern. Für das Produkt folgt dennoch ein relevanter, transparenter Guardrail:

- Vergleiche die geplante Distanz jeder Einheit mit dem längsten Lauf der letzten 30 Tage.
- Bei `candidate_distance > 1.10 × max_distance_last_30d` wird mindestens eine deutliche Warnung und erneute Prüfung ausgelöst.
- Für Anfänger, Wiedereinsteiger, Schmerz-/Verletzungshistorie oder geringe Datenabdeckung wird konservativer reagiert.
- Die Schwelle ist ein **Risikomarker**, keine sichere Grenze; Intensität, Höhenmeter, Untergrund, Schuhe und Erholung bleiben zusätzlich relevant.
- Eine Überschreitung sollte im MVP nicht automatisch durch das LLM freigegeben werden. Sie benötigt eine deterministisch dokumentierte Reduktion, fachliche Ausnahme oder ausdrückliche Nutzerentscheidung innerhalb zulässiger Produktregeln.

## 4.6 Periodisierung

Eine robuste, einfach erklärbare Periodisierung besteht aus:

- **Baseline-/Onboarding-Phase:** Datenqualität prüfen, aktuelle Belastbarkeit verstehen, keine aggressive Spezifität.
- **Base:** Konsistenz, lockeres Volumen, Strides, kurze Hügel, Technik- und Kraftgrundlage.
- **Build:** graduell mehr Schwellen- und zielrelevante Belastung.
- **Specific:** wettkampfspezifische Pace, Dauer und Ermüdungsresistenz.
- **Taper:** Ermüdung abbauen, Intensität teilweise erhalten, Volumen reduzieren.
- **Recovery/Transition:** nach Zielwettkampf reduzierte und flexible Belastung.

Die Literatur zu Tapering unterstützt bei Ausdauerathleten häufig eine progressive Volumenreduktion im Bereich von ungefähr 41–60 %, bei erhaltener Intensität und weitgehend erhaltener Frequenz über bis zu etwa drei Wochen. Für die App sollte dies als Bereich und nicht als exakte Pflichtkurve modelliert werden. Kürzere Rennen benötigen meist kürzere Taper als ein Marathon.

## 4.7 Belastungssteuerung über mehrere Signale

Eine einzelne Metrik darf nie dominieren. Die Engine kombiniert:

- **externe Belastung:** Zeit, Distanz, Geschwindigkeit, Höhenmeter;
- **interne Belastung:** Herzfrequenz, RPE, sRPE;
- **Leistungsfähigkeit:** aktuelle Rennen, Time Trials, Critical Speed, Schwelle;
- **Erholung:** subjektives Befinden, Schlaftrend, RHR-/HRV-Trend;
- **Kontext:** Hitze, Gelände, Reisen, verfügbare Zeit;
- **Warnsignale:** Schmerz, Krankheit, deutliche Leistungseinbrüche.

## 4.8 Hitze und Umwelt

In Hitze steigt die interne Belastung bei gleicher Pace. Deshalb:

- Pace-Ziele in Hitze als sekundär behandeln;
- RPE, Talk Test und Herzfrequenz stärker gewichten;
- Alternativen mit kürzerer Dauer, früherer Uhrzeit oder Indoor-Option anbieten;
- bei wiederholtem Training in Hitze eine Akklimatisierungsphase von ungefähr 1–2 Wochen berücksichtigen;
- keine starre universelle Temperaturgrenze codieren, sondern lokale Bedingungen, Luftfeuchte, direkte Sonne und Athletenhistorie einbeziehen.

---
# 5. Zielabhängige Struktur für 5 km, 10 km, Halbmarathon und Marathon

Die folgenden Strukturen sind **Produktdefaults**, keine universell optimalen Rezepte. Der Generator muss sie an Trainingshäufigkeit, Basis, Zielzeit, Zieltermin und Belastbarkeit anpassen.

## 5.1 Gemeinsamer Kern

Jeder Zieltyp benötigt:

- überwiegend lockeres Laufen;
- einen längeren aeroben Reiz, dessen Dauer relativ zur Historie wächst;
- begrenzte Qualitätsarbeit;
- Strides oder kurze Hügel für neuromuskuläre Qualität, sofern verträglich;
- einfache Wochen, wenn Ermüdung oder Lebenskontext dies verlangen;
- zunehmende Wettkampfspezifität;
- einen Taper vor dem Zielrennen.

## 5.2 Zielmatrix

| Ziel | Primäre Anpassungen | Typische Key Sessions | Long-Run-Rolle | Besonderheiten |
|---|---|---|---|---|
| 5 km | VO2max-nahe Leistung, Laufökonomie, Schwelle, Geschwindigkeit | 2–5-min-Intervalle, kurze Repetitions, Schwellenintervalle, 5-km-Pace | aerobe Basis erhalten, meist weniger dominant als beim Marathon | höhere mechanische Intensität; gutes Warm-up und konservative Einführung |
| 10 km | Schwelle, 10-km-spezifische Ausdauer, VO2max-Unterstützung | Cruise Intervals, 1–2-km-Wiederholungen, Tempodauerlauf, 10-km-Pace | stabiler aerober Pfeiler | Balance zwischen Schwelle und oberer Intensität |
| Halbmarathon | Schwelle, lange kontrollierte Abschnitte, Ermüdungsresistenz | längere Cruise Intervals, HM-Pace-Blöcke, Progression Runs | zunehmend wichtig | Fueling je nach Dauer testen; Tempo nicht in jeden langen Lauf integrieren |
| Marathon | Wochenvolumen, Durability, lange Läufe, Marathonpace, Fueling | Long Runs, Marathonpace-Blöcke, Schwelle zur Erhaltung, moderate Progression | zentral, aber baseline-relativ | Zeit auf den Beinen, Verpflegung, Taper und Ausfallmanagement besonders wichtig |

## 5.3 Planlänge als Default

Mögliche initiale Produktbereiche:

- 5 km: 8–16 Wochen;
- 10 km: 8–16 Wochen;
- Halbmarathon: 10–18 Wochen;
- Marathon: 14–24 Wochen.

Diese Werte sind organisatorische Defaults. Der Coach muss einen Plan ablehnen oder eine Base-Phase vorschalten, wenn die vorhandene Zeit und Baseline nicht zu einem sicheren Aufbau passen.

## 5.4 Wochenfrequenz

### Drei Lauftage

- ein lockerer Lauf;
- eine Qualitäts- oder moderat spezifische Einheit;
- ein langer Lauf;
- nicht automatisch zwei harte Einheiten plus Long Run.

### Vier Lauftage

- zwei lockere Läufe;
- eine Qualitätseinheit;
- ein langer Lauf;
- gelegentlich Strides in einem lockeren Lauf.

### Fünf bis sechs Lauftage

- mehrere lockere Läufe;
- ein bis zwei Qualitätsreize;
- ein langer Lauf;
- mehr Flexibilität für Recovery Runs und spezifische Blöcke.

Die Anzahl Qualitätstage wird nicht allein aus der Zahl der Lauftage abgeleitet. Trainingsalter, aktuelle Intensitätsminuten, Verletzungshistorie und Erholung sind zwingende Inputs.

## 5.5 Beispielhafte Phasenverschiebung

| Phase | 5 km / 10 km | Halbmarathon | Marathon |
|---|---|---|---|
| Base | easy, strides, kurze Hügel, lockerer Long Run | easy, strides, Long Run | Konsistenz, easy, Long Run, Fueling-Basis |
| Build | Schwelle plus VO2-Intervalle | Schwelle plus längere kontrollierte Läufe | Volumen und Long Run, moderate Schwelle |
| Specific | 5-/10-km-Pace und Race-Simulation in Teilstücken | HM-Pace-Blöcke und Schwellenarbeit | Marathonpace-Blöcke, lange Läufe mit kontrollierter Endbeschleunigung |
| Taper | Volumen reduzieren, kurze Schärfe erhalten | Volumen reduzieren, Race Pace dosiert erhalten | stärkere Volumenreduktion, einzelne kurze spezifische Reize erhalten |

---

# 6. Workout-Taxonomie und parametrisierbare Einheiten

## 6.1 Modellierungsprinzip

Jeder Workout-Typ sollte mindestens enthalten:

- `purpose`: gewünschte Anpassung;
- `eligibility`: Mindestvoraussetzungen;
- `intensity_domain`;
- `duration_or_distance_range`;
- `work_recovery_structure`;
- `progression_axes`;
- `weekly_frequency_cap`;
- `contraindications`;
- `fallback_targets`;
- `evidence_refs`;
- `rule_strength`;
- `garmin_capabilities`.

Die Bereiche unten sind Startwerte für Templates. Der Validator entscheidet, ob eine konkrete Instanz zur Baseline passt.

## 6.2 Easy Run

**Zweck:** aerobe Grundlage, zusätzliche Laufökonomie, aktive Erholung, Volumen ohne großen metabolischen Stress.

- Intensität: unter LT1; RPE meist 2–3/10; vollständige Sätze im Talk Test.
- Dauer: häufig 20–90 Minuten, abhängig von Erfahrung und Rolle im Wochenplan.
- Steuerung: primär RPE/Talk Test, sekundär Pace oder Herzfrequenz.
- Progression: zuerst Häufigkeit und Dauer, nicht Pace erzwingen.
- Häufigkeit: kann den größten Anteil der Woche bilden.
- Risiko: „easy“ darf nicht zum unbemerkten mittelharten Dauerlauf werden.

## 6.3 Recovery Run

**Zweck:** sehr niedrige Belastung zwischen anspruchsvollen Einheiten; optional statt vollständigem Ruhetag bei gut trainierten Läufern.

- Intensität: unterer Bereich der lockeren Domäne; RPE 1–2/10.
- Dauer: oft 15–45 Minuten.
- Zulässigkeit: nur, wenn leichtes Laufen tatsächlich erholsam und gewohnt ist.
- Alternative: Ruhetag oder Spaziergang.
- Risiko: Für Anfänger ist ein zusätzlicher Lauftag nicht automatisch Erholung.

## 6.4 Long Run

**Zweck:** aerobe Ausdauer, Ermüdungsresistenz, muskuläre Belastbarkeit, für längere Ziele zusätzlich Verpflegungs- und Pacingpraxis.

- Intensität: überwiegend unter LT1; optional kontrollierte Abschnitte bei erfahrenen Läufern.
- Dauer: baseline-relativ; häufig ungefähr 60–180 Minuten. Längere Einheiten sind Spezialfälle.
- Progression: Dauer, Häufigkeit, spezifische Abschnitte oder Fueling jeweils getrennt steigern.
- Frequenz: meist einmal pro Woche oder in längeren Mikrozyklen.
- Guardrail: keine universelle „30-%-der-Woche“-Grenze. Vergleiche mit jüngster Long-Run-Historie und Gesamtbelastung.
- Marathon: lieber Time-on-feet und Verträglichkeit als eine dogmatische 32-km-Pflicht.

## 6.5 Steady Run / Aerobic Moderate

**Zweck:** kontrollierte aerobe Belastung oberhalb easy, aber klar unter Schwelle; ökonomisches längeres Laufen.

- Intensität: oberes Ende unter LT1 oder untere mittlere Domäne, je nach Modell; RPE 4–5/10.
- Dauer: etwa 20–60 Minuten im Hauptteil.
- Einsatz: gezielt, nicht als Standardtempo aller Läufe.
- Risiko: zu häufige moderate Läufe reduzieren die Erholung, ohne einen klaren Qualitätsreiz zu setzen.

## 6.6 Continuous Threshold / Tempo Run

**Zweck:** Fähigkeit verbessern, einen hohen Anteil der aeroben Leistung kontrolliert zu halten.

- Intensität: nahe LT2 beziehungsweise maximalem metabolischem Steady State; RPE grob 6–8/10.
- Hauptteil: für weniger Erfahrene etwa 10–20 Minuten, für Trainierte oft 20–40 Minuten akkumuliert.
- Talk Test: nur kurze Phrasen.
- Progression: zuerst Zeit im Zielbereich, dann geringfügig schneller oder spezifischer.
- Häufigkeit: häufig höchstens einmal pro Woche im Freizeitbereich.
- Risiko: Schwelle ist kontrolliert hart, nicht Time Trial.

## 6.7 Cruise Intervals

**Zweck:** Schwellenzeit mit kurzen Erholungen akkumulieren, ohne den kontinuierlichen Lauf zu erzwingen.

- Wiederholungen: häufig 3–10 Minuten.
- Erholung: etwa 1–3 Minuten lockeres Traben; kurz genug, um die metabolische Kontinuität zu erhalten.
- Gesamtzeit im Zielbereich: häufig 15–40 Minuten.
- Beispiele: `4 × 6 min`, `3 × 10 min`, `5 × 5 min`.
- Progression: mehr Gesamtzeit, längere Wiederholungen oder kürzere Erholung – nicht alles zugleich.
- Vorteil: leichter zu kontrollieren und abzubrechen als ein langer Tempoblock.

## 6.8 VO2max-nahe Intervalle

**Zweck:** hohe Sauerstoffaufnahme, obere aerobe Leistung und Wettkampfspezifität für kürzere Distanzen.

- Wiederholungen: häufig 2–5 Minuten.
- Erholung: ungefähr gleich lang oder etwas kürzer, locker aktiv.
- Gesamtzeit harter Arbeit: häufig 10–25 Minuten, abhängig von Trainingsstand.
- Ziel: gleichmäßige, kontrollierte Wiederholungen; kein Sprint.
- Intensität: aus aktueller Leistungsfähigkeit ableiten, oft in der Nähe von 3–5-km-Leistung; Herzfrequenz ist wegen Verzögerung kein guter Primärtarget für kurze Wiederholungen.
- Risiko: mechanisch und metabolisch anspruchsvoll; konservativer Einstieg.

## 6.9 Short Repetitions / Speed Economy

**Zweck:** Lauftechnik unter Geschwindigkeit, neuromuskuläre Qualität, Ökonomie.

- Wiederholungen: etwa 10–60 Sekunden.
- Erholung: vollständig oder nahezu vollständig.
- Intensität: schnell und locker, nicht maximal, außer bei spezifischen Sprintprogrammen.
- Gesamtvolumen: gering.
- Einsatz: eher ergänzend als konditionelles Haupttraining.

## 6.10 Strides

**Zweck:** neuromuskuläre Aktivierung ohne große Ermüdung.

- Beispielbereich: 4–8 Wiederholungen à 15–25 Sekunden.
- Erholung: 60–120 Sekunden Gehen oder sehr lockeres Traben.
- Ablauf: kontrolliert beschleunigen, gute Form, nicht all-out.
- Platzierung: nach easy run oder als Teil eines Warm-ups.

## 6.11 Hill Repeats

### Kurze Hügel

- 8–20 Sekunden;
- hohe, aber technisch saubere Leistung;
- vollständige Gehpause zurück;
- Fokus Kraft und Ökonomie.

### Längere Hügel

- etwa 45–180 Sekunden;
- kontrolliert hart;
- lockeres Zurücktraben;
- Fokus aerobe Leistung und Kraftausdauer.

Pace ist am Hügel ungeeignet. RPE, Dauer und Streckenprofil sind die primären Targets.

## 6.12 Progression Run

**Zweck:** Pacing, Ermüdungsresistenz und kontrollierter Übergang zwischen Intensitätsdomänen.

- Start: easy.
- Ende: steady, Race Pace oder bei geeigneter Einheit knapp unter Schwelle.
- Guardrail: Endbeschleunigung nicht automatisch in jeden Long Run integrieren.
- Progression: längerer kontrollierter Schlussabschnitt, nicht aggressiver Start.

## 6.13 Race-Pace Workout

**Zweck:** spezifisches Tempo, Ökonomie, mentale und technische Vertrautheit.

- 5 km: kürzere Wiederholungen oder Blöcke bei 5-km-Pace.
- 10 km: längere Wiederholungen oder zusammenhängende Blöcke.
- Halbmarathon: längere Blöcke, häufig mit lockeren Abschnitten.
- Marathon: längere Marathonpace-Blöcke, häufig im Long Run, aber nicht jede Woche.
- Guardrail: Zielpace ist nur sinnvoll, wenn Ziel und aktuelle Fitness kompatibel sind.

## 6.14 Fartlek

**Zweck:** flexible Intensitätswechsel, geringer psychologischer und logistischer Aufwand.

- Struktur: zeitbasiert, z. B. `10 × 1 min zügig / 1 min locker`.
- Vorteil: gut bei Gelände, Hitze oder fehlender aktueller Pace-Kalibrierung.
- Nachteil: Belastung ist schwerer exakt zu quantifizieren.

## 6.15 Run-Walk

**Zweck:** Einstieg, sichere Volumensteigerung, lange Ziele für weniger erfahrene Läufer.

- Struktur: feste Lauf-/Gehintervalle.
- Progression: längere Laufabschnitte, kürzere Gehabschnitte oder längere Gesamtdauer.
- Wichtig: Run-Walk ist keine „minderwertige“ Alternative, sondern ein valides Belastungsdesign.

## 6.16 Warm-up und Cool-down

Für Qualitätsworkouts sollte der Generator standardmäßig ein Warm-up und Cool-down vorsehen:

- 10–20 Minuten locker, abhängig von Einheit und Athlet;
- optionale Mobilität, Lauf-ABC oder Strides;
- nach der Qualität 5–15 Minuten locker;
- bei kurzen oder sehr lockeren Einheiten kann das gesamte Workout als kontinuierlicher Easy Run modelliert werden.

---

# 7. Individualisierung der Intensität

## 7.1 Internes Drei-Domänen-Modell

Die Engine speichert Intensität unabhängig von Garmin-Zonennummern:

1. `LOW`: unterhalb LT1/VT1 – sprechen in ganzen Sätzen möglich.
2. `MODERATE`: zwischen LT1 und LT2 – kontrolliert, aber deutlich belastend.
3. `HIGH`: oberhalb LT2/critical speed – begrenzte Dauer, Intervallstruktur erforderlich.

Eine 5-Zonen-Darstellung kann daraus abgeleitet werden. Die Nummer „Zone 2“ allein ist kein stabiler fachlicher Wert.

## 7.2 Priorität der Intensitätsquellen

### Priorität 1: aktuelle Wettkampf-, Time-Trial- oder Critical-Speed-Daten

Vorteile:

- direkt leistungsbezogen;
- besonders gut für Pace-basierte Workouts;
- aus mehreren maximalen oder sehr harten Bestleistungen modellierbar.

Nachteile:

- benötigt aktuelle und glaubwürdige Daten;
- Wind, Gelände und Hitze verzerren;
- anaerobe Kapazitätsparameter sind oft weniger stabil als Critical Speed selbst.

### Priorität 2: Laktatschwelle/LTHR und Schwellenpace

Vorteile:

- physiologisch näher an der gewünschten Domäne als pauschale HRmax-Prozente;
- gut für Schwellen- und längere Tempoeinheiten.

Nachteile:

- Garmin-Schätzung ist modellbasiert;
- Geräte-/Sensorvoraussetzungen und Datenqualität beachten;
- veraltete Schwelle darf nicht unbegrenzt weiterverwendet werden.

### Priorität 3: Heart Rate Reserve

`HRR = HRmax - RHR` und `target HR = RHR + fraction × HRR`.

Vorteile:

- personalisierter als reine HRmax-Prozente;
- für lockere und kontinuierliche Läufe brauchbar.

Nachteile:

- benötigt echte, nicht nur altersgeschätzte HRmax;
- RHR variiert;
- HR driftet bei Hitze und langen Läufen;
- optische Handgelenk-HR kann bei schnellen Wechseln ungenau sein.

### Priorität 4: RPE und Talk Test

Vorteile:

- robust gegenüber Gelände, Hitze und Tagesform;
- immer verfügbar;
- wichtiges Sicherheits- und Adaptationssignal.

Nachteile:

- benötigt Lernphase;
- interindividuelle Variation;
- Freitext muss strukturiert erfasst werden.

### Priorität 5: Running Power

Vorteile:

- reagiert schneller als Herzfrequenz;
- kann bei Hügeln und wechselnder Pace helfen.

Nachteile:

- Werte unterschiedlicher Hersteller sind nicht austauschbar;
- Gerät und Algorithmus müssen konstant bleiben;
- ohne individuelle Schwellen-/Historienkalibrierung nur sekundär.

## 7.3 Redundante Targets

Jedes Workout sollte nach Möglichkeit enthalten:

- `primary_target`: z. B. Pace oder RPE;
- `secondary_target`: z. B. HR-Band;
- `environment_fallback`: z. B. RPE/Talk Test;
- `abort_or_downshift_rule`: z. B. ungewöhnlich hohe RPE bei normaler Pace.

Beispiel:

```yaml
primary_target:
  type: pace
  min_sec_per_km: 285
  max_sec_per_km: 295
secondary_target:
  type: heart_rate
  min_bpm: 158
  max_bpm: 168
fallback_target:
  type: rpe
  min: 6
  max: 7
context_rule: "Bei Hitze oder starkem Profil RPE priorisieren und Pace freigeben."
```

## 7.4 Datenqualität und Confidence

Jede abgeleitete Intensität erhält:

- `source_type`;
- `measured_at`;
- `sample_count`;
- `conditions`;
- `confidence`;
- `expires_at` oder maximale Alterung;
- `fallback`.

Ein Coach darf keine sekundengenaue Pace aus einer schwachen, Monate alten VO2max-Schätzung ableiten.

---

# 8. Garmin-Daten: Relevanz, Priorisierung und technische Verfügbarkeit

## 8.1 Grundsatz

Garmin-Daten werden in drei Ebenen getrennt:

1. **Raw:** unveränderte Antworten für Debugging und Reprocessing.
2. **Normalized:** stabile interne Modelle mit Einheiten, Zeitstempeln und Qualitätsflags.
3. **Derived:** Baselines, Trends, Intensitätsmodelle und Entscheidungsfeatures.

Die Fachlogik darf nicht direkt auf wechselnde Garmin-Dict-Strukturen zugreifen.

## 8.2 Priorisierungsmatrix

| Metrik | Priorität | Rolle im Coach | Caveat |
|---|---:|---|---|
| Aktivitätshistorie, Dauer, Distanz, Pace | A | Baseline, Volumen, Konsistenz, Progression | Kernsignal |
| Splits/Laps, geplante versus tatsächliche Ausführung | A | Leistungsfähigkeit, Adhärenz, Workout-Qualität | GPS-/Streckenfehler beachten |
| Herzfrequenz und Zeit in Bereichen | A/B | interne Belastung, Intensitätsvalidierung | Sensorqualität, Hitze, Drift |
| aktuelle Race-/Time-Trial-Leistung | A | Pace-/CS-Modell, Zielplausibilität | Datum und Bedingungen speichern |
| Trainingsfrequenz und Long-Run-Historie | A | Verträglichkeit, Planstruktur | Kernsignal |
| subjektives RPE, Schmerz, Krankheit, Motivation | A | Daily Adaptation und Sicherheit | muss First-Class-Input sein |
| Laktatschwelle/LTHR | A/B | Schwellenbereiche | Aktualität und Erkennungsmethode |
| Ruhepuls-Trend | B | Erholungsindikator | Trend statt Einzelwert |
| HRV/HRV Status | B | Modifikation harter Tage | individuelle Baseline, keine Diagnose |
| Schlafdauer und Schlaftrend | B | Erholungs- und Kontextsignal | Schlafstadien nicht übergewichten |
| Training Readiness | B | Garmin-Kompositsignal | enthält teils dieselben Inputs; Doppelzählung vermeiden |
| Recovery Time | B | Hinweis für Tagesanpassung | proprietär, nicht als Sperre allein |
| Acute Load/Training Load | B | Belastungskontext | nicht als Verletzungsorakel |
| Stress/Body Battery | B/C | unterstützender Kontext | proprietäre Scores |
| VO2max | B/C | Langzeittrend, Plausibilität | Punktwert mit Messfehler; nicht allein für Pace |
| Training Effect/Primary Benefit | C | Beschreibung abgeschlossener Einheit | nachgelagerte Garmin-Klassifikation |
| Running Power | B/C | optionales Intensitätssignal | nur geräteintern vergleichbar |
| Cadence | C | Beschreibung, individuelle Trends | keine universelle Zielkadenz |
| Ground Contact Time | C | explorative Technikdaten | keine automatische Korrekturregel |
| Vertical Oscillation/Ratio | C | explorativ | geringe direkte Entscheidungsrelevanz |
| Performance Condition | C | Live-Kontext/Anomalie | abhängig von HR, Pace und VO2max |
| Race Predictions | C | Plausibilitätscheck | nicht als Zielzeitgarantie |

## 8.3 `python-garminconnect`

Der aktuelle Projektstand dokumentiert mehr als 140 Endpoints in 13 Kategorien. Dazu gehören Gesundheitsdaten, HRV, VO2, Training Readiness, Zonen, Aktivitäten, Workout-Management, typed Workout Uploads, Scheduling, Push-to-device, Update, Delete und Unschedule. Die Bibliothek ist ein **inoffizieller Client für Garmin-Webservices**, kein direkter Watch- oder Mobile-SDK-Zugang.

Relevant sind je nach gepinnter Version unter anderem Methoden in der Art von:

```text
get_stats / get_user_summary
get_heart_rates / get_rhr_day
get_sleep_data
get_hrv_data
get_stress_data
get_body_battery
get_training_readiness / get_morning_training_readiness
get_training_status
get_lactate_threshold
get_activities_by_date / get_activity_details
get_workouts / get_workout_by_id
upload_running_workout
update_workout
delete_workout
schedule_workout / unschedule_workout
push_workout_to_device
```

Wichtig:

- Methodennamen und Response-Shapes müssen gegen die tatsächlich installierte Version geprüft werden.
- Training Readiness kann mehrere Tages-Snapshots liefern, nicht zwingend ein einzelnes Dict.
- Typed Responses sind hilfreich, werden aber als experimentelle Oberfläche beschrieben; Raw Data sollte bei Validierungsfehlern erhalten bleiben.
- Typed Workouts mit Pydantic-Modellen sind verfügbar und passen gut als Ziel des Garmin-Compilers.
- Abhängigkeit und Adaptervertrag müssen gepinnt und mit Contract Tests geschützt werden.

## 8.4 Datenabrufstrategie

- inkrementelle Synchronisation mit Cursor/Datum;
- Rohantwort, Parser-Version und Abrufzeit speichern;
- idempotente Upserts;
- Retry mit Backoff nur für sichere Leseoperationen;
- bei mutierenden Garmin-Calls Idempotency Keys und vorherigen Status prüfen;
- Rate Limits respektieren, obwohl Garmin keine stabile öffentliche Quote garantiert;
- Token wie Passwörter behandeln;
- sensible Gesundheits-, Orts- und Aktivitätsdaten nicht in Logs oder Testfixtures committen.

---

# 9. Athlete Baseline und Mindestdaten

## 9.1 Athlete Profile

Statische oder langsam veränderliche Daten:

- Alter optional, Geschlecht optional nur wo fachlich relevant;
- verfügbare Trainingstage;
- bevorzugte lange Einheit;
- Zeitbudget pro Tag;
- Lauftrainingserfahrung;
- Verletzungs-/Schmerzkontext als selbstberichtete Einschränkung, nicht als Diagnose;
- bevorzugte Intensitätssteuerung;
- Sensor-/Geräteausstattung;
- Zielwettkampf, Datum und Zielart;
- aktuelle versus gewünschte Leistung.

## 9.2 Rolling Baseline

Empfohlene Fenster:

- 7 Tage: akuter Kontext;
- 28 Tage: aktuelle Routine;
- 42–56 Tage: robuste Baseline;
- 90–180 Tage: längerfristige Bestleistungen, Unterbrechungen und Saisonalität.

Zu berechnen:

- Median und robuste Streuung der Wochenzeit/-distanz;
- Anzahl Läufe je Woche;
- längste Laufdauer je Woche;
- Anteil lockerer/moderater/hoher Zeit;
- Zahl und Abstand von Qualitätseinheiten;
- Completion Rate;
- typische RPE bei bekannten Workouts;
- Pace-HR-Beziehung bei vergleichbaren Easy Runs;
- aktuelle Critical-Speed-/Race-Modelle mit Confidence;
- Pausen von mehr als 7–14 Tagen;
- Trend statt nur letzter Wert.

## 9.3 Mindestdaten für einen Einzelvorschlag

Ein einzelner lockerer Vorschlag kann mit wenig Daten erzeugt werden, wenn folgende Angaben vorliegen:

- jüngste Laufhistorie oder ehrliche Selbsteinschätzung;
- verfügbare Zeit;
- aktuelles Befinden;
- keine Warnsignale;
- ein konservativer Intensitätsfallback über RPE/Talk Test.

## 9.4 Mindestdaten für einen Mehrwochenplan

Empfohlen:

- mindestens etwa 4 Wochen Laufhistorie; besser 6–8 Wochen;
- aktuelle Wochenfrequenz und Long-Run-Historie;
- Ziel und Datum;
- verfügbare Tage;
- aktuelle Leistungsreferenz oder geplanter Benchmark;
- subjektive Belastungsverträglichkeit;
- bekannte Unterbrechungen.

HRV, Body Battery oder Training Readiness sind **nicht** zwingend erforderlich.

## 9.5 Wann der Coach nachfragen muss

Der Coach fragt gezielt nach, wenn eine fehlende Information die Sicherheit oder Auswahl wesentlich verändert, zum Beispiel:

- „Ist das ein allgemeiner Motivationsmangel oder hast du Schmerzen?“
- „Wo liegt der Schmerz und verändert er deinen Laufstil?“
- „Hast du Fieber, Brustschmerz, ungewöhnliche Atemnot, Schwindel oder Ohnmachtsgefühl?“
- „Wie viele Tage pro Woche kannst du realistisch laufen?“
- „Ist dein Ziel nur anzukommen oder eine bestimmte Zeit?“
- „Wann war dein letzter harter 5-km-/10-km-Lauf?“

Der Agent darf keine fehlende Race Pace, HRmax oder Schwelle erfinden.

---

# 10. Guardrails und Sicherheitslogik

## 10.1 Hard Safety Stops

Bei folgenden selbstberichteten Signalen darf der Coach kein normales Training freigeben:

- Brustschmerz oder Engegefühl;
- Ohnmacht, Beinahe-Ohnmacht oder deutlicher Schwindel;
- ungewöhnliche Atemnot in Ruhe oder bei sehr leichter Belastung;
- akute neurologische Symptome;
- Fieber oder schwere systemische Krankheitssymptome;
- starker Schmerz, der den Gang oder Laufstil verändert;
- bekannte medizinische Einschränkung, für die Training nicht freigegeben ist.

Das System diagnostiziert nicht. Es stoppt die Trainingsplanung, empfiehlt angemessene professionelle Abklärung und dokumentiert den Grund.

## 10.2 Schmerz versus Muskelkater

Das Feedbackschema trennt:

- allgemeine Müdigkeit;
- symmetrischen Muskelkater;
- lokalisierten Schmerz;
- Schmerzintensität;
- Veränderung von Gang/Laufstil;
- Verschlechterung bei Belastung;
- Dauer und Wiederholung.

Lokalisierter, zunehmender oder bewegungsverändernder Schmerz überstimmt positive Wearable-Scores.

## 10.3 Progressionsregeln

- keine starre 10-%-Regel;
- Baseline-relative Budgets;
- nach Pause oder Krankheit Re-Entry-Modus;
- Long Run nicht sprunghaft über jüngste Historie erhöhen;
- hohe Intensität nicht gleichzeitig mit starkem Umfangsanstieg ausbauen;
- ungewohnte Hügel, Sprints und schnelle Wiederholungen als zusätzliche mechanische Belastung behandeln;
- mehrere ausgelassene Einheiten nicht „nachholen“;
- zusätzlich jede geplante Einzeldistanz gegen den längsten Lauf der letzten 30 Tage prüfen;
- bei einer Einzeldistanz von mehr als 110 % dieses Referenzwertes mindestens Warnung, Review und konservative Alternative erzeugen;
- niemals formulieren, dass bis 10 % Steigerung „sicher“ sei.

### Produktregel für Einzeldistanzspitzen

```yaml
- id: LOAD-SINGLE-SESSION-SPIKE-001
  type: soft_constraint
  when: candidate.distance_meters > 1.10 * baseline.max_run_distance_30d_meters
  action:
    - add_high_visibility_warning
    - offer_reduced_distance_alternative
    - require_explicit_review_before_acceptance
  escalation:
    hard_block_when:
      any:
        - athlete.reentry_mode == true
        - feedback.pain.present == true
        - baseline.data_confidence == low
        - candidate.distance_meters > 2.00 * baseline.max_run_distance_30d_meters
  evidence_refs: [E-INJURY-SESSION-SPIKE-001]
  note: risk_marker_not_safe_boundary
```

Die genaue Eskalation ist eine konservative Produktentscheidung und muss fachlich reviewed werden; die Studie selbst beweist keine individuell sichere oder gefährliche Grenze für jeden Läufer.

## 10.4 Qualitätsdichte

Default für Freizeitläufer:

- keine zwei harten Laufeinheiten an aufeinanderfolgenden Tagen;
- Long Run mit starkem spezifischem Anteil gilt als Qualitätsreiz;
- bei drei Lauftagen oft nur eine Qualitätseinheit plus Long Run;
- nach sehr harter oder abgebrochener Einheit nächste Qualität nicht automatisch beibehalten.

Diese Regeln sind konfigurierbare konservative Defaults.

## 10.5 Wearable Guardrail

- Kein Workout wird allein wegen Training Readiness > X intensiver.
- Ein niedriger Readiness-Score kann eine Nachfrage oder Alternativen auslösen, aber nicht ohne Kontext automatisch einen Ruhetag erzwingen.
- HRV wird relativ zur individuellen Baseline und als Trend interpretiert.
- Schlafstadien werden nicht als exakte physiologische Wahrheit verwendet.
- VO2max wird als Trend und Plausibilitätsindikator verwendet.
- Garmin Load Ratio/ACWR wird nicht als kausale Verletzungsprognose genutzt.

## 10.6 Umwelt und Hydration

- Hitze: RPE/HR priorisieren, Pace lockern, Dauer reduzieren, Schatten/Indoor-Option.
- Luftqualität: zukünftiger externer Service; bei hoher Belastung keine intensive Einheit vorschlagen.
- Eis/Unwetter: sichere Alternative statt Pace-Ziel.
- Lange Läufe: Verpflegungsstrategie testen, ohne medizinische oder ernährungsmedizinische Individualtherapie zu simulieren.

## 10.7 Planabweichungen

Nicht nachholen:

- verpasste Intervalle;
- ausgelassene lange Läufe;
- mehrere Workouts nach Krankheit.

Stattdessen neu priorisieren:

1. Sicherheit;
2. Kontinuität;
3. nächster wichtiger Reiz;
4. Wochenvolumen;
5. perfekte Planerfüllung.

---
# 11. Produkt- und UX-Konzept

## 11.1 Kernprinzip: Chat plus strukturierte UI

Der Chat ist nicht der Speicherort des Trainings. Er ist die Interaktionsschicht. Ein konkreter Vorschlag erscheint als strukturiertes UI-Objekt mit:

- Titel und Trainingszweck;
- Schrittübersicht;
- erwarteter Dauer/Distanz;
- Intensitätszielen und Fallbacks;
- Begründung;
- Risiken/Warnungen;
- Vergleich mit dem ursprünglich geplanten Workout;
- Aktionen `Bearbeiten`, `Annehmen`, `Ablehnen`, `Alternative anzeigen`.

## 11.2 Wiederverwendung des manuellen Workout-Builders

Die bestehende Komponente wird in drei Schichten zerlegt oder entsprechend adaptiert:

1. `WorkoutEditor`: liest und schreibt `WorkoutPrescription`.
2. `WorkoutPreview`: rendert dasselbe Modell read-only.
3. `WorkoutPersistenceAndSync`: validiert, speichert und kompiliert für Garmin.

Die Quelle ist Metadatum, keine separate Komponentenfamilie:

```text
source = manual | coach_single | coach_daily_adaptation | plan_generator
```

## 11.3 UI-Flows

### Einzelvorschlag

1. Nutzer fragt nach einem Training.
2. Coach sammelt fehlende Inputs.
3. Generator erzeugt Kandidat.
4. Validator bestätigt oder korrigiert.
5. Proposal Card erscheint.
6. Nutzer öffnet Preview/Editor.
7. Nutzer übernimmt, verändert oder lehnt ab.
8. Nach Annahme kann geplant und synchronisiert werden.

### Manuelles Workout

1. Nutzer startet Builder.
2. Quelle ist `manual`.
3. Derselbe Validator läuft.
4. Derselbe Garmin-Compiler und Sync-Service werden verwendet.

### Tagesanpassung

1. Chat zeigt heutiges Workout.
2. Nutzer beschreibt Befinden.
3. Feedback wird strukturiert.
4. Adaptation Engine erzeugt zulässige Optionen.
5. UI zeigt Vergleich `vorher → nachher`.
6. Nutzer akzeptiert eine Revision.
7. Original wird `superseded`; neue Version wird geplant/synchronisiert.

## 11.4 UX-Regeln

- Keine versteckten Änderungen am Kalender.
- Immer zeigen, welches Workout ersetzt oder verschoben wird.
- Immer zeigen, warum eine Änderung empfohlen wird.
- Nutzeränderungen lösen erneute Validierung aus.
- Bei ungültiger Änderung konkrete, verständliche Meldung und sichere Alternative.
- Garmin-Sync zeigt Zielgerät, Datum und konkrete Workout-Version.
- Wiederholter Klick darf keine Duplikate erzeugen.
- Sync-Fehler verändern nicht rückwirkend die Annahme; Status wird separat angezeigt.

---

# 12. Gemeinsames Workout-Domainmodell

## 12.1 Aggregate

`WorkoutPrescription` ist die fachliche Beschreibung einer Einheit. Es enthält keine Garmin-spezifischen IDs als Kernlogik. Garmin-IDs leben in Integrationsrecords.

```text
WorkoutPrescription
├── identity/version
├── source/provenance
├── sport/purpose
├── estimated load
├── warm-up / steps / cool-down
├── targets and fallbacks
├── eligibility and safety notes
└── evidence references
```

## 12.2 Schritte

Unterstützte kanonische Schrittarten:

- `WarmupStep`
- `WorkStep`
- `RecoveryStep`
- `CooldownStep`
- `RepeatBlock`
- `OpenStep`

Unterstützte Endbedingungen:

- Zeit;
- Distanz;
- Lap Button;
- offen/manuell.

Unterstützte Targets:

- kein Target;
- Pace-Band;
- Herzfrequenz-Band;
- Power-Band;
- RPE-Band;
- physiologische Domäne;
- kombinierte primäre/sekundäre Targets.

## 12.3 Warum nicht das Garmin-Modell direkt verwenden?

Das Garmin-Modell ist ein Exportformat. Ein eigenes kanonisches Modell ermöglicht:

- mehrere Zielplattformen;
- UI-Editing ohne Garmin-Sonderfälle;
- stabile Tests trotz API-Schemaänderung;
- fachliche Targets, die Garmin nicht vollständig abbildet, etwa RPE-Fallback;
- Versionierung und Provenance;
- verständliche Validierungsfehler.

Der `GarminWorkoutCompiler` übersetzt eine akzeptierte Prescription in die aktuell gepinnte typed Workout-Struktur. Nicht unterstützte Konstrukte werden explizit abgelehnt oder mit dokumentierter Degradation übersetzt.

## 12.4 Load Estimate

Jedes Workout erhält eine Schätzung mit Unsicherheit:

```text
estimated_duration_seconds
estimated_distance_meters
low_intensity_seconds
moderate_intensity_seconds
high_intensity_seconds
mechanical_load_class
estimated_session_rpe_range
confidence
```

Diese Schätzung dient der Validierung und dem Wochenplan, ist aber keine behauptete exakte physiologische Last.

---

# 13. Proposal-, Revision-, Acceptance- und Sync-State-Machine

## 13.1 Trennung der Zustände

Proposal-Status und Sync-Status dürfen nicht in ein einziges Enum gepresst werden.

### Proposal Status

```text
DRAFT -> PROPOSED -> ACCEPTED
                  -> REJECTED
                  -> EXPIRED
                  -> SUPERSEDED
```

Eine Bearbeitung erzeugt eine neue immutable `WorkoutRevision`; das Proposal bleibt dasselbe oder wird über einen Parent-Link fortgeführt.

### Scheduling Status

```text
UNSCHEDULED -> SCHEDULED -> RESCHEDULED
                           -> CANCELLED
```

### Sync Status

```text
NOT_REQUESTED -> PENDING -> SYNCED
                        -> FAILED_RETRYABLE
                        -> FAILED_FINAL
                        -> REMOVED
```

## 13.2 Invarianten

1. Nur eine konkrete Revision kann akzeptiert werden.
2. Änderungen nach Annahme erzeugen eine neue Revision und benötigen erneute Annahme.
3. Nur `ACCEPTED` plus gültige Revision darf geplant oder synchronisiert werden.
4. `sync_workout_to_garmin` benötigt Proposal-ID, Revision-ID und Bestätigungsnachweis.
5. Ein Proposal kann nur ein aktives angenommenes Kind besitzen.
6. Ersetzen eines heutigen Workouts erhält einen Link `replaces_workout_id`.
7. Idempotency Key verhindert Doppel-Upload.
8. Jeder Übergang erzeugt Audit Event mit Akteur, Zeitpunkt, Request-ID und Diff.

## 13.3 Provenance

Zu speichern:

- `source_type`;
- `generator_version`;
- `rule_set_version`;
- `knowledge_base_version`;
- `model_provider/model_id` optional;
- `prompt_template_version`;
- `parent_proposal_id`;
- `replaces_workout_id`;
- `evidence_refs`;
- `user_edit_diff`;
- `validation_report_id`;
- `accepted_by` und `accepted_at`.

## 13.4 Optimistic Concurrency

Der Accept-Call enthält `expected_revision`. Wurde das Proposal zwischen Preview und Accept geändert, antwortet das Backend mit `409 Conflict` und zeigt die neue Revision. So wird nie versehentlich eine veraltete Variante synchronisiert.

---

# 14. Daily Adaptation im Chat

## 14.1 Ziel

Der Nutzer kann morgens sagen:

> „Heute stehen 10 km an, aber ich fühle mich nicht danach.“

Der Coach antwortet nicht sofort mit einem improvisierten Ersatz. Er führt einen kontrollierten Workflow aus.

## 14.2 Workflow

### Schritt 1: Kontext laden

- heutiges Workout und Zweck;
- Rest der Woche;
- letzte 7/28/42 Tage;
- letzter Long Run und letzte Qualität;
- aktuelle Garmin-Signale;
- bekannte Einschränkungen;
- Zielwettkampf und Phase;
- bereits synchronisierte Garmin-Version.

### Schritt 2: subjektives Feedback erfassen

Mindestens:

- Motivation;
- allgemeine Müdigkeit;
- Beinfrische;
- Muskelkater;
- lokalisierter Schmerz;
- Krankheitssymptome;
- wahrgenommene Schlafqualität;
- verfügbarer Zeitrahmen;
- Freitext.

### Schritt 3: Safety Triage

- Red Flags → kein Trainingsvorschlag, sichere Weiterleitung.
- Schmerz mit Laufstiländerung → Ruhetag/kein Lauf und Abklärungshinweis.
- unklare Angaben → gezielte Rückfrage.

### Schritt 4: Adaptationsklasse bestimmen

- `KEEP`
- `REDUCE_VOLUME`
- `REDUCE_INTENSITY`
- `SIMPLIFY_QUALITY`
- `REPLACE_WITH_EASY`
- `DEFER_KEY_SESSION`
- `REST`
- `REPLAN_WEEK`

### Schritt 5: Kandidaten deterministisch erzeugen

Beispiel für geplante 10 km easy:

- 10 km easy unverändert;
- 6–8 km easy;
- 30–40 min easy ohne Pace-Ziel;
- 20–30 min Recovery Run;
- Ruhetag.

Beispiel für geplante Intervalle:

- unverändert;
- weniger Wiederholungen;
- gleiche Wiederholungen langsamer/mit längerer Pause;
- auf später verschieben, heute easy;
- durch easy ersetzen;
- Ruhetag.

### Schritt 6: Wochenkontext validieren

Beim Verschieben prüfen:

- entstehen zwei harte Tage nacheinander?
- kollidiert es mit Long Run?
- wird eine andere Key Session verdrängt?
- steigt die Wochenlast trotz angeblicher Entlastung?
- ist ein vollständiger Replan erforderlich?

### Schritt 7: LLM erklärt Optionen

Das LLM erhält ausschließlich validierte Optionen und erklärt zum Beispiel:

- welche Trainingswirkung erhalten bleibt;
- welche Belastung reduziert wird;
- wie sich die Woche verändert;
- welche Information noch fehlt.

### Schritt 8: Nutzer wählt oder editiert

Die Auswahl öffnet dieselbe Preview-/Editor-Komponente.

### Schritt 9: explizite Annahme

Erst `accept_workout_proposal` bestätigt die konkrete Revision.

### Schritt 10: Schedule/Sync

Nur über Application Service mit Idempotency, Audit und Fehlerstatus.

## 14.3 Kleine Tagesänderung versus Wochen-Neuberechnung

### Kleine Änderung

- gleiche oder geringere Belastung;
- keine neue hohe Intensität;
- Reduktion der Dauer im konfigurierten Bereich;
- weniger Wiederholungen;
- längere Pausen;
- Qualität durch easy/rest ersetzen;
- Verschiebung, wenn alle Abstände und Wochenziele erhalten bleiben.

### Replan Week

- Long Run oder Qualität wird so verschoben, dass mehrere Schlüsseltage betroffen sind;
- mehr als eine Key Session fällt aus;
- Krankheit/Schmerz hält an;
- zwei oder mehr Einheiten wurden nicht absolviert;
- Wochenziel ändert sich materiell;
- Ziel oder Zieltermin ändert sich;
- Taper-Woche ist betroffen;
- vorgeschlagene Änderung würde Belastung erhöhen.

## 14.4 Daily Adaptation darf standardmäßig nicht eskalieren

Der Modus „Ich fühle mich heute schlecht“ darf nicht zufällig eine härtere Alternative generieren. Zulässige Kandidaten müssen `estimated_load_delta <= 0` haben, außer der Nutzer fordert ausdrücklich eine andere Planung an und durchläuft den normalen Planungsworkflow.

---

# 15. Deterministische Engine versus LLM

## 15.1 Deterministisch in Code

- Einheiten- und Pace-Konvertierung;
- Zonenberechnung;
- Critical-Speed-Fit und Confidence;
- Baseline-Aggregation;
- Template-Eligibility;
- Wochenvolumen- und Intensitätsbudgets;
- Abstand von Qualitätsreizen;
- Long-Run-Verträglichkeit;
- Progressions- und Re-Entry-Regeln;
- Red-Flag-Handling;
- Proposal-State-Machine;
- Versionierung und Audit;
- Idempotency;
- Garmin-Compilation und Sync;
- Datenvalidierung;
- Auswahl der zulässigen Adaptationsklassen;
- finaler Constraint Check.

## 15.2 Aufgabe des LLM

- Ziele und Einschränkungen aus Sprache extrahieren;
- subjektives Feedback strukturiert erfassen;
- bei entscheidenden Lücken gezielt nachfragen;
- aus bereits validierten Optionen eine verständliche Empfehlung formulieren;
- Unterschiede erklären;
- Workout-Zweck und Durchführung erklären;
- eine strukturierte Tool-Anfrage erzeugen;
- Nutzeränderungswünsche in einen Proposal-Update-Call übersetzen.

## 15.3 Verbotene LLM-Aktionen

- direkt in Datenbank, Kalender oder Garmin schreiben;
- Workout als akzeptiert darstellen, bevor das Tool dies bestätigt;
- medizinische Diagnosen stellen;
- unbekannte Schwelle, HRmax oder Pace erfinden;
- harte Constraints umgehen;
- ein nicht validiertes Workout als sicher bezeichnen;
- aus einem einzelnen Garmin-Score eine definitive Tagesentscheidung ableiten;
- versteckte Planänderungen durchführen.

## 15.4 RAG-Rolle

RAG eignet sich für:

- Trainingsprinzipien;
- Workout-Erklärungen;
- Quellenbegründungen;
- Produkt-/Supporttexte;
- evidenzbasierte Hinweise.

RAG darf nicht die einzige Durchsetzungsschicht für Guardrails sein. Die Constraint Engine lädt versionierte strukturierte Regeln direkt, nicht als semantisch ähnliche Textpassagen.

---

# 16. Zielarchitektur und Datenfluss

## 16.1 Komponenten

```text
Garmin Connect
    |
    v
GarminClientAdapter  ----> RawPayloadStore
    |
    v
Normalization Pipeline
    |
    +--> ActivitySnapshotRepository
    +--> WellnessSnapshotRepository
    +--> GarminMetricQuality
    |
    v
Athlete Baseline Service
    |
    +--> Performance Model (race/TT/CS/LTHR)
    +--> Load & Consistency Model
    +--> Recovery Context
    |
    v
Workout / Plan Generator <---- Template Registry + Constraint Registry
    |
    v
Deterministic Validator
    |
    v
WorkoutProposal Service
    |
    +--> LangChain Tool Layer <--> Chat LLM
    +--> Shared WorkoutPreview / WorkoutEditor
    |
    v
Acceptance Service
    |
    v
Schedule Service
    |
    v
GarminWorkoutCompiler
    |
    v
GarminSyncService
    |
    v
Execution Reconciliation + Post-session Feedback
```

## 16.2 Ports und Adapter

Empfohlene Ports:

```python
class GarminReadPort(Protocol): ...
class GarminWorkoutWritePort(Protocol): ...
class ActivityRepository(Protocol): ...
class WorkoutProposalRepository(Protocol): ...
class WorkoutValidator(Protocol): ...
class WorkoutGenerator(Protocol): ...
class AdaptationEngine(Protocol): ...
class Clock(Protocol): ...
class WeatherContextPort(Protocol): ...
```

Der Domain-Layer importiert niemals `garminconnect` direkt.

## 16.3 Speicherung

Mindestens folgende Tabellen/Aggregate:

- `garmin_accounts`
- `garmin_sync_cursors`
- `garmin_raw_payloads`
- `activities`
- `activity_laps_or_splits`
- `daily_wellness_snapshots`
- `athlete_profiles`
- `athlete_baseline_snapshots`
- `performance_models`
- `training_plans`
- `planned_workouts`
- `workout_prescriptions`
- `workout_proposals`
- `workout_revisions`
- `proposal_events`
- `schedule_records`
- `garmin_sync_records`
- `pre_session_feedback`
- `post_session_feedback`
- `validation_reports`
- `decision_traces`

## 16.4 Datenschutz und Sicherheit

- Tokens verschlüsselt speichern und strikt vom App-Log trennen;
- minimal notwendige Gesundheitsdaten an das LLM geben;
- Rohpayloads nicht ungefiltert in Prompts;
- Standortdaten minimieren oder separat schützen;
- Audit-Logs ohne sensible Inhalte;
- Lösch- und Exportfunktion für Nutzer;
- Testfixtures anonymisieren;
- keine VCR-Aufzeichnungen mit echten Garmin-Daten committen;
- Mutationen und Sync mit Berechtigungsprüfung und CSRF-/Session-Schutz.

---

# 17. Pydantic-/JSON-/YAML-Beispiele

Die Beispiele sind Ausgangspunkte. OpenCode soll sie an bestehende Namenskonventionen, ORM und API-Framework anpassen.

## 17.1 Kanonische Pydantic-Modelle

```python
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class WorkoutSource(StrEnum):
    MANUAL = "manual"
    COACH_SINGLE = "coach_single"
    COACH_DAILY_ADAPTATION = "coach_daily_adaptation"
    PLAN_GENERATOR = "plan_generator"


class Sport(StrEnum):
    RUNNING = "running"


class IntensityDomain(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class DurationKind(StrEnum):
    TIME = "time"
    DISTANCE = "distance"
    LAP_BUTTON = "lap_button"
    OPEN = "open"


class TargetKind(StrEnum):
    NONE = "none"
    PACE = "pace"
    HEART_RATE = "heart_rate"
    POWER = "power"
    RPE = "rpe"
    DOMAIN = "domain"


class StepRole(StrEnum):
    WARMUP = "warmup"
    WORK = "work"
    RECOVERY = "recovery"
    COOLDOWN = "cooldown"
    OPEN = "open"


class StepDuration(BaseModel):
    kind: DurationKind
    seconds: int | None = Field(default=None, ge=1)
    meters: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_value(self) -> "StepDuration":
        if self.kind == DurationKind.TIME and self.seconds is None:
            raise ValueError("time duration requires seconds")
        if self.kind == DurationKind.DISTANCE and self.meters is None:
            raise ValueError("distance duration requires meters")
        return self


class WorkoutTarget(BaseModel):
    kind: TargetKind
    low: float | None = None
    high: float | None = None
    unit: str | None = None
    domain: IntensityDomain | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "WorkoutTarget":
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("target low must not exceed target high")
        return self


class AtomicStep(BaseModel):
    type: Literal["step"] = "step"
    role: StepRole
    name: str
    duration: StepDuration
    primary_target: WorkoutTarget
    secondary_target: WorkoutTarget | None = None
    fallback_target: WorkoutTarget | None = None
    instructions: list[str] = Field(default_factory=list)


class RepeatBlock(BaseModel):
    type: Literal["repeat"] = "repeat"
    repetitions: int = Field(ge=2, le=50)
    steps: list[AtomicStep]


WorkoutElement = Annotated[AtomicStep | RepeatBlock, Field(discriminator="type")]


class LoadEstimate(BaseModel):
    duration_seconds: int = Field(ge=0)
    distance_meters: int | None = Field(default=None, ge=0)
    low_seconds: int = Field(default=0, ge=0)
    moderate_seconds: int = Field(default=0, ge=0)
    high_seconds: int = Field(default=0, ge=0)
    session_rpe_low: float | None = Field(default=None, ge=0, le=10)
    session_rpe_high: float | None = Field(default=None, ge=0, le=10)
    mechanical_load_class: Literal["low", "moderate", "high"]
    confidence: float = Field(ge=0, le=1)


class Provenance(BaseModel):
    source: WorkoutSource
    generator_version: str | None = None
    rule_set_version: str
    knowledge_base_version: str
    evidence_refs: list[str] = Field(default_factory=list)
    parent_proposal_id: UUID | None = None
    replaces_workout_id: UUID | None = None


class WorkoutPrescription(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=120)
    sport: Sport = Sport.RUNNING
    purpose: str
    scheduled_date: date | None = None
    elements: list[WorkoutElement] = Field(min_length=1)
    load_estimate: LoadEstimate
    safety_notes: list[str] = Field(default_factory=list)
    coach_explanation: str | None = None
    provenance: Provenance
    created_at: datetime
```

## 17.2 Proposal-Modelle

```python
class ProposalStatus(StrEnum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ValidationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SAFETY_STOP = "safety_stop"


class ValidationIssue(BaseModel):
    code: str
    severity: ValidationSeverity
    message: str
    field_path: str | None = None
    rule_id: str
    evidence_refs: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    valid: bool
    issues: list[ValidationIssue]
    rule_set_version: str
    evaluated_at: datetime


class WorkoutRevision(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    proposal_id: UUID
    revision_number: int = Field(ge=1)
    prescription: WorkoutPrescription
    validation_report: ValidationReport
    edit_source: Literal["generator", "user", "coach_tool", "system"]
    created_at: datetime


class WorkoutProposal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    athlete_id: UUID
    status: ProposalStatus
    current_revision_id: UUID
    accepted_revision_id: UUID | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
```

## 17.3 Feedbackmodelle

```python
class IllnessSignal(StrEnum):
    NONE = "none"
    MILD_UPPER_RESPIRATORY = "mild_upper_respiratory"
    FEVER = "fever"
    SYSTEMIC = "systemic"
    CARDIOPULMONARY_WARNING = "cardiopulmonary_warning"
    UNKNOWN = "unknown"


class PainReport(BaseModel):
    present: bool = False
    location: str | None = None
    severity_0_10: int | None = Field(default=None, ge=0, le=10)
    alters_gait: bool | None = None
    worsens_with_activity: bool | None = None
    notes: str | None = None


class PreSessionFeedback(BaseModel):
    athlete_id: UUID
    workout_id: UUID | None = None
    motivation_1_5: int = Field(ge=1, le=5)
    perceived_fatigue_1_5: int = Field(ge=1, le=5)
    leg_freshness_1_5: int = Field(ge=1, le=5)
    soreness_0_10: int = Field(ge=0, le=10)
    sleep_quality_1_5: int | None = Field(default=None, ge=1, le=5)
    pain: PainReport
    illness_signal: IllnessSignal
    available_minutes: int | None = Field(default=None, ge=0)
    free_text: str | None = None
    extraction_confidence: float = Field(ge=0, le=1)
    needs_clarification: bool = False
    recorded_at: datetime


class PostSessionFeedback(BaseModel):
    workout_id: UUID
    completion_percent: int = Field(ge=0, le=100)
    session_rpe_0_10: float = Field(ge=0, le=10)
    overall_feel_1_5: int = Field(ge=1, le=5)
    pain: PainReport
    stopped_reason: str | None = None
    free_text: str | None = None
    recorded_at: datetime
```

## 17.4 Daily-Adaptation-Modelle

```python
class AdaptationKind(StrEnum):
    KEEP = "keep"
    REDUCE_VOLUME = "reduce_volume"
    REDUCE_INTENSITY = "reduce_intensity"
    SIMPLIFY_QUALITY = "simplify_quality"
    REPLACE_WITH_EASY = "replace_with_easy"
    DEFER_KEY_SESSION = "defer_key_session"
    REST = "rest"
    REPLAN_WEEK = "replan_week"


class AdaptationOption(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kind: AdaptationKind
    original_workout_id: UUID
    proposed_prescription: WorkoutPrescription | None
    expected_load_delta_percent: float
    preserves_primary_purpose: bool
    week_replan_required: bool
    rationale_codes: list[str]
    validation_report: ValidationReport
```

## 17.5 Workout-Template YAML

```yaml
id: threshold_cruise_v1
name: Cruise Intervals
sport: running
purpose: threshold_endurance
rule_strength: heuristic
eligibility:
  min_consistent_running_weeks: 6
  min_runs_per_week: 3
  contraindications:
    - active_pain_affecting_gait
    - fever_or_systemic_illness
    - insufficient_baseline
structure:
  warmup:
    duration_minutes: {min: 12, default: 15, max: 20}
    domain: low
    optional_strides: {min: 0, default: 4, max: 6}
  repeat:
    repetitions: {min: 3, default: 4, max: 6}
    work:
      duration_minutes: {min: 4, default: 6, max: 10}
      domain: moderate
      target_reference: threshold
      rpe: {min: 6, max: 8}
    recovery:
      duration_minutes: {min: 1, default: 2, max: 3}
      domain: low
  cooldown:
    duration_minutes: {min: 8, default: 10, max: 15}
limits:
  max_total_threshold_minutes_by_level:
    novice: 15
    intermediate: 30
    advanced: 40
  max_frequency_per_7_days: 1
progression_axes:
  - total_work_duration
  - repetition_duration
  - recovery_duration
progression_rule: change_one_axis_at_a_time
fallbacks:
  no_reliable_pace: use_rpe_and_talk_test
  heat_or_hills: disable_pace_target
references:
  - E-TID-001
  - E-HIIT-001
```

## 17.6 Constraint YAML

```yaml
version: 1.0.0
rules:
  - id: SAFE-RED-FLAG-001
    type: hard_constraint
    when:
      any:
        - feedback.illness_signal == cardiopulmonary_warning
        - feedback.illness_signal == fever
        - feedback.pain.alters_gait == true
    action: block_running_workout
    severity: safety_stop

  - id: LOAD-NO-CATCHUP-001
    type: hard_constraint
    when: request.intent == catch_up_missed_sessions
    action: reject_and_replan

  - id: DENSITY-HARD-DAYS-001
    type: soft_constraint
    when:
      athlete.level in [novice, intermediate]
      hours_since_last_quality < 48
    action: require_easy_or_rest_unless_reviewed
    note: conservative_product_default

  - id: ADAPT-NO-ESCALATION-001
    type: hard_constraint
    when: mode == daily_adaptation
    assert: candidate.estimated_load <= original.estimated_load

  - id: LOAD-CHANGE-BUDGET-001
    type: soft_constraint
    calculation: baseline_relative_change_budget
    parameters:
      default_min_percent: 5
      default_max_percent: 15
      reduce_after_break: true
      disallow_simultaneous_major_volume_and_intensity_increase: true
    note: heuristic_not_ten_percent_law
```

## 17.7 Evidence Index

```yaml
- id: E-TID-001
  claim: Distance-running programs generally allocate most training below the first threshold.
  evidence_level: A
  source_type: systematic_review
  citation: Casado et al. 2022
  doi: 10.1123/ijspp.2021-0435
  applies_to: highly_trained_and_elite
  transfer_note: Do not copy elite absolute volume to recreational runners.

- id: E-PROGRESSION-001
  claim: A universal 10-percent weekly progression rule is not supported as an injury-prevention law.
  evidence_level: B
  source_type: randomized_trial_and_systematic_reviews
  applies_to: recreational_runners

- id: E-INJURY-SESSION-SPIKE-001
  claim: Single-session running distance above 110 percent of the longest run in the prior 30 days was associated with a higher rate of self-reported overuse injury in a large prospective cohort.
  evidence_level: B
  source_type: prospective_observational_cohort
  citation: Frandsen et al. 2025
  doi: 10.1136/bjsports-2024-109380
  limitations:
    - observational_not_causal
    - self_reported_injury
    - predominantly_male_experienced_middle_aged_sample
  implementation_note: Use as a visible baseline-relative warning, not a claim that 10 percent is safe.

- id: E-WEARABLE-001
  claim: Consumer wearable sleep stages and composite readiness metrics should be secondary signals.
  evidence_level: B
  source_type: validation_reviews
```

---

# 18. API- und Tool-Calling-Design

## 18.1 REST- oder RPC-Endpunkte

```text
GET    /api/coach/context/today
POST   /api/feedback/pre-session
POST   /api/feedback/post-session
POST   /api/workout-proposals
GET    /api/workout-proposals/{proposal_id}
POST   /api/workout-proposals/{proposal_id}/revisions
POST   /api/workout-proposals/{proposal_id}/validate
POST   /api/workout-proposals/{proposal_id}/accept
POST   /api/workout-proposals/{proposal_id}/reject
POST   /api/workout-proposals/{proposal_id}/schedule
POST   /api/workout-proposals/{proposal_id}/sync/garmin
GET    /api/workout-proposals/{proposal_id}/events
POST   /api/daily-adaptation/options
POST   /api/training-plans/replan-week
```

## 18.2 Accept Request

```json
{
  "proposal_id": "5d29...",
  "revision_id": "2ed8...",
  "expected_revision_number": 3,
  "confirmation": {
    "action": "accept_workout",
    "displayed_name": "40 min Easy Run",
    "displayed_date": "2026-08-21"
  }
}
```

## 18.3 Sync Request

```json
{
  "proposal_id": "5d29...",
  "accepted_revision_id": "2ed8...",
  "target_device_id": "garmin-device-id",
  "scheduled_date": "2026-08-21",
  "idempotency_key": "athlete-proposal-revision-device-date"
}
```

Backend prüft:

- Proposal akzeptiert;
- Revision exakt identisch;
- Nutzer berechtigt;
- kein Safety Stop;
- nicht bereits erfolgreich synchronisiert;
- Garmin-Compilation erfolgreich;
- Datum und Gerät gültig.

## 18.4 LLM Tools

### Read-only

```text
get_today_training_context
get_athlete_baseline_summary
get_current_week_plan
get_workout_proposal
get_allowed_adaptations
get_validation_explanation
```

### Mutating, kontrolliert

```text
record_pre_session_feedback
record_post_session_feedback
create_workout_proposal
update_workout_proposal
accept_workout_proposal
reject_workout_proposal
schedule_workout
sync_workout_to_garmin
```

## 18.5 Tool-Sicherheitsregeln

- `get_allowed_adaptations` liefert bereits validierte Kandidaten.
- `update_workout_proposal` validiert jede Änderung erneut.
- `accept_workout_proposal` benötigt explizite Nutzeräußerung im aktuellen Turn.
- `schedule_workout` und `sync_workout_to_garmin` dürfen nicht aus impliziter Zustimmung abgeleitet werden.
- Sync-Tool prüft serverseitig Akzeptanz; das LLM kann die Prüfung nicht umgehen.
- Tool-Response ist die Wahrheit. Das LLM darf einen fehlgeschlagenen Sync nicht als erfolgreich formulieren.

## 18.6 Freitext-Extraktion

Das LLM extrahiert subjektives Feedback mit einem strikten Schema:

```json
{
  "motivation_1_5": 2,
  "perceived_fatigue_1_5": 4,
  "leg_freshness_1_5": 2,
  "soreness_0_10": 3,
  "pain": {
    "present": false,
    "location": null,
    "severity_0_10": null,
    "alters_gait": null,
    "worsens_with_activity": null
  },
  "illness_signal": "none",
  "needs_clarification": false,
  "extraction_confidence": 0.84
}
```

Bei „mein Knie fühlt sich komisch an“ muss `needs_clarification=true` gesetzt werden. Das Modell darf daraus weder „harmlos“ noch eine Diagnose ableiten.

---
# 19. Knowledge-Base- und Repository-Struktur

## 19.1 Zielstruktur

```text
AGENTS.md
opencode.jsonc

/docs
  /architecture
    coach-system-overview.md
    workout-domain-model.md
    proposal-and-sync-state-machine.md
    garmin-integration-boundary.md
  /domain
    training-principles.md
    intensity-models.md
    race-goal-strategies.md
    workout-taxonomy.md
    athlete-baseline.md
    daily-adaptation.md
    garmin-metric-policy.md
    safety-policy.md
  /decisions
    ADR-001-canonical-workout-model.md
    ADR-002-deterministic-constraints.md
    ADR-003-human-approval-before-sync.md

/knowledge
  /evidence
    index.yaml
  /workouts
    easy_run.yaml
    recovery_run.yaml
    long_run.yaml
    steady_run.yaml
    threshold_continuous.yaml
    threshold_cruise.yaml
    vo2_intervals.yaml
    short_repetitions.yaml
    strides.yaml
    hill_repeats.yaml
    progression_run.yaml
    race_pace.yaml
    fartlek.yaml
    run_walk.yaml
  /constraints
    safety.yaml
    progression.yaml
    quality_density.yaml
    daily_adaptation.yaml
    race_specificity.yaml
  /plans
    five_k.yaml
    ten_k.yaml
    half_marathon.yaml
    marathon.yaml

/schemas
  workout_prescription.schema.json
  workout_template.schema.json
  constraint_rule.schema.json
  evidence_entry.schema.json
  coach_tool_contracts.schema.json

/src
  /domain
    /athlete
    /training
    /workouts
    /planning
    /feedback
  /application
    /baseline
    /generation
    /validation
    /proposals
    /adaptation
    /scheduling
    /sync
  /integrations
    /garmin
      client_adapter.py
      normalizers.py
      workout_compiler.py
      sync_service.py
      contracts.py
  /coach
    /tools
    /prompts
    /policies
  /api
  /ui

/tests
  /unit
  /property
  /contract
  /integration
  /scenario
  /llm_evals
  /e2e
```

## 19.2 Markdown versus YAML/JSON

### Markdown

Für:

- Begründungen;
- Konzepte;
- Grenzen der Evidenz;
- Erklärtexte;
- Architekturentscheidungen;
- Onboarding für Entwickler.

### YAML/JSON

Für:

- Workout-Templates;
- Constraint-Parameter;
- Evidenz-Metadaten;
- Tool-Schemas;
- maschinenlesbare Zielstrategien;
- Versionierung und Tests.

### Python/Code

Für:

- Berechnung;
- Invarianten;
- State Machines;
- Zugriffsrechte;
- Validierung;
- Garmin-Compilation;
- Persistence und Idempotency.

## 19.3 Versionierung

Jedes generierte Workout speichert:

```text
knowledge_base_version
rule_set_version
template_id + template_version
generator_version
performance_model_version
```

Dadurch kann später nachvollzogen werden, warum eine Einheit entstanden ist, und ein alter Plan kann reproduziert oder migriert werden.

---

# 20. Direkt nutzbare OpenCode-Instructions

OpenCode unterstützt projektspezifische Regeln über `AGENTS.md`; zusätzliche Instruktionsdateien können über das `instructions`-Feld in `opencode.json` eingebunden werden. Die folgende Fassung ist als Startpunkt gedacht und muss nach dem initialen Repo-Audit um konkrete Build-, Test- und Architekturdetails ergänzt werden.

## 20.1 Vorschlag `AGENTS.md`

```markdown
# Garmin AI Running Coach – Project Instructions

## Mission

Extend the existing server-side Python web application with a safe, evidence-aware AI running coach. The application already retrieves Garmin health/activity data through `cyberjunky/python-garminconnect`, has a manual workout builder, can create/schedule/sync workouts to Garmin, and has a LangChain-based conversational coach.

The implementation must evolve the existing system rather than create a parallel coach application.

## Non-negotiable product rules

1. Inspect and reuse the existing manual workout builder, preview, persistence, validation, and Garmin sync flow before introducing new components.
2. Do not build a coach-specific workout editor or a second Garmin upload pipeline.
3. Introduce or adapt one canonical `WorkoutPrescription` domain model used by manual, coach-generated, plan-generated, and daily-adapted workouts.
4. Coach output is a structured `WorkoutProposal`, never only prose.
5. A proposal must be previewable and editable through the shared workout UI.
6. The user must explicitly accept an exact immutable revision before it can be scheduled or synchronized to Garmin.
7. Any edit after acceptance creates a new revision and requires renewed acceptance.
8. Chat text itself never mutates the calendar, database, proposal state, or Garmin account. Mutations occur only through typed application-service tools.
9. Daily adaptations must be generated and validated by the deterministic adaptation/constraint pipeline before the LLM may recommend them.
10. In daily-adaptation mode, generated alternatives must not increase training load by default.
11. When decisive information is missing—especially pain, illness, goal, current ability, or available time—ask a targeted question instead of guessing.
12. Do not make medical diagnoses. Implement safety stops and recommend appropriate professional evaluation for red-flag symptoms.

## Architecture boundaries

- Domain and application layers must not import `garminconnect` directly.
- Wrap `python-garminconnect` behind explicit read/write ports and an anti-corruption adapter.
- Persist raw Garmin responses separately from normalized domain models.
- Pin the Garmin library version and protect the adapter with sanitized contract fixtures.
- Treat Garmin endpoints and response shapes as unstable external contracts.
- Keep proposal status, schedule status, and Garmin sync status separate.
- Make revisions immutable and keep complete provenance/audit metadata.
- Use idempotency keys for Garmin mutations.

## Training-science boundaries

- The deterministic rule engine owns calculations, eligibility, progression, workload budgets, quality-session spacing, safety stops, validation, and plan invariants.
- The LLM may interpret goals and feedback, ask questions, select among already validated options, and explain decisions.
- Do not encode “80/20”, the weekly 10-percent rule, Garmin Training Readiness, Body Battery, or ACWR/load ratio as universal laws.
- Use actual training history, consistency, current performance, long-run history, subjective feedback, and pain/illness signals as primary inputs.
- Use HRV, resting HR, sleep, Training Readiness, Recovery Time, Garmin load, stress, Body Battery, and VO2max only as secondary context with data-quality flags.
- Pain, illness, and cardiopulmonary warning signals override positive wearable scores.
- Store physiological intensity domains independently from vendor-specific zone numbers.
- Every workout template and constraint must reference an evidence ID or be explicitly marked as a product heuristic.

## Canonical workflow

Garmin import -> normalization -> athlete baseline -> performance/intensity model -> deterministic generator -> deterministic validator -> workout proposal -> shared preview/editor -> explicit acceptance -> schedule -> Garmin compiler -> Garmin sync -> execution reconciliation -> post-session feedback.

## Required implementation behavior

- Prefer small, reversible changes.
- Before editing, map the current data model, builder, UI component boundaries, API routes, sync service, and tests.
- Preserve existing behavior with characterization tests before refactoring.
- Use Pydantic v2 models or the project’s existing typed-model standard.
- Use database transactions around proposal acceptance and schedule changes.
- Return machine-readable validation codes plus user-readable messages.
- Log decision metadata, not sensitive raw health payloads.
- Never commit credentials, OAuth tokens, real Garmin payloads, location traces, or VCR cassettes containing personal data.
- Do not perform destructive migrations or real Garmin writes in tests.

## Verification requirements

For every change:

1. Run the smallest relevant unit and type checks first.
2. Add or update unit tests for domain logic.
3. Add property tests for invariants when a rule has combinatorial inputs.
4. Add contract tests around Garmin normalization/compilation.
5. Add scenario tests for user-visible coach workflows.
6. Verify that sync cannot occur without acceptance of the exact current revision.
7. Verify idempotency and retry behavior.
8. Report assumptions, files changed, tests run, and unresolved risks.

## Knowledge loading

Read only the domain files relevant to the current task. Do not load every knowledge file into context at once.

- Training principles: `docs/domain/training-principles.md`
- Intensity: `docs/domain/intensity-models.md`
- Garmin metrics: `docs/domain/garmin-metric-policy.md`
- Safety: `docs/domain/safety-policy.md`
- Daily adaptation: `docs/domain/daily-adaptation.md`
- Workout templates: `knowledge/workouts/*.yaml`
- Constraints: `knowledge/constraints/*.yaml`
- Evidence registry: `knowledge/evidence/index.yaml`

Treat structured constraints and application code as authoritative for allowed actions. Markdown and RAG provide explanation, not enforcement.

## Initial priority

1. Audit the existing workout-builder and sync flow.
2. Establish the canonical workout model and compatibility adapters.
3. Add proposal/revision/acceptance state and shared Preview/Edit/Accept UI.
4. Generate one validated coach workout.
5. Reuse the current Garmin compiler/sync path.
6. Add pre-session feedback and daily adaptation.
7. Only then implement weekly and multi-week adaptive plans.
```

## 20.2 Vorschlag `opencode.jsonc`

Die genaue Permission-Konfiguration muss an das Projekt und die verwendeten Tools angepasst werden. Ein konservativer Startpunkt:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": [
    "AGENTS.md",
    "docs/architecture/coach-system-overview.md",
    "docs/domain/safety-policy.md"
  ],
  "permission": {
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "edit": "ask",
    "bash": {
      "*": "ask",
      "git status*": "allow",
      "git diff*": "allow",
      "pytest*": "allow",
      "ruff*": "allow",
      "mypy*": "allow",
      "git push*": "deny"
    },
    "external_directory": "deny"
  }
}
```

Die Knowledge-YAMLs werden bewusst nicht pauschal als `instructions` geladen. OpenCode soll sie bedarfsorientiert lesen; andernfalls steigt der Kontextverbrauch und strukturierte Regeln werden fälschlich wie freier Prompttext behandelt.

## 20.3 Master-Prompt für eine neue OpenCode-Session

```text
You are extending an existing Garmin-based running application. Work as a senior Python/domain engineer with strong safety and auditability requirements.

First inspect the repository. Do not assume the framework, database, UI stack, or existing domain model. Locate and document:
- the current manual workout builder and its data model;
- workout preview/rendering components;
- validation logic;
- Garmin workout compilation, upload, scheduling, device push, update, delete, and unschedule flows;
- LangChain agent/tool setup;
- persistence models and migrations;
- tests and fixtures.

Goal: implement the next smallest vertical slice of the AI running coach while reusing the existing workout path. The target architecture is:
Garmin normalization -> athlete baseline -> deterministic generator/validator -> WorkoutProposal with immutable revisions -> shared preview/editor -> explicit acceptance -> existing Garmin sync pipeline.

Hard constraints:
- no second coach-specific editor;
- no direct LLM database or Garmin mutation;
- exact-revision acceptance before scheduling/sync;
- deterministic validation for every generated or user-edited workout;
- subjective pain/illness feedback has priority over wearable readiness;
- no medical diagnosis;
- no universal 10-percent, 80/20, ACWR, Body Battery, or Training Readiness law;
- pin and isolate python-garminconnect behind an adapter;
- no real account writes in automated tests.

Before coding, provide:
1. a repository map;
2. the current workout lifecycle;
3. reuse opportunities;
4. incompatibilities with the target model;
5. a minimal migration/refactor plan;
6. risks and assumptions;
7. the exact tests that will protect existing behavior.

Then implement only the agreed vertical slice. Make changes small and reversible. Add typed models, migrations, validation, audit metadata, idempotency, and tests. At the end, report files changed, behavior added, commands/tests run, and remaining gaps.
```

---

# 21. Schrittweise OpenCode-Arbeitsaufträge

Die folgenden Prompts sind nacheinander gedacht. Jeder Auftrag soll auf dem tatsächlichen Ergebnis des vorherigen aufbauen.

## Prompt 1 – Repository Audit und Characterization Tests

```text
Audit the existing repository for the Garmin workout lifecycle. Do not implement the coach yet.

Find the manual workout builder, canonical or de facto workout schema, frontend editor/preview, backend endpoints, database models, Garmin conversion/upload/schedule/device-push logic, and LangChain tools. Trace one manual workout from UI input to Garmin.

Deliver:
- an architecture map with file paths and dependencies;
- the current data contract at each boundary;
- duplicated or tightly coupled logic;
- exact components that can be reused by the coach;
- gaps relative to a source-agnostic WorkoutPrescription;
- a minimal refactor sequence;
- characterization tests that freeze current behavior.

Add only characterization tests and documentation unless a tiny non-behavioral extraction is needed to make tests possible. Do not create a new editor or sync path.
```

## Prompt 2 – Canonical `WorkoutPrescription`

```text
Using the repository audit, introduce a canonical source-agnostic WorkoutPrescription domain model without breaking the existing manual builder.

Requirements:
- support warm-up, work, recovery, cooldown, repeat blocks, time/distance/lap/open endings, and pace/HR/power/RPE/domain targets;
- include source and provenance metadata;
- keep Garmin IDs outside the domain aggregate;
- provide compatibility adapters from the current manual-builder payload to WorkoutPrescription and back where necessary;
- validate units and ranges;
- preserve existing UI behavior;
- add migrations only when required;
- add unit and property tests.

Do not add AI generation yet. Document unsupported Garmin constructs and conversion loss explicitly.
```

## Prompt 3 – Garmin Anti-Corruption Layer und Compiler

```text
Refactor the Garmin integration behind explicit ports. The domain/application layers must no longer import garminconnect directly.

Implement:
- GarminReadPort and GarminWorkoutWritePort;
- an adapter around the pinned python-garminconnect version;
- normalized response models and data-quality flags;
- GarminWorkoutCompiler from WorkoutPrescription to typed running workout models;
- explicit errors for unsupported constructs;
- idempotent upload/schedule/push operations;
- sanitized contract fixtures and tests;
- secure token/logging behavior.

Reuse the current working Garmin behavior. Do not perform live writes in tests. Record the exact library version and observed response shapes.
```

## Prompt 4 – Proposal, Revision, Preview/Edit/Accept

```text
Implement the human-in-the-loop WorkoutProposal workflow using the shared workout editor and preview.

Required states:
- proposal status: draft, proposed, accepted, rejected, expired, superseded;
- separate scheduling and Garmin sync statuses;
- immutable WorkoutRevision records;
- optimistic concurrency on acceptance;
- full provenance and audit events.

UI requirements:
- show a structured proposal card;
- open the existing shared preview/editor;
- allow edit, accept, reject;
- show validation errors and the diff from the previous revision;
- do not expose Garmin sync before the exact current revision is accepted.

Backend requirements:
- every edit is revalidated;
- acceptance is transactional;
- schedule/sync services verify accepted revision server-side;
- repeated requests are idempotent.

Add API, domain, integration, and end-to-end tests.
```

## Prompt 5 – Deterministic Single-Workout Generator

```text
Implement a deterministic single-running-workout generator driven by versioned YAML templates and constraints.

Initial supported workout types:
- easy run;
- recovery run;
- long run;
- threshold cruise intervals;
- simple VO2 interval workout;
- strides.

Inputs:
- athlete baseline;
- current goal;
- available time/date;
- recent training history;
- current performance/intensity model;
- pre-session safety context.

Outputs:
- one or more WorkoutPrescription candidates;
- load estimates and confidence;
- validation reports;
- evidence/template/rule versions;
- structured reasons for selection and rejection.

The generator must not call the LLM. Mark heuristic parameters explicitly and avoid false precision. Add deterministic snapshot tests and Hypothesis properties.
```

## Prompt 6 – LangChain Coach Tools für Einzelvorschläge

```text
Connect the existing LangChain coach to the proposal services through typed tools.

Add read tools for athlete context and mutating tools for proposal creation/update/acceptance. The LLM may interpret the user request and explain results, but it may only present candidates returned by the generator/validator.

Enforce:
- no direct database or Garmin access from the LLM;
- no implied acceptance;
- targeted questions when goal, current ability, pain/illness, or available time is missing;
- tool result is authoritative;
- user-facing explanation includes purpose, structure, intensity fallback, and why it fits the baseline.

Create LLM evaluation cases for hallucinated pace, missing data, unsafe requests, and attempted direct sync.
```

## Prompt 7 – Daily Adaptation und subjektives Feedback

```text
Implement the Daily Adaptation vertical slice.

Add structured pre-session and post-session feedback. Safely extract chat free text into the schema with confidence and needs_clarification. Do not diagnose.

Implement deterministic adaptation options:
- keep;
- reduce volume;
- reduce intensity;
- simplify quality;
- replace with easy/recovery;
- defer key session;
- rest;
- request week replan.

Rules:
- daily-adaptation candidates may not increase estimated load by default;
- red flags block running proposals;
- pain affecting gait overrides wearable readiness;
- moving a workout must revalidate the whole week;
- accepted revisions use the existing shared editor and Garmin flow.

Add scenario tests for: low motivation only, high fatigue, poor sleep plus low HRV, localized pain, fever, and a quality session moved next to a long run.
```

## Prompt 8 – Wochenplanung

```text
Implement a one-week running planner after the single-workout and daily-adaptation flows are stable.

The planner must place already supported WorkoutPrescription templates under deterministic constraints for:
- available days;
- current frequency and volume baseline;
- quality density;
- long-run history;
- goal emphasis;
- current phase;
- user preferences;
- recovery and missed-session handling.

The LLM may explain the week, but it must not invent sessions outside the validated output. Each planned session remains a proposal until accepted according to the chosen batch-approval UX. Define whether acceptance is per workout or per immutable week revision and implement the state model explicitly.
```

## Prompt 9 – Mehrwochenplan und adaptive Replanung

```text
Implement versioned multi-week plans only after the weekly planner passes all scenario and property tests.

Requirements:
- phases: onboarding/base/build/specific/taper/recovery;
- goals: 5K, 10K, half marathon, marathon;
- baseline-relative volume and long-run progression;
- race-specific emphasis without copying elite absolute volume;
- taper as a configurable range;
- plan revisions with immutable history;
- no catch-up stacking after missed sessions;
- replan triggers for persistent fatigue, illness, pain, multiple missed sessions, material goal/date changes, or constraint conflicts;
- explicit confidence and unresolved assumptions.

Add simulation tests across synthetic athletes and inspect distributions, not just individual snapshots.
```

## Prompt 10 – Hardening und Observability

```text
Harden the coach for production.

Add:
- decision traces with rule/template/evidence versions;
- structured metrics for proposal generation, validation failures, acceptance, edits, sync success, and adaptation types;
- privacy-safe logs;
- retry and idempotency dashboards;
- stale Garmin data warnings;
- dependency and schema drift detection;
- feature flags and rollback path;
- data export/deletion support;
- load tests for plan generation;
- red-team tests for prompt injection and unauthorized mutations.

Do not expose raw health data, credentials, routes, or location traces in logs or model prompts.
```

---

# 22. Teststrategie

## 22.1 Unit Tests

- Pace-/Speed-/Duration-Konvertierung;
- HRR-/LTHR-Zonen;
- Critical-Speed-Fit und Confidence;
- Baseline-Median/MAD;
- Workout-Template-Expansion;
- Load Estimate;
- State Transitions;
- Validation Codes;
- Garmin Mapping;
- Feedback Triage;
- Week Replan Trigger.

## 22.2 Property-Based Tests

Mit Hypothesis oder gleichwertig:

1. Workout-Schritte haben nie negative Dauer/Distanz.
2. `low <= high` für alle Targets.
3. Repeat-Blöcke erzeugen korrekte Gesamtdauer.
4. Daily Adaptation erhöht Last standardmäßig nie.
5. Safety Stop kann nicht durch positiven Readiness-Score aufgehoben werden.
6. Nicht akzeptierte Revision kann nicht synchronisiert werden.
7. Veraltete Revision kann nicht akzeptiert werden.
8. Idempotenter Sync erzeugt höchstens einen aktiven Garmin-Record.
9. Ein Plan enthält keine unzulässige Qualitätstagsdichte.
10. Ausgelassene Workouts werden nicht automatisch gestapelt.
11. Compiler-Output erfüllt Garmin-Schema oder liefert expliziten Fehler.
12. Einheitenkonvertierungen sind round-trip-stabil innerhalb definierter Toleranz.

## 22.3 Contract Tests

- gespeicherte anonymisierte Garmin-Responses;
- verschiedene Shapes für Training Readiness;
- fehlende Felder und neue Felder;
- HRV `None` oder unvollständige Baseline;
- Workout Upload/Update/Schedule/Unschedule Payloads;
- typed Model Validation Errors mit Erhalt des Raw Payloads.

## 22.4 Scenario Tests

### A: Coach erzeugt Easy Run

- 4 Wochen konsistente Historie;
- 45 Minuten verfügbar;
- Proposal wird angezeigt;
- Nutzer reduziert auf 35 Minuten;
- erneute Validierung;
- Annahme;
- einmaliger Sync.

### B: Nutzer fühlt sich unmotiviert, aber körperlich gut

- Coach fragt nicht unnötig nach medizinischen Details;
- bietet unverändert, verkürzt und lockere Zeitoption;
- kein automatischer Ruhetag nur wegen Motivation.

### C: Lokalisierter Schmerz und veränderter Laufstil

- Safety Stop;
- kein Laufproposal;
- keine positive Wearable-Metrik überschreibt Stop.

### D: Schlechter Schlaf plus niedrige HRV, kein Schmerz

- keine Diagnose;
- reduzierte oder verschobene Optionen;
- Nutzer kann trotzdem unverändert wählen, sofern keine harte Regel verletzt wird und Risiko transparent ist.

### E: Intervalle auf morgen verschieben

- morgiger Long Run kollidiert;
- Engine fordert Wochen-Replan statt bloßer Verschiebung.

### F: Doppelter Sync-Click

- ein Garmin-Workout;
- zweiter Request liefert bestehenden Erfolg.

### G: Garmin API 500 nach Upload vor lokaler Bestätigung

- Reconciliation prüft vorhandenes Workout;
- kein blindes erneutes Duplizieren;
- Status und Recovery-Action nachvollziehbar.

## 22.5 LLM-Evals

- extrahiert Feedback korrekt;
- fragt bei „Knie komisch“ nach;
- erfindet keine Diagnose;
- erfindet keine Pace bei fehlender Basis;
- präsentiert nur Tool-Kandidaten;
- behauptet keinen Sync ohne Tool-Erfolg;
- interpretiert „klingt gut“ nicht automatisch als Garmin-Sync, wenn die UI-Bestätigung fehlt;
- reagiert auf Prompt Injection in Freitext nicht mit Regelumgehung;
- erklärt Unsicherheit und Datenalter.

## 22.6 UI-E2E

- Manual und Coach öffnen denselben Editor;
- Source Badge unterscheidet Herkunft;
- Diff nach User Edit;
- Accept exakt aktuelle Revision;
- Sync Button erst nach Accept;
- Fehler und Retry;
- mobile Darstellung;
- Accessibility und Tastatursteuerung.

## 22.7 Simulation

Synthetische Athleten:

- Anfänger 2 Läufe/Woche;
- konsistenter 10-km-Läufer 4 Läufe/Woche;
- HM-Läufer mit unregelmäßigen Wochen;
- Marathonläufer mit hoher Basis;
- Rückkehr nach 3 Wochen Pause;
- wiederholte hohe Müdigkeit;
- fehlende HRV-/Sleep-Daten;
- unrealistisches Ziel.

Zu prüfen:

- Lastverteilungen;
- Qualitätstage;
- Long-Run-Progression;
- Häufigkeit von Safety Stops und Warnungen;
- Zielabhängige Spezifität;
- Taper-Verhalten;
- Stabilität bei fehlenden Daten.

---

# 23. MVP-Roadmap und Arbeitspakete

## WP0 – Bestehendes System verstehen

**Ergebnis:** Repo Map, Lifecycle, Characterization Tests, Liste wiederverwendbarer Komponenten.

**Nicht enthalten:** neue Coach-UI oder Planlogik.

## WP1 – Gemeinsames Workout-Domainmodell

**Ergebnis:** `WorkoutPrescription`, Adapter für bestehenden Builder, Validator-Grundlage, unveränderte manuelle Funktion.

## WP2 – Garmin-Adapter und gemeinsamer Compiler

**Ergebnis:** isolierte Integration, gepinnte Bibliothek, Contract Tests, idempotente Mutationen.

## WP3 – Proposal/Revision/Human Approval

**Ergebnis:** Coach-Proposal Card, gemeinsamer Editor, Preview/Edit/Accept/Reject, Audit, exact-revision acceptance.

## WP4 – Ein deterministischer Coach-Vorschlag

**Ergebnis:** zunächst Easy Run und ein einfaches strukturiertes Workout; validiert und erklärbar.

## WP5 – Sync über bestehenden Flow

**Ergebnis:** akzeptierte Proposal-Revision wird über denselben Compiler und Sync-Service wie manuell erstellte Workouts synchronisiert.

## WP6 – Daily Adaptation

**Ergebnis:** heutiges Workout + subjektives Feedback + Garmin-Kontext → validierte Alternativen → Nutzerwahl → neue akzeptierte Revision.

## WP7 – Wochenplan

**Ergebnis:** eine Woche mit gemeinsamen Templates und Constraints; klare Batch- oder Einzelannahme.

## WP8 – Mehrwochenpläne

**Ergebnis:** 5 km, 10 km, HM, Marathon; Phasen, Taper, Planrevisionen.

## WP9 – Adaptive Replanung

**Ergebnis:** Ausführung und Feedback verändern zukünftige Wochen kontrolliert.

## WP10 – Produktionshärtung

**Ergebnis:** Observability, Datenschutz, Drift-Erkennung, Feature Flags, Evals, Reconciliation.

## Priorisierung

```text
MUST: WP0–WP6
SHOULD: WP7
LATER: WP8–WP10
```

---

# 24. Definition of Done

Der erste Coach-MVP ist fertig, wenn:

- der bestehende manuelle Workout-Flow weiterhin funktioniert;
- Manual und Coach dasselbe kanonische Modell und dieselbe UI verwenden;
- der Coach mindestens einen strukturierten Laufvorschlag erzeugt;
- jede Proposal- und User-Revision deterministisch validiert wird;
- Nutzer Vorschlag ansehen, ändern, annehmen und ablehnen kann;
- Garmin-Sync ohne Annahme der exakten Revision technisch unmöglich ist;
- Doppel-Sync idempotent ist;
- Provenance und Audit vollständig sind;
- subjektives Pre-Session-Feedback erfasst wird;
- Schmerz-/Krankheits-Guardrails funktionieren;
- Daily Adaptation mindestens `keep`, `reduce volume`, `replace with easy`, `defer` und `rest` unterstützt;
- Wochenkonflikte eine Replan-Anforderung auslösen;
- Unit-, Property-, Contract-, Scenario-, LLM- und E2E-Tests grün sind;
- echte Garmin-Daten und Tokens nicht in Logs/Testfixtures landen;
- alle Heuristiken als solche gekennzeichnet sind;
- Knowledge-, Rule- und Generator-Version pro Vorschlag gespeichert werden;
- Fehlerfälle verständlich in der UI erscheinen.

---

# 25. Offene Entscheidungen

Diese Punkte müssen nach Repo-Audit oder Produktentscheidung konkretisiert werden:

1. Frontend-Framework und exakte Wiederverwendungsgrenze des Builders.
2. Batch-Accept für einen Wochenplan versus Annahme jeder Einheit.
3. Ob ein akzeptiertes Workout vor Sync noch automatisch geplant wird oder beides getrennte Aktionen sind.
4. Welche Garmin-Geräte/Workout-Targets im MVP unterstützt werden.
5. Umgang mit bereits synchronisierten Workouts bei einer Tagesänderung: Update in place, Unschedule/Delete oder neue Version.
6. Datenhaltung und Verschlüsselungsmodell für Garmin-Tokens.
7. Wie aktuelle Race-/Time-Trial-Daten eingegeben oder erkannt werden.
8. Ob Critical Speed im MVP oder erst später berechnet wird.
9. Welche externen Umweltinformationen eingebunden werden.
10. Welche Produktdefaults für Volumenbudgets je Erfahrungsstufe gewählt und wie sie fachlich reviewed werden.
11. Welche medizinischen Disclaimer und Eskalationstexte juristisch/produktseitig erforderlich sind.
12. Wie Feedback- und Entscheidungsdaten für spätere Personalisierung genutzt werden dürfen.

---
# 26. Quellen und weiterführende Literatur

## 26.1 Verwendung der Quellen

Die Quellenbasis ist bewusst in drei Gruppen getrennt:

1. **Trainingswissenschaftliche Evidenz** begründet Prinzipien, Risikosignale und die zulässige Richtung von Regeln.
2. **Hersteller- und Bibliotheksdokumentation** beschreibt, welche Garmin-Werte und Mutationen technisch existieren. Sie belegt nicht automatisch die wissenschaftliche Validität eines proprietären Scores.
3. **Produktheuristiken** – etwa konkrete Defaultbereiche, UI-Zustände oder Eskalationsstufen – sind Implementierungsentscheidungen. Sie müssen als solche versioniert und dürfen nicht fälschlich als durch ein Paper exakt vorgegeben dargestellt werden.

Eine Quellen-ID in einer Regel bedeutet daher nicht zwingend, dass das Paper exakt den numerischen Produktwert vorgibt. Sie dokumentiert, welches übergeordnete Prinzip die Regel stützt. Numerische Heuristiken erhalten zusätzlich `product_default: true` und eine fachliche Review-Historie.

## 26.2 Evidence-to-Rule-Matrix

| Regel-/Wissensbereich | Hauptquellen | Implementierungsfolgen |
|---|---|---|
| Trainingsverteilung | Casado et al. 2022; Stöggl & Sperlich 2015; Oliveira et al. 2024; Rosenblat et al. 2019 | überwiegend lockere Zeit, keine starre 80/20-Pflicht, ziel- und phasenabhängige Verteilung |
| Intervallgestaltung | Buchheit & Laursen 2013 I/II | Work-/Recovery-Dauer, Intensität, Wiederholungen, Serien und mechanische Last getrennt modellieren |
| Schwelle/Critical Speed | Poole et al. 2016; Jones & Vanhatalo 2017 | aktuelle Leistungsreferenzen bevorzugen, Modellkonfidenz speichern, `D′/W′` nicht überpräzise verwenden |
| Talk Test/RPE/sRPE | Persinger et al. 2004; Foster et al. 2008/2021; Haddad et al. 2017 | robuste Fallbacks bei Hitze, Hügeln oder unsicheren Pace-/HR-Daten; subjektive Last als First-Class-Input |
| Taper | Bosquet et al. 2007 | Volumen deutlich reduzieren, Intensität teilweise erhalten, genaue Kurve ziel- und athletenabhängig |
| Wöchentliche 10-%-Regel | Buist et al. 2008; Damsted et al. 2018; Fredette et al. 2022 | nicht als universelle harte Regel codieren |
| Einzeldistanz-Spikes | Frandsen et al. 2025 | geplante Distanz gegen längsten Lauf der letzten 30 Tage prüfen; >10 % als sichtbaren Risikomarker behandeln, nicht als sichere/gefährliche Naturgrenze |
| ACWR | Impellizzeri et al. 2020; Frandsen et al. 2025 | nicht als kausales Verletzungsorakel oder alleinige Freigabe verwenden |
| Hitze | Racinais et al. 2015 | Pace sekundär, RPE/HR/Kontext stärker gewichten; Akklimatisierung, Kürzung und Alternativen |
| Krankheit/Return to Sport | Snyders et al. 2022; Kaulback et al. 2023; ACC 2022 | keine Diagnose; symptomorientierte Stops, gezielte Rückfragen und professionelle Abklärung bei Warnzeichen |
| HRV-geführtes Training | Manresa-Rocamora et al. 2021; Düking et al. 2021 | HRV als Trend und Modifikator, nicht als alleinige Tagesentscheidung |
| Schlaf-Wearables | Miller et al. 2022 | Schlafdauer/-timing hilfreicher als exakte Schlafstadien; Unsicherheit speichern |
| Garmin-VO2max | Engel et al. 2025/2026 | als Trend/Plausibilität, nicht als exakte Laborgröße; höhere Unsicherheit bei sehr gut Trainierten |
| Garmin-Kompositscores | Garmin-Handbücher/Support | als sekundäre, proprietäre Signale; Rohkomponenten und subjektives Befinden priorisieren |
| Technische Garmin-Integration | `python-garminconnect` Repository | Adapter, Version Pinning, Contract Tests, Raw-Payload-Erhalt und idempotente Mutationen |
| OpenCode-Kontext | offizielle OpenCode Rules/Config Docs | `AGENTS.md` plus modulare `instructions`; Aufgabenbezogenes Laden statt ein riesiger unstrukturierter Prompt |

## 26.3 Trainingsstruktur, Periodisierung und Intensitätsverteilung

1. **Casado A, González-Mohíno F, González-Ravé JM, Foster C.** Training Periodization, Methods, Intensity Distribution, and Volume in Highly Trained and Elite Distance Runners: A Systematic Review. *International Journal of Sports Physiology and Performance*. 2022;17(6):820–833. [DOI: 10.1123/ijspp.2021-0435](https://doi.org/10.1123/ijspp.2021-0435)
2. **Stöggl TL, Sperlich B.** The Training Intensity Distribution among Well-Trained and Elite Endurance Athletes. *Frontiers in Physiology*. 2015;6:295. [DOI: 10.3389/fphys.2015.00295](https://doi.org/10.3389/fphys.2015.00295)
3. **Oliveira PS, Boppre G, Fonseca H.** Comparison of Polarized Versus Other Types of Endurance Training Intensity Distribution on Athletes’ Endurance Performance: A Systematic Review with Meta-analysis. *Sports Medicine*. 2024;54:2071–2095. [DOI: 10.1007/s40279-024-02034-z](https://doi.org/10.1007/s40279-024-02034-z)
4. **Rosenblat MA, Perrotta AS, Vicenzino B.** Polarized vs. Threshold Training Intensity Distribution on Endurance Sport Performance: A Systematic Review and Meta-Analysis of Randomized Controlled Trials. *Journal of Strength and Conditioning Research*. 2019;33(12):3491–3500. [DOI: 10.1519/JSC.0000000000002618](https://doi.org/10.1519/JSC.0000000000002618)
5. **Nøst HL, Aune MA, van den Tillaar R.** The Effect of Polarized Training Intensity Distribution on Maximal Oxygen Uptake and Work Economy Among Endurance Athletes: A Systematic Review. *Sports*. 2024;12(12):326. [DOI: 10.3390/sports12120326](https://doi.org/10.3390/sports12120326)
6. **Bosquet L, Montpetit J, Arvisais D, Mujika I.** Effects of Tapering on Performance: A Meta-Analysis. *Medicine & Science in Sports & Exercise*. 2007;39(8):1358–1365. [DOI: 10.1249/mss.0b013e31806010e0](https://doi.org/10.1249/mss.0b013e31806010e0)

## 26.4 Intervalltraining, Intensitätsdomänen und Leistungsmodelle

7. **Buchheit M, Laursen PB.** High-Intensity Interval Training, Solutions to the Programming Puzzle: Part I: Cardiopulmonary Emphasis. *Sports Medicine*. 2013;43:313–338. [DOI: 10.1007/s40279-013-0029-x](https://doi.org/10.1007/s40279-013-0029-x)
8. **Buchheit M, Laursen PB.** High-Intensity Interval Training, Solutions to the Programming Puzzle. Part II: Anaerobic Energy, Neuromuscular Load and Practical Applications. *Sports Medicine*. 2013;43:927–954. [DOI: 10.1007/s40279-013-0066-5](https://doi.org/10.1007/s40279-013-0066-5)
9. **Poole DC, Burnley M, Vanhatalo A, Rossiter HB, Jones AM.** Critical Power: An Important Fatigue Threshold in Exercise Physiology. *Medicine & Science in Sports & Exercise*. 2016;48(11):2320–2334. [DOI: 10.1249/MSS.0000000000000939](https://doi.org/10.1249/MSS.0000000000000939)
10. **Jones AM, Vanhatalo A.** The ‘Critical Power’ Concept: Applications to Sports Performance with a Focus on Intermittent High-Intensity Exercise. *Sports Medicine*. 2017;47(Suppl 1):65–78. [DOI: 10.1007/s40279-017-0688-0](https://doi.org/10.1007/s40279-017-0688-0)
11. **Persinger R, Foster C, Gibson M, Fater DCW, Porcari JP.** Consistency of the Talk Test for Exercise Prescription. *Medicine & Science in Sports & Exercise*. 2004;36(9):1632–1636. [PubMed](https://pubmed.ncbi.nlm.nih.gov/15354048/)
12. **Foster C et al.** The Talk Test as a Marker of Exercise Training Intensity. *Journal of Cardiopulmonary Rehabilitation and Prevention*. 2008. [DOI: 10.1097/01.HCR.0000311504.41775.78](https://doi.org/10.1097/01.HCR.0000311504.41775.78)
13. **Haddad M et al.** Session-RPE Method for Training Load Monitoring: Validity, Ecological Usefulness, and Influencing Factors. *Frontiers in Neuroscience*. 2017. [PubMed](https://pubmed.ncbi.nlm.nih.gov/29163016/)
14. **Foster C et al.** 25 Years of Session Rating of Perceived Exertion: Historical Perspective and Development. *International Journal of Sports Physiology and Performance*. 2021. [DOI: 10.1123/ijspp.2020-0599](https://doi.org/10.1123/ijspp.2020-0599)

## 26.5 Trainingslast und Verletzungsrisiko

15. **Buist I et al.** No Effect of a Graded Training Program on the Number of Running-Related Injuries in Novice Runners: A Randomized Controlled Trial. *American Journal of Sports Medicine*. 2008;36(1):33–39. [DOI: 10.1177/0363546507307505](https://doi.org/10.1177/0363546507307505)
16. **Damsted C et al.** Is There Evidence for an Association Between Changes in Training Load and Running-Related Injuries? A Systematic Review. *International Journal of Sports Physical Therapy*. 2018;13(6):931–942. [PubMed/PMC](https://pubmed.ncbi.nlm.nih.gov/30534459/)
17. **Fredette A et al.** The Association Between Running Injuries and Training Parameters: A Systematic Review. *Journal of Athletic Training*. 2022;57(7):650–671. [DOI: 10.4085/1062-6050-0195.21](https://doi.org/10.4085/1062-6050-0195.21)
18. **Impellizzeri FM, Tenan MS, Kempton T, Novak A, Coutts AJ.** Acute:Chronic Workload Ratio: Conceptual Issues and Fundamental Pitfalls. *International Journal of Sports Physiology and Performance*. 2020;15(6):907–913. [DOI: 10.1123/ijspp.2019-0864](https://doi.org/10.1123/ijspp.2019-0864)
19. **Frandsen JSB et al.** How Much Running Is Too Much? Identifying High-Risk Running Sessions in a 5200-Person Cohort Study. *British Journal of Sports Medicine*. 2025;59(17):1203–1210. [DOI: 10.1136/bjsports-2024-109380](https://doi.org/10.1136/bjsports-2024-109380)
20. **Risk Factors for Running-Related Injuries: An Umbrella Systematic Review.** *Journal of Sport and Health Science*. 2024. [DOI: 10.1016/j.jshs.2024.04.011](https://doi.org/10.1016/j.jshs.2024.04.011)
21. **Are Alterations in Running Biomechanics Associated with Running Injuries? A Systematic Review with Meta-analysis.** *Brazilian Journal of Physical Therapy*. 2023. [DOI: 10.1016/j.bjpt.2023.100538](https://doi.org/10.1016/j.bjpt.2023.100538)

## 26.6 Erholung, HRV, Schlaf und Wearables

22. **Manresa-Rocamora A et al.** Heart Rate Variability-Guided Training for Enhancing Cardiac-Vagal Modulation, Aerobic Fitness, and Endurance Performance: A Methodological Systematic Review with Meta-Analysis. *International Journal of Environmental Research and Public Health*. 2021;18(19):10299. [DOI: 10.3390/ijerph181910299](https://doi.org/10.3390/ijerph181910299)
23. **Düking P et al.** Monitoring and Adapting Endurance Training on the Basis of Heart Rate Variability Monitored by Wearable Technologies: A Systematic Review with Meta-analysis. *Journal of Science and Medicine in Sport*. 2021;24(11):1180–1192. [DOI: 10.1016/j.jsams.2021.04.012](https://doi.org/10.1016/j.jsams.2021.04.012)
24. **Miller DJ, Sargent C, Roach GD.** A Validation of Six Wearable Devices for Estimating Sleep, Heart Rate and Heart Rate Variability in Healthy Adults. *Sensors*. 2022;22(16):6317. [DOI: 10.3390/s22166317](https://doi.org/10.3390/s22166317)
25. **Engel FA, Masur L, Sperlich B, Düking P.** Validity of VO₂max Estimates from the Forerunner 245 Smartwatch in Highly vs. Moderately Trained Endurance Athletes. *European Journal of Applied Physiology*. Online 2025; print 2026. [DOI: 10.1007/s00421-025-05923-x](https://doi.org/10.1007/s00421-025-05923-x)

## 26.7 Hitze, Krankheit und Safety Escalation

26. **Racinais S et al.** Consensus Recommendations on Training and Competing in the Heat. *British Journal of Sports Medicine*. 2015;49(18):1164–1173. [DOI: 10.1136/bjsports-2015-094915](https://doi.org/10.1136/bjsports-2015-094915)
27. **Snyders C et al.** Acute Respiratory Illness and Return to Sport: A Systematic Review and Meta-analysis by a Subgroup of the IOC Consensus on ‘Acute Respiratory Illness in the Athlete’. *British Journal of Sports Medicine*. 2022;56(4):223–231. [DOI: 10.1136/bjsports-2021-104719](https://doi.org/10.1136/bjsports-2021-104719)
28. **Kaulback K et al.** The Effects of Acute Respiratory Illness on Exercise and Sports Performance Outcomes in Athletes: A Systematic Review. *European Journal of Sport Science*. 2023;23(7):1356–1374. [DOI: 10.1080/17461391.2022.2089914](https://doi.org/10.1080/17461391.2022.2089914)
29. **ACC Expert Consensus Decision Pathway.** Cardiovascular Sequelae of COVID-19 and Return to Play. *Journal of the American College of Cardiology*. 2022;79(17):1717–1756. [DOI: 10.1016/j.jacc.2022.02.003](https://doi.org/10.1016/j.jacc.2022.02.003)

## 26.8 Garmin- und Bibliotheksdokumentation

30. **cyberjunky/python-garminconnect.** Unofficial Python wrapper for Garmin Connect; Health-, Performance-, Activity- und Workout-Funktionen einschließlich typed Workout Uploads und Scheduling. [GitHub Repository](https://github.com/cyberjunky/python-garminconnect)
31. **Garmin Training Readiness.** Komposit aus Sleep Score, Recovery Time, HRV Status, Acute Load, Sleep History und Stress History. [Garmin Owner’s Manual](https://www8.garmin.com/manuals/webhelp/GUID-025D75CF-3445-49E1-8D81-1AA74AB4E00F/EN-US/GUID-C21BE0C8-A08E-4DA1-B6C6-2E0E2DDDB372.html)
32. **Garmin HRV Status.** Individuelle Baseline erfordert ungefähr drei Wochen konsistenter nächtlicher Daten; Garmin weist auf Schätzcharakter und Nicht-Medizinprodukt hin. [Garmin Support](https://support.garmin.com/en-IE/aviation/faq/HnFAR4oFRF4kHeqYme3bU6/)
33. **Garmin Training Load.** Acute Load als gewichtete EPOC-Summe der letzten sieben Tage; Chronic Load und Load Ratio als herstellerspezifische Ableitungen. [Garmin Support](https://support.garmin.com/en-GB/navionics/faq/SEkNpdGyhR917js0qQL3Q6/)
34. **Garmin Recovery Time.** Herstellerbeschreibung der dynamischen Recovery-Time-Schätzung. [Garmin Support](https://support.garmin.com/en-IE/aviation/faq/8ImmxVkZMh4EYYq5Zp2bR8/)

## 26.9 OpenCode-Dokumentation

35. **OpenCode Rules.** Projektweite `AGENTS.md`, globale Regeln und externe Instruction-Dateien. [Official Documentation](https://dev.opencode.ai/docs/rules/)
36. **OpenCode Configuration.** Projektkonfiguration und `instructions`-Pfade in `opencode.json`. [Official Documentation](https://opencode.ai/docs/config/)

## 26.10 Quellenpflege im Repository

Empfohlene Ablage:

```text
knowledge/evidence/
├── evidence-index.yaml
├── training-intensity-distribution.md
├── workout-prescription.md
├── injury-load-guardrails.md
├── hrv-sleep-wearables.md
├── heat-and-illness.md
└── vendor/
    ├── garmin-metrics.md
    ├── python-garminconnect-capabilities.md
    └── opencode-context-loading.md
```

Jeder Eintrag in `evidence-index.yaml` sollte mindestens enthalten:

```yaml
id: E-INJURY-SESSION-SPIKE-001
claim: >-
  In a large prospective cohort, single-session distance above 110 percent
  of the longest run in the prior 30 days was associated with a higher rate
  of self-reported overuse injury.
source:
  title: How much running is too much?
  year: 2025
  doi: 10.1136/bjsports-2024-109380
evidence_level: B
study_design: prospective_observational_cohort
population:
  n: 5205
  notes: predominantly experienced middle-aged male runners using Garmin devices
limitations:
  - observational_not_causal
  - self_reported_injury
  - limited_subgroup_generalization
allowed_uses:
  - visible_warning
  - conservative_alternative_ranking
forbidden_uses:
  - claim_that_up_to_ten_percent_is_safe
  - medical_prediction
  - sole_injury_risk_score
last_reviewed: 2026-08-20
```

Damit bleibt nachvollziehbar, welche Regeln aus belastbarer Evidenz stammen, welche nur produktseitige Sicherheitsdefaults sind und wann eine Quelle erneut geprüft werden muss.

---

# Schlussfolgerung

Die zentrale Produktidee ist tragfähig, wenn der Coach als **erklärende, dialogfähige Steuerungsschicht über einer deterministischen Trainings- und Workflow-Engine** verstanden wird. Das LLM soll die persönliche Situation erfassen und verständlich machen; die Anwendung selbst muss Datenqualität, Trainingslogik, Safety, Versionierung, Zustimmung und Garmin-Mutationen kontrollieren.

Der sinnvollste nächste Entwicklungsschritt ist daher nicht der vollständige Marathonplan, sondern ein vertikaler Slice:

```text
bestehenden Builder inspizieren
→ kanonisches WorkoutPrescription-Modell extrahieren
→ einen Easy-Run-Vorschlag deterministisch erzeugen und validieren
→ im bestehenden Editor anzeigen und ändern
→ exakte Revision annehmen
→ idempotent über den bestehenden Garmin-Flow synchronisieren
→ denselben Flow für eine chatbasierte Tagesanpassung wiederverwenden
```

Wenn dieser Slice stabil, getestet und auditierbar funktioniert, können Wochen- und Mehrwochenpläne auf derselben Grundlage ergänzt werden, ohne einen zweiten, schwer kontrollierbaren Coach-Stack zu erzeugen.
