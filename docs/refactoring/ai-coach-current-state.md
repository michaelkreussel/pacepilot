# AI Coach Current-State Context

## Purpose And Authority

This is a navigational map of the implementation inspected on 26 August 2026. Use it before
changing the AI Coach or its direct dependencies. It is intentionally not a file-by-file catalog.

`docs/refactoring/ai-coach-intent.md` describes the desired refactoring outcome, not the current
architecture. For current behavior, prefer application code and migrations, then behavioral tests,
then this document. The phase plans and research records are useful history, not runtime truth.

## Application Shape

PacePilot is a Python 3.12, single-process FastAPI application. FastAPI/Jinja serves the German UI;
SQLAlchemy uses local SQLite; APScheduler and a thread pool run Garmin synchronization in the same
process. The optional Coach uses LangChain and OpenRouter. There is no separate API service, worker,
frontend application, or external database.

Important project areas:

| Area | Responsibility |
| --- | --- |
| `app/main.py` | Lifespan, middleware, static mount, and router registration |
| `app/config.py`, `app/database.py`, `app/auth.py` | Cached settings, import-time engine/session factory, import-time OAuth registry |
| `app/routes/` | Authenticated HTML and mutation boundaries; Coach, plans, workouts, and feedback are separate route modules |
| `app/models/`, `app/repositories/` | SQLAlchemy persistence and user-scoped query primitives |
| `app/services/coach/` | OpenRouter agent, event translation, trusted runtime context, and LLM-visible tools |
| `app/services/analytics/` | Deterministic athlete, recovery, training, and feedback read models |
| `app/services/planning/` | Workout definitions, validation, proposals, revisions, adaptation, and weekly/multiweek planning |
| `app/services/garmin/` | Garmin client, sync/backfill, account locks, workout export, and durable operations |
| `app/jobs/scheduler.py` | Periodic Garmin data sync and interrupted/stale operation repair; not Coach planning |
| `knowledge/` | Versioned YAML workout templates, constraints, and evidence references |
| `app/templates/`, `app/static/` | Server-rendered UI, committed Tailwind output, and browser JavaScript |
| `migrations/`, `tests/` | Linear Alembic history and behavioral/contract/migration coverage |

## Current Coach Boundary

The conversational Coach is not the whole planning system.

- `app/routes/coach.py` owns conversation pages and CRUD, SSE orchestration, tool-event
  persistence, proposal-card projection, direct running-proposal forms, and a weekly planning
  preview. This is a broad controller boundary.
- `app/services/coach/dependencies.py` constructs a `LangChainCoachAgent` per dependency resolution
  only when both `LLM_API_KEY` and `LLM_MODEL` are configured. The proposal tool is registered only
  when its feature flag and optional rollout allowlist permit it for the current user.
- `app/services/coach/agent.py` owns the German system prompt, OpenRouter configuration, model/tool
  call limits, and translation of LangChain/LangGraph output into internal `CoachEvent` values.
- `app/services/coach/tools.py` injects trusted `user_id`, date, session factory, request ID, and
  origin IDs outside the model-visible schemas. Eight tools read through `AthleteDataService`.
- The only LLM-visible mutation is `create_running_workout_proposal`. It delegates date, available
  time, and a template ID to deterministic planning services. It cannot author workout steps.
- The LLM has no tool to edit, accept, reject, schedule, adapt, publish, push, synchronize, or delete
  workouts and no tool to generate weekly or multiweek plans. Those capabilities exist in separate
  structured routes and services.
- `app/models/coach.py` and `app/repositories/coach.py` persist conversations, messages, assistant
  runs, and safe tool-call telemetry. Repository functions do not own transactions.
- `app/templates/coach.html`, `app/templates/workouts/_coach_proposal_card.html`, and
  `app/static/js/coach.js` form the chat UI. Streaming messages use `fetch` and a custom SSE parser,
  not HTMX.

The docstring in `app/services/coach/__init__.py` still calls the package read-only; that is stale
when the proposal tool is enabled.

## Data Flows

### Conversation And Streaming

```text
authenticated, onboarded user
  -> app/routes/coach.py
  -> user-scoped conversation lookup
  -> commit user message + empty streaming assistant message + assistant run
  -> bounded completed history (20 messages / 12,000 characters)
  -> LangChainCoachAgent -> OpenRouter
  -> AthleteDataService read tools and optional deterministic proposal tool
  -> SSE status/tool/proposal/answer events -> app/static/js/coach.js
  -> persist final assistant prose and completed status
```

- The request is committed before the provider call. Each tool event uses its own session and
  commit. Final assistant prose is accumulated in memory and persisted only after successful model
  completion.
- Failed provider runs become `failed`; disconnects become `interrupted`. Partial streamed prose is
  not retained. A workout proposal committed before either failure remains durable.
