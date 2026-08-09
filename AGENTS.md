# PacePilot Agent Notes

## Commands

- Use Python 3.12 and `uv`; install the locked environment with `uv sync`.
- Start locally with `uv run uvicorn app.main:app --reload`; app lifespan applies pending Alembic migrations before serving requests.
- Always use Ruff for code cleanup and linting, and `ty` for type checking. Run the full checks with `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run ty check`.
- Focus pytest with a node ID, for example `uv run pytest tests/test_routes.py::test_create_and_confirm_workout`.
- For model or migration changes, run `uv run pytest tests/test_migrations.py`; it upgrades a fresh database and runs Alembic's schema check.

## Runtime Wiring

- `app.main:app` is one process containing FastAPI, server-rendered Jinja pages, SQLite access, Garmin synchronization, and APScheduler. There is no separate frontend build or worker service.
- App lifespan creates data/token directories, applies Alembic migrations, and then starts the scheduler. Migration failure aborts startup before background jobs or requests can run.
- Keep deployment at one Uvicorn worker. Garmin sync exclusion uses in-process per-account `threading.Lock` instances, and every worker would start its own scheduler. SQLite must remain on a local filesystem, not SMB/NFS.
- `app.config.get_settings()` is cached, and `app.database` creates its engine at import time. Set database/environment overrides before importing the app; route tests override `get_db` instead of replacing the global engine.
- The app intentionally uses the first database user as a lazily-created local default user. It has no HTTP authentication, multi-user isolation, or CSRF protection.

## Data And Integrations

- Schema changes need both SQLAlchemy model updates and Alembic revisions. Alembic imports `app.models`, so export every new model from `app/models/__init__.py` or autogeneration/schema checks will miss it.
- Production schema creation belongs to Alembic. `Base.metadata.create_all()` is used only by the in-memory test fixture.
- Garmin tokens live under `GARMIN_TOKEN_DIR`, not in SQLite. Activity source payloads are gzip JSON files under `DATA_DIR/raw/activities/<year>/`; preserve the database-to-file relationship when changing sync behavior.
- Garmin Connect is an unofficial external API. Tests must replace `connect_garmin` with a fake and direct raw files to `tmp_path`; do not require live credentials or network access.
- Preserve the workout boundary `draft -> confirmed -> published -> pushed`: unconfirmed or unvalidated content must not reach Garmin. The coach UI is a placeholder and does not yet generate or persist plans.

## Tests And UI

- `tests/conftest.py` uses one in-memory SQLite connection via `StaticPool`, overrides FastAPI's database dependency, and disables the scheduler for `TestClient` lifespan.
- Settings are shared mutable state in tests because `get_settings()` is cached; restore values or clear/rebuild the cache when a new test changes them.
- User-facing UI copy is German. Templates and CSS are served directly from `app/templates/` and `app/static/`.
