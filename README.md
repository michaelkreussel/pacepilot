# PacePilot

PacePilot ist ein kleiner, selbst gehosteter Trainingsbegleiter für Garmin Connect. FastAPI liefert API und serverseitige Jinja-Oberfläche aus; SQLite, Garmin-Sync und APScheduler laufen gemeinsam in einem Prozess.

## Aktueller Umfang

- Garmin-Anmeldung mit kontobezogen gespeichertem Token, ohne Passwortspeicherung in SQLite
- Anmeldung über Google OpenID Connect oder GitHub OAuth2 mit signierter Session
- fortsetzbare historische Synchronisierung von Health-Tageswerten, Schlaf, HRV und Fitnesswerten
- vollständiger, inkrementeller Aktivitätsverlauf mit Runden, Zonen und Krafttrainingssätzen
- deterministische Gesundheits-, Readiness- und sportartspezifische Trainingsanalysen
- Athletenprofil mit persönlichen Health-, Schlaf- und Trainingstrends
- komprimierte Garmin-Aktivitätsantworten unter `data/raw/activities/`
- Dashboard mit Erholungs- und Trainingsumfang-Trends
- Aktivitätenliste und Detailansicht
- Wochenkalender
- Workout-Editor für Lauf-, Rad-, Geh- und Wandereinheiten
- explizite Kette `Entwurf -> Bestätigung -> Garmin -> Uhr`
- regelmäßiger Garmin-Sync über APScheduler mit Prozess-Lock
- live aktualisierter Sync-Fortschritt und Laufzeitmessungen pro Garmin-Endpunkt
- zustandsloser KI-Coach mit Agno und OpenRouter auf einem begrenzten Trainings-Snapshot
- Alembic-Migrationen, SQLite-WAL und Docker-Deployment mit einem Uvicorn-Worker

Der KI-Coach beantwortet Planungsfragen anhand der letzten Aktivitäten, Erholungswerte, Wochenlast und anstehenden Workouts. Der Agent arbeitet ausschließlich lesend und ohne Chat-Speicher. Seine Antwort bleibt Text: Sie wird weder als Workout gespeichert noch bestätigt oder zu Garmin übertragen.

## Lokal starten

Benötigt werden `uv` und Python 3.12 oder neuer. `uv` kann die passende Python-Version selbst installieren.

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Danach ist PacePilot unter <http://localhost:8000> erreichbar. Vor dem ersten Login muss mindestens ein Anmeldeanbieter konfiguriert sein.

Die Konfiguration kann aus `.env` gelesen werden:

```bash
cp .env.example .env
```

Unter Windows kann die Beispieldatei einfach als `.env` dupliziert werden.

## Anmeldung konfigurieren