- Later model history contains only completed user/assistant prose. It excludes failed/interrupted
  answers, tool inputs/results, and provider reasoning.
- The browser inserts answer text as text nodes. Proposal HTML is rendered server-side and fetched
  through a user-scoped route after a `proposal.created` event.

### Proposal To Garmin

```text
Coach tool or direct proposal form
  -> RunningProposalService
  -> AthleteDataService + safety/history checks + knowledge registry
  -> template expansion
  -> WorkoutService.create_proposal()
  -> draft/proposed, unaccepted, unscheduled Workout + immutable WorkoutRevision
  -> explicit acceptance of an exact revision
  -> optional local scheduling
  -> explicit Garmin publish
  -> explicit device push
```

- `app/services/planning/workout_proposals.py` selects and expands deterministic running templates;
  recent observed running history is required. Garmin heart-rate zones may personalize eligible
  targets and their principal fingerprint is retained for later revalidation.
- `app/services/planning/workout_service.py` is the central application service for revisioning,
  acceptance, scheduling, Garmin orchestration, idempotency, and workout events.
- `app/models/workout.py` stores several related state axes: workout lifecycle, approval, local
  schedule, accepted/current revision, Garmin content/calendar/device bindings, and durable Garmin
  operation/attempt state.
- Acceptance checks exact revision identity, content hash, lock version, and fresh safety/context
  fingerprints. Editing an accepted workout creates a new current revision; execution continues to
  use the previously accepted revision until the new one is explicitly accepted.
- `publish` and `push` are distinct user actions in `app/routes/workouts.py`. Garmin mutations are
  recorded before network calls. Ambiguous upload/update/push/delete outcomes are not blindly
  retried; only reconcilable schedule operations can be reconciled automatically.

### Planning, Adaptation, And Feedback

- `app/services/planning/daily_adaptation.py` implements user-triggered adaptation for an accepted,
  scheduled running workout for today. It can keep, rest, reduce volume, or propose an easy
  replacement. Changed/replacement workouts still require explicit acceptance.
- `app/services/planning/weekly_planner.py` builds deterministic weekly candidates. The preview in
  `app/routes/coach.py` is read-only; `app/routes/plans.py` persists plan and cycle revisions and
  ordinary workout proposals.
- `app/services/planning/weekly_plan_service.py`, `multiweek_planner.py`, and
  `app/models/planning.py` own immutable weekly/cycle revisions and planning inputs. Accepting a
  cycle does not accept or schedule its child workouts.
- Subjective activity feedback is collected through `app/routes/feedback.py` and read through
  analytics. It is not a separate conversational-memory subsystem.
- Proposal generation, generated-workout Garmin sync, daily adaptation, and plan generation are
  implemented behind default-off feature flags. They are not autonomous background Coach behavior;
  the scheduler only handles Garmin synchronization and recovery.

### Athlete Data Ingestion

```text
Garmin Connect
  -> app/services/garmin/sync.py and resumable backfills
  -> user-scoped SQLite rows + compressed raw activity files
  -> app/services/analytics/athlete_data.py
  -> Profile, planners, proposal generation, and Coach read tools
```

`AthleteDataService` returns compact deterministic dataclasses with explicit dates, units, coverage,
and user scoping. It does not expose credentials, raw file paths, sampled GPS tracks, or unbounded
source payloads to the Coach.

## External Integrations

- **OpenRouter:** `langchain-openrouter` sends the system prompt, current question, bounded completed
  conversation history, and tool-selected athlete data off-host. Automated tests mock the
  agent/provider.
- **Garmin Connect:** unofficial `garminconnect` API for authentication, import, workout publication,
  calendar operations, and device push. Tokens are files under
  `GARMIN_TOKEN_DIR/account-<account_id>/`; passwords are not stored in SQLite.
- **Google/GitHub:** Authlib-backed OIDC/OAuth2 login. There is no local password authentication.
- **Browser providers:** HTMX, Alpine, Chart.js, Leaflet, fonts, and OpenStreetMap tiles are loaded
  from third parties where used.

## Configuration And Startup

- Proposal, generated-workout Garmin sync, adaptation, plan-generation, and deferred-template flags
  in `app/config.py` default off; planner history gates default on. Garmin sync, adaptation, and plan
  generation require proposals to be enabled. Deferred quality templates and disabled history gates
  are development-only; malformed rollout allowlists fail closed.
- `get_settings()` is process-cached. `app.database` creates the engine and `app.auth` builds OAuth
  clients at import time, so tests and scripts must set environment overrides before importing app
  modules.
- Lifespan startup validates configuration, creates data/token/log directories, validates and caches
  the knowledge registry, upgrades Alembic, repairs interrupted account/Garmin state, and starts the
  scheduler. Invalid migrations or knowledge YAML abort startup.
