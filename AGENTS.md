# PacePilot Agent Notes

## Commands

- Use Python 3.12 and `uv`; reproduce CI's locked environment with `uv sync --frozen`.
- Start locally with `uv run uvicorn app.main:app --reload`; app lifespan applies pending Alembic migrations before serving requests.
- Match CI with `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, then `uv run ty check`.
- Focus pytest with a node ID, for example `uv run pytest tests/test_routes.py::test_create_and_confirm_workout`.
- For model or migration changes, run `uv run pytest tests/test_migrations.py`; it upgrades a fresh database and runs Alembic's schema check.
- Templates use Tailwind utilities, but the standalone Tailwind CLI is not a project dependency. After changing templates or `app/static/css/tailwind.input.css`, rebuild the committed output with `tailwindcss -i ./app/static/css/tailwind.input.css -o ./app/static/css/tailwind.css --minify` (currently Tailwind 4.3.3), then bump the stylesheet cache key in `base.html` and `login.html`.

## Runtime Wiring

- `app.main:app` is one process containing FastAPI, server-rendered Jinja pages, SQLite access, Garmin synchronization, and APScheduler. There is no separate frontend build or worker service.
- App lifespan creates data/token directories, applies Alembic migrations, and then starts the scheduler. Migration failure aborts startup before background jobs or requests can run.
- Keep deployment at one Uvicorn worker. Garmin sync exclusion uses in-process per-account `threading.Lock` instances, and every worker would start its own scheduler. SQLite must remain on a local filesystem, not SMB/NFS.
- `app.config.get_settings()` is cached; `app.database` creates its engine and `app.auth` builds its OAuth registry at import time. Set environment overrides before importing the app. Route tests override dependencies rather than replacing the global engine.
- Google OpenID Connect and GitHub OAuth create user-scoped signed sessions; protected routes use `CurrentUser`, and incomplete accounts are redirected through onboarding. There is no CSRF protection on state-changing forms.

## Data And Integrations

- Schema changes need both SQLAlchemy model updates and Alembic revisions. Alembic imports `app.models`, so export every new model from `app/models/__init__.py` or autogeneration/schema checks will miss it.
- Production schema creation belongs to Alembic. `Base.metadata.create_all()` is used only by the in-memory test fixture.
- Garmin tokens live under `GARMIN_TOKEN_DIR/account-<account_id>`, not in SQLite. Activity source and detail payloads are gzip JSON under `DATA_DIR/raw/activities/user-<user_id>/<year>/`; preserve database/file cleanup together.
- Garmin Connect is an unofficial external API. Tests must replace `connect_garmin` with a fake and direct raw files to `tmp_path`; do not require live credentials or network access.
- Preserve the workout boundary `draft -> confirmed -> published -> pushed`: unconfirmed or unvalidated content must not reach Garmin. The coach persists chats but does not generate or persist plans.

## Tests And UI

- `tests/conftest.py` uses one in-memory SQLite connection via `StaticPool`, overrides both `get_db` and `get_current_user`, and disables migrations and the scheduler for authenticated `TestClient` tests. Use `unauthenticated_client` for real session/OAuth behavior.
- Settings are shared mutable state in tests because `get_settings()` is cached; restore values or clear/rebuild the cache when a new test changes them.
- User-facing UI copy is German. Jinja templates and committed CSS/JS are served directly from `app/templates/` and `app/static/`; HTMX and Alpine are loaded from CDNs.
- For UI changes, verify authenticated pages at desktop and mobile widths and run an accessibility check when `agent-browser` is available. Do not automate or expose Google/GitHub credentials; use a locally authenticated application session.
