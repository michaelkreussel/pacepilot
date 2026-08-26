# PacePilot Agent Notes

## Commands

- Use Python 3.12 and `uv`; install the locked environment with `uv sync --frozen`.
- During implementation, add appropriate unit tests and run focused pytest targets, for example `uv run pytest tests/test_routes.py::test_create_and_confirm_workout`.
- After all code changes are complete, run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run ty check`; fix failures before finishing. Do not repeatedly run the full format or type-check commands during implementation.
- For model or migration changes, run `uv run pytest tests/test_migrations.py`; it upgrades a fresh database and runs Alembic's schema check.
- After template or `app/static/css/tailwind.input.css` changes, rebuild committed CSS with `npm run build:css` (Tailwind 4.3.3 via the committed `package.json`; run `npm install` first if `node_modules` is missing), then bump the cache key in `base.html` and `login.html`.

## Workflow

- Fix known issues, add relevant unit tests, and run those tests while implementing.
- For a substantial change, perform one review after implementation, fix the findings, then run final verification. Do not create repeated review cycles. Small changes do not require a separate review pass.

## Runtime Wiring

- `app.main:app` contains FastAPI, Jinja, SQLite, Garmin sync, and APScheduler. Its lifespan creates directories, applies Alembic migrations, then starts the scheduler.
- Keep deployment at one Uvicorn worker. Garmin sync exclusion uses in-process per-account `threading.Lock` instances, and every worker would start its own scheduler. SQLite must remain on a local filesystem, not SMB/NFS.
- `app.config.get_settings()` is cached; `app.database` creates its engine and `app.auth` builds its OAuth registry at import time. Set environment overrides before importing the app. Route tests override dependencies rather than replacing the global engine.
- Protected routes use user-scoped signed sessions and `CurrentUser`. Every unsafe request requires the session CSRF token via `csrf_field()` or `X-CSRF-Token`.

## Data And Integrations

- Schema changes need both SQLAlchemy model updates and Alembic revisions. Alembic imports `app.models`, so export every new model from `app/models/__init__.py` or autogeneration/schema checks will miss it.
- Garmin tokens live under `GARMIN_TOKEN_DIR/account-<account_id>`, not in SQLite. Activity source and detail payloads are gzip JSON under `DATA_DIR/raw/activities/user-<user_id>/<year>/`; preserve database/file cleanup together.
- Garmin Connect is an unofficial external API. Tests must replace `connect_garmin` with a fake and direct raw files to `tmp_path`; do not require live credentials or network access.
- Preserve the workout boundary `draft -> confirmed -> published -> pushed`: unconfirmed or unvalidated content must not reach Garmin. The coach persists chats but does not generate or persist plans.

## Tests And UI

- Tests use an in-memory SQLite connection and override authenticated dependencies; use `unauthenticated_client` for real session/OAuth behavior.
- Settings are shared mutable state in tests because `get_settings()` is cached; restore values or clear/rebuild the cache when a new test changes them.
- User-facing UI copy is German. Jinja templates and committed CSS/JS are served directly from `app/templates/` and `app/static/`; HTMX and Alpine are loaded from CDNs.

## Agent Browser UI Verification

- Use Agent Browser only once, as a final validation pass after substantial UI changes when visual, responsive, interaction, or accessibility checks are warranted. Do not use it during implementation or for small CSS/HTML changes.
- Launch it only after the app is reachable at `http://127.0.0.1:8000/`. If needed, start the server only with `uv run uvicorn app.main:app --reload`; never use another host or port.
- Reuse the gitignored `pacepilot-auth.json` state with `agent-browser --state pacepilot-auth.json --session <task-name> open http://127.0.0.1:8000/`. Always use a task-specific named session and the saved state; do not attach to a live Chrome/CDP session.
- Confirm the state file exists, is non-empty, and provides an authenticated session. If not, ask the user to run `just get-session` and wait; never create, repair, or bypass session state yourself.
