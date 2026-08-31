# PacePilot

[![CI](https://github.com/michaelkreussel/pacepilot/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/michaelkreussel/pacepilot/actions/workflows/docker-publish.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

PacePilot is a self-hosted training companion for Garmin Connect. It imports health and activity
history, turns personal data into recovery and training insights, manages structured workouts, and
can send confirmed workouts to Garmin devices.

The application is designed as a small single-process deployment: FastAPI serves the API and the
server-rendered interface while SQLite, Garmin synchronization, and APScheduler run in the same
process. The user interface is currently available in German.

> [!IMPORTANT]
> PacePilot is an early-stage project and is not a medical service. Garmin Connect access relies on
> an unofficial third-party API and may be affected by Garmin authentication, rate limits, or API
> changes.

## Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Authentication](#authentication)
- [Connecting Garmin](#connecting-garmin)
- [Configuration](#configuration)
- [Docker Deployment](#docker-deployment)
- [Architecture](#architecture)
- [Data and Privacy](#data-and-privacy)
- [Security Considerations](#security-considerations)
- [Development](#development)
- [Project Structure](#project-structure)
- [Documentation](#documentation)
- [Known Limitations](#known-limitations)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Recovery dashboard** with Garmin Training Readiness when available, a deterministic PacePilot
  fallback, health signals, the next workout, the latest activity, and a seven-day training summary
- **Resumable Garmin synchronization** for health, sleep, HRV, Body Battery, activities, laps,
  zones, strength sets, GPS samples, and device-dependent performance metrics
- **Athlete analysis** across daily, 7-day, 28-day, 12-week, and yearly ranges, including personal
  baselines, training load, training effect, sleep, recovery, and performance trends
- **Activity history** with filters, pagination, splits, sensor charts, zones, strength sets, and
  route maps when detailed data is available
- **Training calendar** with week and month views
- **Structured workout editor** for running, cycling, walking, and hiking, including repeats and
  pace or heart-rate targets
- **Guarded Garmin publishing flow**: `draft -> confirmed -> published -> pushed`
- **Optional AI coach** powered by LangChain and OpenRouter, with streamed responses, persistent
  conversations, and read-only access to bounded athlete data
- **Multi-user authentication** through Google OpenID Connect and/or GitHub OAuth2
- **Operational visibility** through live synchronization progress, endpoint timings, JSON sync-run
  exports, rotating logs, and an unauthenticated `/api/health` endpoint

## Tech Stack

- Python 3.12
- FastAPI and Uvicorn
- Jinja2, HTMX, Alpine.js, and Tailwind CSS
- SQLAlchemy, SQLite, and Alembic
- APScheduler
- `garminconnect`
- LangChain and OpenRouter for the optional coach
- `uv` for dependency management

## Quick Start

### Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A Google OAuth or GitHub OAuth application
- A Garmin Connect account for data synchronization

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/michaelkreussel/pacepilot.git
   cd pacepilot
   ```

2. Create a local environment file:

   ```bash
   cp .env.example .env
   ```

   On Windows PowerShell, use `Copy-Item .env.example .env` instead.

3. Generate a session secret and add it to `.env` as `SESSION_SECRET`:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. Add credentials for at least one login provider to `.env`. See
   [Authentication](#authentication) for the required callback URLs.

5. Install the locked dependencies and start the application:

   ```bash
   uv sync --frozen
   uv run uvicorn app.main:app --reload
   ```

6. Open <http://localhost:8000>.

PacePilot creates its data directories and applies pending Alembic migrations during startup. The
checked-in Tailwind stylesheet is ready to use, so no frontend build is required for a normal
application start.

## Authentication

PacePilot has no local username and password login. Configure Google, GitHub, or both through the
corresponding variables in `.env`.

Register these local callback URLs with the providers you enable:

```text
http://localhost:8000/auth/google/callback
http://localhost:8000/auth/github/callback
```

Google uses OpenID Connect discovery. GitHub uses OAuth2 and the verified email API because GitHub
does not provide OpenID Connect for user login.

For a public deployment:

- use HTTPS;
- set `ENVIRONMENT=production`;
- set `SESSION_HTTPS_ONLY=true`;
- set `PUBLIC_BASE_URL` to the externally visible HTTPS origin; and
- update the provider callback URLs to use that origin.

Provider client secrets and `SESSION_SECRET` must never be committed to the repository.

## Connecting Garmin

After signing in, connect Garmin from the **Settings** page (`Einstellungen` in the German
interface). PacePilot supports Garmin MFA and forwards the password and verification code directly
to Garmin Connect. It does not store the Garmin password in SQLite. The resulting token files are
stored per account under `GARMIN_TOKEN_DIR/account-<account-id>/`.

The initial synchronization stores activity summaries first and then enriches activity details in
bounded batches. Recent health days are processed before older history so the dashboard becomes
useful while the resumable historical import continues in the background.

Synchronization jobs can process different accounts concurrently up to `GARMIN_SYNC_WORKERS`, but
PacePilot prevents overlapping jobs for the same account. All Garmin calls share a process-wide
minimum delay. After an HTTP 429 response, synchronization pauses for the configured cooldown and
continues later from persisted cursors.

Device-dependent metrics are synchronized independently. Unsupported or empty resources do not
block other metrics and are periodically probed again, which allows PacePilot to discover data from
a newly connected Garmin device.

## Configuration

Settings are read from environment variables and, for local development, from `.env`.

| Variable | Default | Description |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | Runtime mode: `development`, `test`, or `production` |
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLAlchemy database URL |
| `DATA_DIR` | `./data` | Runtime files, logs, and raw Garmin payloads |
| `GARMIN_TOKEN_DIR` | `./data/garmin-tokens` | Per-account Garmin token storage |
| `HEALTH_SYNC_OVERLAP_DAYS` | `7` | Recent health days refreshed by each sync |
| `SYNC_INTERVAL_MINUTES` | `60` | APScheduler synchronization interval |
| `GARMIN_SYNC_WORKERS` | `2` | Maximum number of accounts synchronized concurrently |
| `GARMIN_CALL_DELAY_SECONDS` | `0.75` | Minimum delay between historical Garmin calls |
| `GARMIN_ACTIVITY_INITIAL_ENRICHMENT` | `0` | Activity details enriched during the first sync |
| `GARMIN_ACTIVITY_ENRICHMENT_PER_SYNC` | `5` | Older activity details enriched per subsequent sync |
| `GARMIN_RATE_LIMIT_COOLDOWN_SECONDS` | `300` | Global pause after a Garmin HTTP 429 response |
| `GARMIN_OPERATION_STALE_MINUTES` | `15` | Marks abandoned pending Garmin attempts as unknown |
| `SCHEDULER_ENABLED` | `true` | Enables periodic background synchronization |
| `LOG_LEVEL` | `INFO` | Console and rotating file log level |
| `MUTATION_RATE_LIMIT_PER_MINUTE` | `120` | Per-user limit for general unsafe requests |
| `COACH_RATE_LIMIT_PER_MINUTE` | `12` | Per-user limit for Coach mutations |
| `AUTH_RATE_LIMIT_PER_MINUTE` | `20` | Per-client limit for OAuth and Garmin authentication |
| `ACCOUNT_EXPORT_RATE_LIMIT_PER_MINUTE` | `2` | Per-user limit for complete ZIP exports |
| `METRICS_BEARER_TOKEN` | unset | Optional 32+ character token for privacy-safe `/api/metrics` |
| `SESSION_SECRET` | unset | Secret used to sign sessions; minimum 32 characters |
| `SESSION_HTTPS_ONLY` | `false` | Sends session cookies only over HTTPS |
| `PUBLIC_BASE_URL` | unset | Public application origin used for OAuth redirects |
| `GOOGLE_CLIENT_ID` | unset | Google OpenID Connect client ID |
| `GOOGLE_CLIENT_SECRET` | unset | Google OpenID Connect client secret |
| `GITHUB_CLIENT_ID` | unset | GitHub OAuth2 client ID |
| `GITHUB_CLIENT_SECRET` | unset | GitHub OAuth2 client secret |
| `GARMIN_EMAIL` | unset | Optional credential for unattended Garmin reauthentication |
| `GARMIN_PASSWORD` | unset | Optional credential for unattended Garmin reauthentication |
| `LLM_API_KEY` | unset | OpenRouter API key for the optional coach |
| `LLM_MODEL` | `z-ai/glm-5.3-flash` | OpenRouter model ID; Coach calls are routed only through Z.AI |
| `LLM_TIMEOUT_SECONDS` | `60` | Timeout for an OpenRouter model call |
| `COACH_WORKOUT_PROPOSALS_ENABLED` | `false` | Enables future local coach workout proposals |
| `COACH_GARMIN_SYNC_ENABLED` | `false` | Enables future Garmin sync for accepted coach workouts |
| `COACH_DAILY_ADAPTATION_ENABLED` | `false` | Enables future coach daily adaptations |
| `COACH_PLAN_GENERATION_ENABLED` | `false` | Enables future coach week and multi-week plans |
| `COACH_PLANNER_HISTORY_GATES_ENABLED` | `true` | Enforces observed week/frequency eligibility; development can disable it for planner testing |
| `COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED` | `false` | Development-only override for testing deferred threshold and VO2max templates |
| `COACH_ROLLOUT_USER_IDS` | unset | Optional comma-separated internal cohort; malformed values fail closed |

Production mode requires `SESSION_SECRET` and `SESSION_HTTPS_ONLY=true`. Configuring only one half
of an OAuth provider's client ID and secret pair also prevents startup.

## Docker Deployment

Create `.env`, configure `SESSION_SECRET` and at least one OAuth provider, then run:

```bash
docker compose up --build -d
```

The Compose setup:

- exposes the application on `${PORT:-8000}`;
- bind-mounts `${DATA_DIR:-./data}` from the host to `/data` in the container;
- stores SQLite, logs, Garmin tokens, and raw activity payloads under `/data`;
- applies pending migrations before serving requests; and
- starts exactly one Uvicorn worker.

The included Compose file forwards deployment, OAuth, LLM, and coach feature settings. Coach
feature flags never disable the existing manual workout or Garmin flows. Add other tuning
variables to a Compose override if their defaults need to change in the container.

For internet-facing installations, place PacePilot behind an HTTPS reverse proxy and configure
`PUBLIC_BASE_URL`. A reverse proxy and TLS termination are not included.

## Architecture

PacePilot intentionally runs as one process. There is no separate frontend build service, API
service, job worker, or external database requirement.

```text
Browser
   |
FastAPI + Jinja2 + HTMX
   |-- SQLAlchemy -> SQLite
   |-- APScheduler -> Garmin synchronization
   |-- Garmin Connect -> health, activities, and workouts
   `-- OpenRouter -> optional AI coach
```

At startup, the application creates data directories, configures rotating logs, applies Alembic
migrations, and starts APScheduler. Migration failure aborts startup before requests or jobs run.

SQLite uses WAL mode and must reside on a local filesystem, not SMB or NFS. Multiple Uvicorn workers
are unsupported because Garmin account locks and the scheduler are process-local.

## Data and Privacy

PacePilot stores sensitive personal data. Protect the host, backups, `.env`, database, token
directory, and raw-data directory accordingly.

- OAuth identity metadata, Garmin account metadata, health history, activities, workouts, coach
  conversations, and coach tool records are stored in SQLite.
- Garmin tokens are stored as local files outside SQLite.
- Compressed raw activity payloads are stored under
  `DATA_DIR/raw/activities/user-<user-id>/<year>/` and may contain GPS routes.
- Application logs are written to the console and `DATA_DIR/logs/pacepilot.log`. Coach logs contain
  identifiers and tool names, but not question text, answer text, or health values.
- Enabling the coach sends prompts, bounded conversation history, and selected athlete data to
  OpenRouter. If proposals are enabled, its single bounded mutation tool can create only an
  unaccepted and unscheduled server-side proposal; it cannot accept, schedule, publish, or push it.
- The web interface loads some assets from third-party CDNs. Activity maps request OpenStreetMap
  tiles, which exposes the viewed map area to the tile provider.
- Disconnecting Garmin removes token files but retains imported data. The separate Garmin-data
  deletion action removes imported Garmin rows and raw files, but retains the application account,
  local workouts, and coach chats.
- The complete ZIP export contains all user-scoped database rows and raw activity files, but never
  token files, session secrets, shared logs, or host backups. Local account deletion removes the
  database account, local Garmin tokens and raw files; data and workouts already held by Garmin or
  external backups remain outside PacePilot's deletion boundary.

## Security Considerations

PacePilot is primarily intended for trusted self-hosted environments. Before exposing it publicly,
consider these current limitations:

- Any valid identity from a configured OAuth provider can create an account; there is no built-in
  allowlist, invitation flow, or administrative approval.
- Identities from different OAuth providers are not automatically linked by email address.
- Requests using unsafe HTTP methods require a synchronizer token bound to the signed session;
  OAuth login initiation uses a protected POST.
- Dynamic responses use `no-store`, standard browser security headers, per-user/client rate limits,
  and stale Garmin-operation detection. A minimal CSP protects framing, forms, objects, and base URLs;
  the remaining third-party assets prevent a strict `default-src 'self'` policy for now.
- Application data is not encrypted at rest by PacePilot.

## Development

Install the locked development environment:

```bash
uv sync --frozen
```

Run the same checks as CI:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

For schema changes, update both the SQLAlchemy models and Alembic revisions, then run:

```bash
uv run pytest tests/test_migrations.py
```

### Agent Browser Session

Google may reject automated Chromium login attempts. To create a persistent authenticated
agent-browser session for local development, drive real Chrome through a dedicated automation
profile and a CDP debugging port:

```bash
just get-session
```

`get-session` opens Chrome with a dedicated profile, waits until you have signed in with Google,
and saves the signed-in state to `pacepilot-auth.json`. Reuse it with:

```bash
agent-browser --state pacepilot-auth.json open http://127.0.0.1:8000/
```

Other recipes: `just open-browser`, `just wait-login`, `just save-state`, and
`just check`. The `pacepilot-auth.json` state file is gitignored.

### Tailwind CSS

The generated `app/static/css/tailwind.css` file is committed. Tailwind 4.3.3 is pinned as a Node
dev dependency in the committed `package.json` (only `node_modules` is gitignored). After changing
templates or `app/static/css/tailwind.input.css`, rebuild it with:

```bash
npm install
npm run build:css
```

Then update the stylesheet cache key in `app/templates/base.html` and
`app/templates/login.html`.

### Maintenance Scripts

```bash
# Fully enrich activity history for connected accounts
uv run python scripts/backfill_garmin_activities.py

# Backfill health and recovery history
uv run python scripts/backfill_garmin_health.py

# Audit Garmin data availability without persisting personal metric values
uv run python scripts/audit_garmin_history.py
```

Use `--help` to inspect each script's account, date, pacing, and page-size options.

## Project Structure

```text
app/
|-- jobs/            APScheduler integration
|-- models/          SQLAlchemy models
|-- repositories/    Database queries and persistence
|-- routes/           HTML, authentication, settings, and API routes
|-- services/         Garmin, analytics, planning, and coach logic
|-- static/           Generated CSS, JavaScript, and icons
`-- templates/        Server-rendered Jinja2 interface
docs/                 Data research and architecture documentation
migrations/           Alembic migration environment and revisions
scripts/              Backfill and audit utilities
tests/                Unit, integration, route, and migration tests
compose.yaml          Local Docker Compose deployment
Dockerfile            Single-worker production image
pyproject.toml        Python project and tool configuration
uv.lock               Locked dependency graph
```

## Documentation

- [Activity backfill](docs/activity-backfill.md)
- [Health backfill](docs/health-backfill.md)
- [Garmin data inventory](docs/garmin-data-inventory.md)
- [Athlete history architecture](docs/athlete-history-architecture.md)
- [Athlete profile](docs/athlete-profile.md)
- [Athlete trends](docs/athlete-trends.md)

Some documents capture the investigation or design state at a specific point in development. The
application code, migrations, and this README are authoritative for current runtime behavior.

## Known Limitations

- Garmin Connect is not an official public API. Authentication flows, schemas, rate limits, and
  device-dependent metric availability can change without notice.
- Detailed activity data is enriched incrementally, so maps, charts, splits, or strength sets may
  not appear immediately after the initial import.
- The workout editor does not support nested repeat groups. Pace targets are limited to running.
- The AI coach requires an external OpenRouter account and is intended for informational use only.
- Only a single Uvicorn worker and a locally stored SQLite database are supported.
- The application interface is currently German-only.

## Contributing

Contributions are welcome. Keep changes focused, add or update tests for behavioral changes, and
run the full [development checks](#development) before opening a pull request. For substantial
features or architecture changes, open an issue first to align on scope and approach.

## License

PacePilot is licensed under the [GNU General Public License v3](LICENSE). Third-party icon
attribution is available in [`LICENSES/lucide.txt`](LICENSES/lucide.txt).