PacePilot verwendet [Authlib](https://authlib.org/) für OAuth2 und OpenID Connect. Google wird über OpenID Connect Discovery angebunden. GitHub stellt für Benutzer-Logins kein OpenID Connect bereit und wird deshalb über den OAuth2-Flow sowie die verifizierte GitHub-E-Mail-API angebunden.

1. Erzeuge `SESSION_SECRET` mit mindestens 32 zufälligen Bytes, zum Beispiel mit `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Lege bei Google und/oder GitHub eine OAuth-Anwendung an.
3. Trage Client-ID und Client-Secret in `.env` ein.
4. Hinterlege beim Provider die passende Callback-URL.

Lokale Callback-URLs:

```text
http://localhost:8000/auth/google/callback
http://localhost:8000/auth/github/callback
```

Für eine öffentliche Installation müssen die Callback-URLs HTTPS verwenden und `SESSION_HTTPS_ONLY=true` gesetzt sein. Client-Secrets und `SESSION_SECRET` dürfen nicht in Git eingecheckt werden.

## Garmin verbinden

Unter `Einstellungen` erfolgt die erste Anmeldung. Bei aktivierter MFA fragt PacePilot anschließend den von Garmin gesendeten oder in der Authenticator-App erzeugten Bestätigungscode ab. Passwort und Bestätigungscode werden nur an Garmin Connect weitergereicht. Der resultierende Token wird unter `GARMIN_TOKEN_DIR/account-<id>` gespeichert.

Garmin Connect ist keine offizielle öffentliche API. MFA, Rate-Limits oder Änderungen auf Garmin-Seite können eine neue Anmeldung erforderlich machen. Garmin-Tokens liegen kontobezogen unter `GARMIN_TOKEN_DIR/account-<account id>/`. Ohne gültigen Token startet PacePilot weiterhin; nur Sync und Workout-Übertragung sind dann nicht verfügbar.

Synchronisierungen laufen als kontobezogene Hintergrundjobs. Verschiedene Konten können bis zum konfigurierten `GARMIN_SYNC_WORKERS`-Limit parallel arbeiten; ein zweiter Lauf für dasselbe Konto wird ausgeschlossen, damit Sitzung, Token und Datenbank-Cursor nicht konkurrierend verändert werden. Garmin-Abfragen aller Jobs teilen sich zusätzlich einen prozessweiten Mindestabstand. Bei einem HTTP-429 pausiert PacePilot weitere Läufe standardmäßig fünf Minuten und setzt danach über die gespeicherten Cursor fort.

Beim ersten Lauf werden Aktivitätsübersichten mit wenigen paginierten Anfragen gespeichert. Diagramme, Runden, Zonen und Kraftsätze werden anschließend in begrenzten Paketen ergänzt. Aktuelle Health-Tage werden vor dem älteren Verlauf geladen, sodass Dashboard und Trends früh nutzbar werden, während der historische Import im Hintergrund weiterläuft.

## Docker

```bash
docker compose up --build -d
```

PacePilot führt ausstehende Alembic-Migrationen bei jedem Programmstart vor dem Scheduler automatisch aus. Der Container startet genau einen Uvicorn-Worker. Datenbank, Tokens und Rohdaten liegen im Volume `pacepilot-data` unter `/data`.

## Konfiguration

| Variable | Standard | Zweck |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` erzwingt sichere Session-Cookies und deaktiviert OpenAPI-Dokumentation |
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLAlchemy-Verbindung |
| `DATA_DIR` | `./data` | Rohdaten und lokale Laufzeitdaten |
| `GARMIN_TOKEN_DIR` | `./data/garmin-tokens` | Garmin-Tokenablage |
| `HEALTH_SYNC_OVERLAP_DAYS` | `7` | erneut geladene aktuelle Health-Tage |
| `SYNC_INTERVAL_MINUTES` | `60` | APScheduler-Intervall |
| `GARMIN_SYNC_WORKERS` | `2` | Maximale Anzahl gleichzeitig synchronisierter Garmin-Konten |
| `GARMIN_CALL_DELAY_SECONDS` | `0.75` | Mindestabstand historischer Garmin-Abfragen |
| `GARMIN_ACTIVITY_INITIAL_ENRICHMENT` | `0` | Detailaktivitäten, die schon im ersten Lauf vollständig geladen werden |
| `GARMIN_ACTIVITY_ENRICHMENT_PER_SYNC` | `5` | Maximal vollständig ergänzte Altaktivitäten je Folgelauf |
| `GARMIN_RATE_LIMIT_COOLDOWN_SECONDS` | `300` | Pause nach einem Garmin-HTTP-429 |
| `SCHEDULER_ENABLED` | `true` | Hintergrundjobs aktivieren |
| `SESSION_SECRET` | Development-Fallback ohne Login | Signatur der Session-Cookies; mit aktiviertem Login zwingend setzen |
| `SESSION_HTTPS_ONLY` | `false` | Session-Cookie ausschließlich über HTTPS senden |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | leer | Google-OpenID-Connect-Anwendung |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | leer | GitHub-OAuth2-Anwendung |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | leer | optionale unbeaufsichtigte Neuanmeldung |
| `LLM_API_KEY` | leer | OpenRouter API-Key für den KI-Coach |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API-Basis-URL |
| `LLM_MODEL` | leer | OpenRouter Modell-ID, zum Beispiel `openai/gpt-4o-mini` |

SQLite muss auf einem lokalen Volume liegen, nicht auf SMB oder NFS. Mehrere Uvicorn-Worker sind wegen Scheduler und prozesslokaler Kontosperren nicht unterstützt.
Für eine öffentliche HTTPS-Installation müssen `ENVIRONMENT=production`, `SESSION_SECRET` und `SESSION_HTTPS_ONLY=true` gesetzt sein.

## Qualitätssicherung

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

## Projektstruktur

```text
app/
├── models/          SQLAlchemy-Modelle
├── repositories/    Datenbankabfragen
├── services/        Garmin, Planung und Analytics
├── routes/          FastAPI- und HTML-Routen
├── jobs/            APScheduler
├── templates/       Jinja2-Oberfläche
└── static/          CSS
migrations/          Alembic-Schemaänderungen
tests/               Integrations- und Unit-Tests
```

PacePilot trennt Anwendungsdaten anhand des angemeldeten Nutzers. Für eine öffentlich erreichbare Installation bleiben administrative Nutzerverwaltung und zusätzliche CSRF-Härtung für zustandsändernde Formulare wichtige Betriebsaufgaben.