- SQLite uses WAL, foreign keys, and a five-second busy timeout. Raw activity data lives below
  `DATA_DIR/raw/activities/user-<user_id>/<year>/`; database and filesystem cleanup must remain
  coordinated.

## Invariants To Preserve

- Protected data is always scoped through the authenticated `CurrentUser`; Coach routes also require
  completed onboarding. Unsafe requests require session-bound CSRF protection and rate limiting.
- Unaccepted or invalid content must never reach Garmin. Chat prose never constitutes acceptance,
  scheduling, publication, or push authorization.
- Preserve the accepted-revision execution boundary and the lifecycle shorthand
  `draft -> confirmed -> published -> pushed`.
- Workout, plan, and cycle revisions are immutable; migrations also enforce important ownership and
  revision relationships.
- Run exactly one Uvicorn worker and keep SQLite on a local filesystem. Scheduler state, rate
  limiting, MFA state, Garmin locks, and some queues are process-local.
- Serialize Garmin calls per account through `app/services/garmin/locks.py`. Preserve uncertain
  outcomes instead of retrying potentially successful external mutations.
- Garmin tokens and raw files are outside SQLite; account deletion and recovery must coordinate both
  stores.
- Automated tests must not require live Garmin credentials, OpenRouter credentials, or network
  access.

## Known Risk And Coupling Areas

- `app/routes/coach.py` combines chat transport/persistence with proposal forms, artifact projection,
  and weekly preview. Changes can cross otherwise separate conversation and planning concerns.
- `WorkoutService` is a large hub spanning revisions, safety, adaptation, feature gates, Garmin
  operations, events, and materialization; state changes require broad invariant awareness.
- Chat setup, tool telemetry, proposal creation, Garmin operations, and final answers intentionally
  use separate transactions. Failures can leave durable intermediate states that must remain
  interpretable.
- Active-response exclusion is an application-level check without a database uniqueness/locking
  guard. Stale streaming repair occurs only on selected mutations, not ordinary page reads.
- The agent stream adapter depends on LangChain/LangGraph event shapes and node names. Dependency
  upgrades can change streamed text/tool behavior even when local interfaces compile.
- Persistent Coach telemetry does not retain raw tool inputs/results or exact provider transcripts.
  A completed answer is auditable only through final prose, safe tool summaries, model/request IDs,
  and resulting artifacts.
- Workout state is distributed across approval, revision, schedule, Garmin binding, operation, and
  attempt fields. Avoid reasoning from `Workout.status` alone.
- Planning and Coach code frequently use server `date.today()`, while settings and knowledge are
  process-cached. User timezone handling and mutable test state need explicit attention.
- Current UI pathways duplicate some outcomes: direct and conversational single-workout proposals,
  plus Coach weekly preview and Plans persistence. Do not infer that colocated UI means shared
  service ownership.

## Commands

```bash
# Install exact locked Python dependencies
uv sync --frozen

# Development server
uv run uvicorn app.main:app --reload

# Focused tests while changing behavior
uv run pytest tests/test_training_agent.py
uv run pytest tests/test_workout_proposals.py

# Final verification
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check

# Also required for model or migration changes
uv run pytest tests/test_migrations.py

# Required after template or Tailwind-input changes; then update CSS cache keys
npm run build:css
```

Use `tests/test_training_agent.py` for chat/SSE/tool persistence, `test_workout_proposals.py` and
`test_workout_revisions.py` for proposal/lifecycle behavior, `test_workout_service.py` for Garmin
operation semantics, `test_daily_adaptation.py` for adaptation, `test_weekly_planner.py`,
`test_weekly_plan_service.py`, and `test_multiweek_planner.py` for plans, and
`test_production_hardening.py` for prompt/tool authority contracts.

## Documentation Pointers

- `AGENTS.md`: authoritative engineering commands and repository-wide invariants.
- `README.md`: product, deployment, privacy, and operational overview. Some feature descriptions
  still call implemented default-off Coach capabilities "future."
- `.env.example`: configuration template; it has the same stale "future" label for implemented
  default-off Coach capabilities.
- `docs/refactoring/ai-coach-intent.md`: desired refactoring/product direction only.
- `docs/athlete-trends.md`: detailed `AthleteDataService` behavior; references to a "future" Coach
  are stale.
- `docs/activity-backfill.md`, `docs/health-backfill.md`, `docs/garmin-data-inventory.md`: ingestion
  and source-data context.
- `docs/athlete-profile.md`, `docs/athlete-history-architecture.md`: useful historical/current mix;
  verify statements against code before relying on them.
- `docs/plans/ai-coach-implementation-masterplan.md`, phase completion files, and gate matrices:
  implementation history and time-specific evidence, not current architecture or current test
  results.
- `docs/research/garmin-ai-running-coach-consolidated-report.md`: research and prior target design,
  subordinate to code, migrations, and current intent.
