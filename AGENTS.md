# PacePilot Agent Instructions

## Engineering Workflow

Before starting a non-trivial change, check whether an installed agent skill
applies.

For AI Coach refactoring, read both documents and keep their roles distinct:

- `docs/refactoring/ai-coach-current-state.md` maps the current implementation.
- `docs/refactoring/ai-coach-intent.md` describes the desired outcome, not the
  current architecture.

For substantial changes, prefer:

1. Understand the relevant existing context.
2. Clarify or read the specification.
3. Create a small implementation plan when necessary.
4. Implement one scoped change.
5. Add or update relevant tests.
6. Review the resulting diff once.
7. Run final verification.
8. In the completion report, briefly explain in plain language what the task changed.

- Do not perform unrelated cleanup.
- Keep behavior-preserving refactoring separate from intentional behavior
  changes.
- Prefer deleting or simplifying code over introducing new abstractions unless
  the abstraction clearly reduces complexity.
- Keep changes small enough that they can be understood, tested, reviewed, and
  reverted independently.

## Commands

Use Python 3.12 and `uv`.

### Install Dependencies

```bash
uv sync --frozen
```

### Focused Verification

During implementation, run focused tests relevant to the changed behavior. For
example:

```bash
uv run pytest tests/test_routes.py::test_create_and_confirm_workout
```

### Final Verification

After all code changes for a task are complete, run:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Do not repeatedly run the complete test, formatting, linting, or type-check
suite during implementation.

For model or migration changes also run:

```bash
uv run pytest tests/test_migrations.py
```

For changes to templates or `app/static/css/tailwind.input.css`, rebuild CSS:

```bash
npm run build:css
```

Then update the static cache key where required.

## Architecture Invariants

- `app.main:app` contains FastAPI, Jinja, SQLite, Garmin sync, and APScheduler.
- Deployment uses exactly one Uvicorn worker.
- SQLite must remain on a local filesystem, not SMB/NFS.
- Garmin sync exclusion currently relies on in-process per-account locks.
- `app.config.get_settings()` is cached.
- `app.database` creates its engine and `app.auth` builds its OAuth registry at
  import time. Environment overrides must therefore be configured before app
  imports where relevant.

## Data And Integrations

- Schema changes require:
  - SQLAlchemy model changes
  - an Alembic revision
- Alembic imports `app.models`. Export new models from
  `app/models/__init__.py`.
- Garmin tokens are stored outside SQLite under
  `GARMIN_TOKEN_DIR/account-<account_id>`.
- Raw activity data is stored under
  `DATA_DIR/raw/activities/user-<user_id>/<year>/`.
- Preserve database and filesystem cleanup together.
- Garmin Connect is an unofficial external API. Automated tests must not
  require live Garmin credentials or network access.

## Security And Domain Invariants

- Protected routes use user-scoped signed sessions and `CurrentUser`.
- Unsafe requests require CSRF protection through `csrf_field()` or
  `X-CSRF-Token`.
- Preserve the workout lifecycle:

  ```text
  draft -> confirmed -> published -> pushed
  ```

- Unconfirmed or invalid content must never reach Garmin.

## Testing

- Prefer tests at stable behavioral boundaries over tests of private
  implementation details.
- Tests use in-memory SQLite and authenticated dependency overrides where
  appropriate.
- `get_settings()` is cached mutable state during tests. Restore modified values
  or rebuild the cache when tests change settings.

## UI

- User-facing UI copy is German.
- Templates and committed frontend assets live under:
  - `app/templates/`
  - `app/static/`
- HTMX and Alpine are loaded from CDNs.
- Visual browser verification is not required by default. Use it only when a
  task explicitly requires interactive or visual validation.
