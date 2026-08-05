# PacePilot

PacePilot ist ein kleiner, selbst gehosteter Trainingsbegleiter für Garmin Connect. FastAPI liefert API und serverseitige Jinja-Oberfläche aus; SQLite, Garmin-Sync und APScheduler laufen gemeinsam in einem Prozess.

## Aktueller Umfang

- Garmin-Anmeldung mit lokal gespeichertem Token, ohne Passwortspeicherung in SQLite
- Synchronisierung von Aktivitäten, Health-Tageswerten, Schlaf, HRV und Geräten
- komprimierte Garmin-Aktivitätsantworten unter `data/raw/activities/`
- Dashboard mit Erholungs- und Trainingsumfang-Trends
- Aktivitätenliste und Detailansicht
- Wochenkalender
- Workout-Editor für Lauf-, Rad-, Geh- und Wandereinheiten
- explizite Kette `Entwurf -> Bestätigung -> Garmin -> Uhr`
- regelmäßiger Garmin-Sync über APScheduler mit Prozess-Lock
- live aktualisierter Sync-Fortschritt und Laufzeitmessungen pro Garmin-Endpunkt
- Alembic-Migrationen, SQLite-WAL und Docker-Deployment mit einem Uvicorn-Worker

Der KI-Coach ist als Phase-5-Grenze in der Oberfläche sichtbar, generiert aber noch keine Pläne. Ein unvalidierter LLM-Text wird bewusst nicht als Workout gespeichert oder zu Garmin übertragen.

## Lokal starten

Benötigt werden `uv` und Python 3.12 oder neuer. `uv` kann die passende Python-Version selbst installieren.

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Danach ist PacePilot unter <http://localhost:8000> erreichbar. Beim ersten Seitenaufruf wird der lokale Standardnutzer erzeugt.

Die Konfiguration kann aus `.env` gelesen werden:

```bash
cp .env.example .env
```

Unter Windows kann die Beispieldatei einfach als `.env` dupliziert werden.

## Garmin verbinden

Unter `Einstellungen` erfolgt die erste Anmeldung. Das Passwort wird nur an Garmin Connect weitergereicht. Der resultierende Token wird unter `GARMIN_TOKEN_DIR` gespeichert.

Garmin Connect ist keine offizielle öffentliche API. MFA, Rate-Limits oder Änderungen auf Garmin-Seite können eine neue Anmeldung erforderlich machen. Bei MFA kann ein außerhalb der Weboberfläche erzeugter Token in `GARMIN_TOKEN_DIR` abgelegt werden. Ohne gültigen Token startet PacePilot weiterhin; nur Sync und Workout-Übertragung sind dann nicht verfügbar.

## Docker

```bash
docker compose up --build -d
```

Der Container führt beim Start `alembic upgrade head` aus und startet genau einen Uvicorn-Worker. Datenbank, Tokens und Rohdaten liegen im Volume `pacepilot-data` unter `/data`.

## Konfiguration

| Variable | Standard | Zweck |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLAlchemy-Verbindung |
| `DATA_DIR` | `./data` | Rohdaten und lokale Laufzeitdaten |
| `GARMIN_TOKEN_DIR` | `./data/garmin-tokens` | Garmin-Tokenablage |
| `SYNC_DAYS` | `14` | Zeitraum je Synchronisierung |
| `SYNC_INTERVAL_MINUTES` | `60` | APScheduler-Intervall |
| `SCHEDULER_ENABLED` | `true` | Hintergrundjobs aktivieren |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | leer | optionale unbeaufsichtigte Neuanmeldung |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | leer | reserviert für den KI-Coach |

SQLite muss auf einem lokalen Volume liegen, nicht auf SMB oder NFS. Mehrere Uvicorn-Worker sind wegen Scheduler und Prozess-Lock nicht unterstützt.

## Qualitätssicherung

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy app
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

PacePilot ist derzeit für einen lokalen Standardnutzer ausgelegt. Vor einer öffentlich erreichbaren Mehrnutzerinstallation fehlen noch HTTP-Authentifizierung, Nutzerverwaltung und CSRF-Schutz.
