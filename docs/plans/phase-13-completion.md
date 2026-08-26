# Phase 13 - Produktionshärtung - Completion

**Status:** Completed  
**Date:** 26. August 2026  
**Browser validation:** skipped by explicit user instruction

## Delivered

- Zentrale Security-Header für dynamische und statische Antworten. Personalisierte Seiten,
  Downloads, Redirects, Fehler und Coach-Streams erhalten `private, no-store`; HSTS wird nur in
  Production gesetzt. Die CSP begrenzt Base URL, Form Actions, Objects und Framing, ohne die noch
  extern geladenen UI-Abhängigkeiten zu brechen.
- Thread-sichere In-Process Sliding-Window-Limits für allgemeine Mutationen, Coach und
  Authentifizierung. Sie entsprechen der unterstützten Ein-Worker-Architektur und laufen nach
  erfolgreicher CSRF-Prüfung.
- Vollständiger, user-scoped ZIP-Export aller 40 Anwendungstabellen und lokaler
  Rohaktivitätsdateien mit Schema-Version, Tabellenzahlen und SHA-256-Dateimanifest. Tokens,
  Sessions, Logs und Host-Backups sind explizit ausgeschlossen.
- Lokale Account-Löschung mit exakter Bestätigungsphrase, Garmin-Account-Lock, Abbruch offener
  MFA-Zustände, gestagten Roh-/Tokenverzeichnissen, User-Root-Cascade und Session-Löschung. Externe
  Garmin-Daten werden nicht als gelöscht behauptet.
- Privacy-safe Decision Traces aus immutable Revisionen und Validation Runs. Sichtbar sind nur
  Template-, Generator-, Regel-, Knowledge-, Model- und Prompt-Versionen, Evidence-Referenzen und
  Regelcodes.
- Token-geschützte Betriebsmetriken unter `/api/metrics`, aggregiert aus Audit Events, Validation
  Runs und Garmin Operations ohne Nutzer- oder Workoutdimensionen.
- Periodische Erkennung hängen gebliebener Garmin Operations zusätzlich zur Startup-Reparatur.
  Pending/Unknown-Vorgänge sind in den Einstellungen sichtbar und werden nicht blind wiederholt.
- Strikte Garmin-Kalender-Contract-Erkennung verhindert, dass ein unbekanntes Response-Schema als
  bewiesene Abwesenheit interpretiert wird.
- Versionierte, vollständig synthetische Garmin-/Coach-Contract-Fixtures und ein deterministischer
  Prompt-Injection-/Mutation-Boundary-Test.
- Globaler Feature-Flag-Rollback plus optionale interne Nutzerkohorte über
  `COACH_ROLLOUT_USER_IDS`; ungültige Kohortenwerte schließen Funktionen statt sie zu öffnen.
- CI baut committed Tailwind-CSS reproduzierbar und baut das Container-Image auch für Pull
  Requests, bevor ein Publish möglich ist.

## Operational Boundaries

- Rate Limits sind absichtlich prozesslokal. Mehrere Uvicorn-Worker bleiben aus denselben Gründen
  wie Scheduler und Garmin-Locks nicht unterstützt.
- `/api/metrics` ist ohne `METRICS_BEARER_TOKEN` nicht auffindbar und liefert selbst mit Token nur
  aggregierte Zähler.
- Account-Löschung wirkt lokal. Garmin, OAuth-Provider, rotierende Shared Logs, SQLite-Free-Pages,
  Host-Snapshots und Backups liegen außerhalb einer sofortigen sicheren Überschreibung.
- Nicht reconciliierbare unbekannte Garmin Upload-/Update-/Push-/Delete-Ausgänge bleiben bewusst
  blockiert, bis der Remote-Zustand manuell geprüft wurde.

## Verification

- `uv run pytest`: 441 passed, 28 known dependency/deprecation warnings.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 201 files formatted.
- `uv run ty check`: passed.
- `git diff --check`: passed; only the repository's existing Windows LF/CRLF notices were emitted.
- `npm ci`: 0 vulnerabilities; `npm run build:css`: passed with Tailwind 4.3.3.
- Der einmalige Implementierungsreview fand neun Security-, Race-, Rollback- und
  Export-Randfälle. Alle Findings wurden behoben und durch Regressionstests ergänzt.
- Der lokale Docker-Image-Build konnte nicht ausgeführt werden, weil die Docker-Desktop-Engine
  nicht lief. Der identische PR-Image-Build ist als verpflichtender CI-Job konfiguriert.

## Browser Waiver

Agent Browser oder eine andere Browserautomation wurde entsprechend der ausdrücklichen
Nutzeranweisung nicht ausgeführt. Das verbleibende visuelle, responsive und clientseitige
Accessibility-Risiko ist akzeptiert und nicht als automatisierter CI-Gate dargestellt.
