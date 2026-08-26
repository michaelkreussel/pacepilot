# AI Coach Current-State Context

## Purpose And Authority

This is a navigational map and code-quality assessment of the implementation inspected on 26
August 2026. Use it before changing the AI Coach or its direct dependencies. It is intentionally
not a target architecture, implementation plan, or file-by-file catalog.

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
  preview. The resulting broad controller boundary is classified in F2.
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

The docstring in `app/services/coach/__init__.py` still calls the package read-only when the proposal
tool can be enabled. This documentation finding is classified in F25.

## Responsibility And Size Inventory

The name "AI Coach" currently covers both the conversational agent and several deterministic
planning entry points colocated with it:

| Area | Current responsibilities | Size signals |
| --- | --- | --- |
| `app/routes/coach.py` | Chat pages and CRUD, stale-run handling, history selection, assistant-run creation, SSE translation, tool telemetry, proposal-card projection, a direct proposal form, and weekly-plan preview presentation | 790 lines; `planning_shadow()` is about 139 lines, `_stream_answer()` 89, `ask_coach()` 71, and `_proposal_card()` 60 |
| `app/services/coach/agent.py` | German prompt policy, OpenRouter and LangChain construction, model/tool limits, provider-event adaptation, safe event projection, and provider logging | 287 lines; `LangChainCoachAgent.stream()` is about 130 lines |
| `app/services/coach/tools.py` | Trusted runtime authority, eight bounded read tools, one proposal mutation, Coach-specific serialization, and activity labels | 350 lines; `create_running_workout_proposal()` is about 86 lines |
| `app/models/coach.py`, `app/repositories/coach.py` | Conversations, messages, assistant runs, tool-call telemetry, user-scoped lookup, and synchronized run/message lifecycle updates | 312 lines combined; four persistent entities represent a chat and its execution telemetry |
| `app/templates/coach.html`, `app/static/js/coach.js` | Persisted and live chat rendering, activity timeline, custom SSE parsing, proposal-card loading, form state, and conversation-title projection | 166 and 346 lines; persisted and streaming assistant UI are implemented separately |
| `app/services/planning/workout_proposals.py` | Proposal eligibility, history and safety checks, template selection/parameterization, personal HR targets, generation metadata, and constrained Easy Run edits | 682 lines; `RunningProposalService.create()` is about 141 lines and `edited_easy_run_metadata()` 121 |
| `app/services/planning/weekly_planner.py` | Planning-input loading, weekly composition, placement, validation report, generation context, and history counting | 690 lines; `compose_week()` is about 267 lines |
| `app/services/planning/workout_service.py` | Manual and generated workout creation, revisions, validation, acceptance, scheduling, adaptation, events, feature gates, Garmin operations, and reconciliation | 2,406 lines; Coach proposal creation enters this full application-service hub |
| `app/services/analytics/athlete_data.py` | User- and date-scoped facade over health, training, activity, running, feedback, and upcoming-workout analytics | 274 lines; a mixture of useful projections and one-line forwarding methods |
| `tests/test_training_agent.py` | Route, persistence, tool, agent-adapter, proposal-artifact, logging, and failure coverage | 1,132 lines across several behavioral boundaries |

These sizes are inspection signals, not hard limits. F5 classifies the stronger concern that several
large functions combine orchestration, policy, persistence, framework adaptation, and presentation.

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

## Current-State Findings

The classifications below describe the kind of follow-up each finding would require. They do not
pre-approve a fix or imply that all candidates should survive prioritization.

### F1. The conversational Coach does not own the adaptive coaching loop

**Classification:** `behavior-change candidate`

The registered tools can read recovery, feedback summaries, health, activities, and upcoming
workouts, and can create one new workout proposal (`app/services/coach/tools.py:80-320`). They cannot
read athlete goals, planning profile, plan/cycle state, or progress toward a goal. They also cannot
modify an existing proposal, request daily adaptation, record feedback, or operate on a plan.

Those capabilities exist in planning models, services, and structured routes, but not in the
conversation. As a result, the implementation provides several coaching features without providing
the continuous, goal- and feedback-aware relationship described by the refactoring intent. Changing
that boundary would intentionally change product behavior, so it is not a behavior-preserving
cleanup.

### F2. The Coach route is a broad controller rather than a cohesive HTTP adapter

**Classification:** `architecture concern`

`app/routes/coach.py` combines at least four independently changing areas:

- conversation loading, rendering, creation, deletion, and stale-state handling (`:87-252`,
  `:461-573`);
- weekly planner presentation policy and labels (`:255-458`);
- direct workout-proposal form parsing and error translation (`:497-544`); and
- SSE protocol, tool telemetry, artifact projection, run lifecycle, and model orchestration
  (`:576-790`).

A change to weekly-plan copy, proposal-card state, conversation lifecycle, or streaming persistence
requires navigating the same 790-line module and its unrelated imports. The split test ownership
mirrors this boundary problem: `test_training_agent.py`, `test_workout_proposals.py`, and
`test_routes.py` all protect behavior owned by this one route file.

### F3. `WorkoutService` is a 2,406-line application-service hub

**Classification:** `architecture concern`

`app/services/planning/workout_service.py` owns manual and generated creation, immutable revisions,
contextual validation, acceptance, scheduling, adaptations, events, feature policy, Garmin
publication, device push, reconciliation, and state materialization. The Coach's narrow proposal
operation enters this full hub through `create_proposal()` (`:149-280`).

Changes to one workout transition require awareness of unrelated Garmin and adaptation invariants.
Conversely, changing Coach proposal lineage or feature naming requires understanding a service whose
primary responsibility is the complete workout lifecycle.

### F4. Planning depends back on conversational implementation details

**Classification:** `architecture concern`

The nominally general workout service imports `CoachAssistantRun` at module scope
(`app/services/planning/workout_service.py:14-25`), mutates the run during proposal creation
(`:188-202`), and dynamically imports `COACH_PROMPT_TEMPLATE_VERSION` (`:203-210`). The proposal
service also imports the private `_count_consistent_weeks()` implementation from the weekly planner
(`app/services/planning/workout_proposals.py:38`, `:254`).

These dependency directions couple generic workout persistence to a specific LLM provider/run model
and couple single-workout eligibility to a private weekly-planner helper. Prompt provenance,
conversation persistence, proposal creation, and weekly-history policy therefore cannot change
independently.

### F5. Several core functions combine too many abstraction levels

**Classification:** `behavior-preserving refactor candidate`

The main size hotspots are not merely long; each mixes distinct levels of work:

- `planning_shadow()` maps request input, calls domain planning, decodes loosely typed generation
  context, applies German presentation labels, and builds navigation (`coach.py:311-449`).
- `LangChainCoachAgent.stream()` interprets provider/framework internals, records timing, translates
  tool calls, recognizes domain artifacts, and emits UI-neutral events (`agent.py:143-272`).
- `RunningProposalService.create()` combines replay, feature policy, athlete context, safety,
  quality spacing, template parameterization, device personalization, metadata, and persistence
  (`workout_proposals.py:419-559`).
- `compose_week()` combines eligibility, placement, template expansion, warnings, validation,
  provenance, and hashing (`weekly_planner.py:216-482`).

Each function can be changed without changing intended behavior, but safe edits currently require a
large working set and tests across several concepts.

### F6. Proposal origin is represented and checked in several forms

**Classification:** `architecture concern`

The same origin relationship is carried as four optional IDs in `CoachRuntimeContext`
(`app/services/coach/tools.py:43-52`), four required IDs in `ProposalOrigin`
(`app/services/planning/workout_service.py:65-70`), three origin foreign keys on `Workout`
(`app/models/workout.py:95-103`), and a reverse `CoachAssistantRun.workout_id`
(`app/models/coach.py:68-99`). The tool, workout service, and proposal-card projection all repeat
cross-checks (`tools.py:238-267`, `workout_service.py:188-210`, `:266-280`, and
`routes/coach.py:87-107`).

The database does not enforce that all origin messages belong to the recorded conversation, so the
implementation carries a high validation and test burden for one proposal-per-run relationship.
This lineage may have audit value, but its current bidirectional representation is disproportionate
to the single mutation it supports.

### F7. Message and assistant-run lifecycle state is duplicated

**Classification:** `behavior-preserving refactor candidate`

`CoachMessage` and `CoachAssistantRun` both persist status, model ID, and completion time
(`app/models/coach.py:39-51`, `:68-92`). `complete_message()` and `fail_message()` update both records
in lockstep (`app/repositories/coach.py:121-145`). Application reads need the run identity, origin,
request metadata, and artifact link, but no production query was found that independently consumes
the duplicated run status or completion time.

The duplicate writable state makes divergence representable even though normal repository calls try
to keep it synchronized.

### F8. Proposal creation repeats adjacent validation responsibilities

**Classification:** `behavior-preserving refactor candidate`

`RunningProposalService` checks date, running history, safety, eligibility, quality spacing, and
template constraints before creating the aggregate
(`app/services/planning/workout_proposals.py:182-300`, `:419-553`).
`WorkoutService.create_proposal()` immediately performs structural validation and another contextual
safety validation in the same synchronous flow
(`app/services/planning/workout_service.py:159-167`, `:218-249`). Origin consistency is similarly
checked before the call and again in the workout service.

Revalidation at lifecycle boundaries can be valuable, but ownership of "safe enough to create" is
currently split across the tool, proposal service, safety-context builders, template expansion, and
workout service. That makes it hard to tell which check is canonical and which is defense in depth.

### F9. Single-workout proposals have two user pathways to the same service

**Classification:** `deletion/deprecation candidate`

The Coach page exposes a direct structured form (`app/templates/coach.html:20-33`,
`app/routes/coach.py:497-544`) and the conversation exposes
`create_running_workout_proposal` (`app/services/coach/tools.py:217-302`). Both delegate to
`RunningProposalService` and produce the same unaccepted, unscheduled workout outcome, but they have
separate UI, error translation, origin semantics, and integration tests.

No distinct domain capability was found that requires both entry points. Choosing which experience
provides more value is a product decision; preserving both solely because both exist would conflict
with the simplification intent.

### F10. Weekly planning is split into a Coach preview and a Plans workflow

**Classification:** `deletion/deprecation candidate`

`/coach/planning-shadow` builds a read-only candidate and approximately 139 lines of presentation
context, then its template posts to `/plans/generate-week`
(`app/routes/coach.py:311-449`, `app/templates/coach/planning_shadow.html:43-54`). Plan persistence,
calendar presentation, and multiweek planning live under `app/routes/plans.py`.

The preview adds another navigation and presentation layer but no separate planning capability. Its
continued value should be demonstrated rather than assumed, especially because the desired
experience calls for fewer modes and review stages.

### F11. Persisted and live assistant messages have separate renderers

**Classification:** `behavior-preserving refactor candidate`

Server-rendered history constructs assistant activity timelines in
`app/templates/coach.html:96-131`; live responses independently construct equivalent DOM in
`app/static/js/coach.js:43-188`. Step counts, statuses, duration text, final steps, classes, and
accessibility attributes must remain synchronized manually. Conversation-title truncation is also
implemented on the server (`app/routes/coach.py:745-746`) and in JavaScript (`coach.js:305-311`).

This is a multiple-implementation problem: a visual or accessibility change can be correct during
streaming and wrong after reload, or vice versa.

### F12. Presentation and schema concepts have multiple authorities

**Classification:** `behavior-preserving refactor candidate`

Examples include:

- running template IDs in `workout_proposals.py:61-68` and again as a literal in
  `coach/tools.py:222-229`;
- goal labels in `routes/coach.py:295-301` and `routes/plans.py:62-68`;
- plan-role labels in `routes/coach.py:264-268` and `routes/plans.py:54-60`;
- health metrics as analytics fields plus a separate `HealthMetric` literal consumed with dynamic
  `getattr()` (`coach/tools.py:24-39`, `:108-135`); and
- Coach proposal status labels reconstructed from raw workout axes in `routes/coach.py:116-136`
  rather than using the normal workout view projection.

Adding a template, metric, goal type, role, or lifecycle state is therefore a multi-file change with
no single model-to-presentation contract.

### F13. Feature ownership and rollout policy are scattered

**Classification:** `architecture concern`

Proposal availability is evaluated during agent construction
(`app/services/coach/dependencies.py:10-21`), template context creation (`app/web.py:99-124`), direct
proposal creation
(`app/services/planning/workout_proposals.py:419-435`), and later workout/Garmin transitions
(`app/services/planning/workout_service.py:2061-2199`). Plan flags are similarly checked in Coach and
Plans routes.

The call sites share `coach_feature_enabled()`, so the rollout algorithm itself is not duplicated.
However, capability evaluation, presentation, and enforcement are scattered and must be coordinated
across all of these locations. The `coach_*` naming also makes deterministic planning and
generated-workout Garmin behavior appear owned by the conversational Coach even when they are
invoked through other routes.

### F14. Active-response exclusion is race-prone

**Classification:** `architecture concern`

`ask_coach()` loads all messages, checks for a streaming response, then inserts a new streaming
message and commits (`app/routes/coach.py:741-763`). There is no uniqueness constraint, lock, or
compare-and-set representing one active response per conversation in `app/models/coach.py`.
Concurrent requests can both pass the read check before either inserts its response.

Stale repair is also opportunistic: `_has_active_response()` marks rows older than ten minutes
interrupted only when sending a message or deleting a conversation (`coach.py:163-169`, `:558-560`).
Ordinary page rendering can continue to present a stale response as active.

### F15. Conversation rendering is unbounded and performs repeated artifact lookups

**Classification:** `behavior-preserving refactor candidate`

`conversation_messages()` eagerly loads every message and tool call in a conversation without a
limit (`app/repositories/coach.py:41-52`). Only model history is bounded. Proposal projection then
accesses `message.generated_run`, re-queries each run through `_proposal_card()`, and loads its
workout and revision (`app/routes/coach.py:87-160`). `generated_run` is not included in the repository
eager-load options.

Long-lived conversations can therefore grow page payload, object materialization, and query count
without bound even though prior prose history is limited to 20 messages and 12,000 characters. The
current user message and trusted date context are added separately to the next model call.

### F16. Transaction splitting leaves durable states that are only partly explainable

**Classification:** `architecture concern`

The user message, empty assistant message, and run commit before the provider call
(`app/routes/coach.py:745-763`). Each tool lifecycle event commits in a separate session
(`:581-605`), proposal creation can commit in `WorkoutService.create_proposal()`, and final prose
commits later (`:677-680`). A proposal intentionally survives provider failure, but partial prose is
lost and a tool row can remain `running` if no completion event arrives.

Persistent telemetry stores safe input summaries and generic failures, not tool results or the exact
provider transcript (`app/models/coach.py:102-119`). Later model history contains prose only. The
system therefore pays the complexity of durable run and tool entities without being able to
reconstruct exactly which returned facts informed an answer.

### F17. The provider adapter depends on unstable framework internals

**Classification:** `architecture concern`

`LangChainCoachAgent.stream()` depends on the legacy combined `messages`/`updates` stream, exact
LangGraph node names `model` and `tools`, `metadata["langgraph_node"]`, tool-call dictionary shapes,
`ToolMessage.status`, and JSON string tool results (`app/services/coach/agent.py:164-244`,
`:275-287`). The inline comment explicitly explains dependence on the legacy v1 stream shape.

A LangChain, LangGraph, or OpenRouter-adapter upgrade can change visible text, tool telemetry, or
artifact events without violating the local `CoachAgent` protocol.

### F18. Date semantics are resolved independently and use server-local time

**Classification:** `behavior-change candidate`

The request runtime captures `date.today()` in `app/routes/coach.py:765-775`, but proposal generation
resolves today again in `app/services/planning/workout_proposals.py:188`, `:193`, `:254`, and `:314`.
Weekly planning resolves it independently in `weekly_planner.py:603`, and rendering has additional
calls in `routes/coach.py:231-233` and `:320`.

The trusted date shown to the model is not passed through the deterministic proposal service. Calls
crossing server midnight can disagree, and all dates use the server timezone rather than an explicit
user timezone. Correcting those semantics would be observable behavior, not a pure refactor.

### F19. Full agent construction is used as a configured-state check

**Classification:** `behavior-preserving refactor candidate`

`get_coach_agent()` constructs `ChatOpenRouter`, middleware, all tools, and the LangChain graph every
time the dependency resolves (`app/services/coach/dependencies.py:10-21`,
`app/services/coach/agent.py:110-141`). The dependency is injected into Coach GET routes and direct
proposal error rendering merely to determine whether the page should say "configured"
(`app/routes/coach.py:189-252`, `:461-469`, `:497-544`).

Ordinary page rendering therefore depends on provider-agent construction even when no model call can
occur.

### F20. Several compatibility or thin APIs have no production consumer

**Classification:** `deletion/deprecation candidate`

Verified candidates are:

- `EasyRunProposalRequest` and `RunningProposalService.create_easy_run()` are used extensively by
  tests, but production callers use `RunningProposalRequest` and `create()`
  (`app/services/planning/workout_proposals.py:80-90`, `:406-417`).
- `/coach/workout-proposals/easy-run` aliases `/running`; the committed template posts only to
  `/running` (`app/routes/coach.py:497-499`, `app/templates/coach.html:23`).
- `AthleteDataService.get_weekly_running_volume()` has no application caller, while
  `get_training_load_trend()` is an identical wrapper and is used only in a test
  (`app/services/analytics/athlete_data.py:148-152`).
- The server emits `run.started`, but the browser has no handler; the browser handles an `error` SSE
  event that the server never emits (`app/routes/coach.py:648-651`,
  `app/static/js/coach.js:223-244`). The `run_id` in `proposal.created` is likewise not consumed by
  the browser.

These are candidates rather than confirmed safe deletions because external or operational consumers
were not assessed beyond repository usage.

### F21. Important Coach decisions lack behavioral tests

**Classification:** `test debt`

Route tests replace the agent with canned event generators (`tests/test_training_agent.py:55-80`,
`:216-505`). The focused-question example scripts the desired answer rather than exercising the
agent (`:445-497`). No automated test demonstrates that a real agent answer:

- uses relevant personal data without treating missing data as zero;
- makes material assumptions or uncertainty explicit;
- asks one focused clarification rather than refusing or over-questioning;
- changes later guidance after feedback; or
- relates recommendations to goals or plan progress.

The test named for prompt-injection protection only loads a corpus and asserts the statically
registered tool names; it never submits any corpus case to an agent
(`tests/test_production_hardening.py:99-120`). These tests protect mutation authority, not answer
quality or the adaptive coaching promise.

### F22. Streaming, history, and concurrency boundaries have test gaps

**Classification:** `test debt`

`test_follow_up_includes_bounded_conversation_history` uses only three messages and reaches neither
the 20-message nor 12,000-character limit (`tests/test_training_agent.py:641-664`). There are no
tests for truncating an oversized message, preserving role order at the boundary, or excluding
failed/interrupted messages.

No test sends simultaneous message submissions or verifies stale-stream repair. Failure tests do not
cover answer text emitted before failure, orphaned `running` tool calls, tool telemetry persistence
failure, a proposal commit followed by artifact-event projection failure, or provider completion
without visible text (`test_training_agent.py:543-639`, `:1054-1132`).

### F23. Coach adapters and browser behavior have shallow coverage

**Classification:** `test debt`

Only a subset of read tools is called at the Coach wire boundary. Analytics tests cover underlying
calculations, but not Coach-specific serialization, point truncation, metric selection, bounds, or
payload shape for all tools (`app/services/coach/tools.py:55-214`; Coach tool tests are concentrated
in `tests/test_training_agent.py:785-895`). Origin validation has a happy-path test but no mismatched
role, conversation, user, or run cases.

Direct proposal route tests cover disabled and successful flows, but not malformed dates, time
bounds, unsupported templates, HTTP idempotent replay, safety errors, or preserving form input
(`tests/test_workout_proposals.py:675-826`). Proposal-card tests cover unconfirmed and scheduled
states but not every integrity guard or displayed lifecycle state
(`tests/test_training_agent.py:391-420`).

JavaScript tests assert source substrings rather than executing the SSE parser or DOM state machine
(`tests/test_static_theme.py:85-92`, `:110-115`). Chunk boundaries, multi-line data, malformed JSON,
UTF-8 splitting, proposal fetch behavior, and control restoration are not behaviorally exercised.

### F24. Some tests make behavior-preserving change unnecessarily expensive

**Classification:** `test debt`

`tests/test_training_agent.py` imports private `_stream_answer` (`:37`), calls LangChain-decorated
tool `.func` through `cast(Any, ...)` (`:292-317`, `:459-468`, `:563-568`), constructs
`LangChainCoachAgent` via `__new__`, replaces private `_agent`, and asserts exact LangGraph stream
mode, node names, metadata, and event order (`:898-1050`). The primary proposal test spans tool
execution, replay, persistence, origin links, SSE, card rendering, lifecycle projection, and deletion
in one 169-line scenario (`:275-443`).

These tests catch current framework shapes but couple stable product behavior to private adapters and
make it difficult to simplify boundaries without rewriting broad tests.

### F25. Current documentation misstates active behavior

**Classification:** `documentation/context concern`

`app/services/coach/__init__.py` calls the Coach read-only even though the proposal mutation can be
enabled. `app/services/analytics/athlete_data.py:116-117` still refers to future Coach consumers.
`README.md:53-54` describes read-only Coach access, while later privacy text correctly documents the
proposal tool (`README.md:267-269`). Configuration descriptions call implemented default-off Coach
features "future" (`README.md:200-205` and `.env.example`).

Phase plans and completion documents are useful historical evidence but describe staged goals and
time-specific gates. Treating them as current architecture obscures which capabilities are active,
default-off, conversational, or available only through structured routes.

## Established Safeguards

These are verified strengths, not findings requiring classification:

- Coach routes require authenticated, onboarded users, and conversation lookup is user-scoped.
- Unsafe requests pass through shared CSRF protection and Coach-specific rate limiting.
- Trusted user, date, session, request, and origin context is outside model-visible tool schemas.
- The only LLM-visible mutation cannot author workout steps and creates an unaccepted, unscheduled
  server-side proposal.
- Chat prose cannot accept, schedule, publish, or push a workout; those remain explicit commands.
- Browser answer text is inserted as text nodes, and proposal HTML comes from a user-scoped server
  route rather than model output.
- The deterministic planning services and analytics have substantial direct test coverage even where
  the conversational integration is weak.

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
- `README.md`: product, deployment, privacy, and operational overview. F25 classifies its stale
  descriptions of implemented default-off Coach capabilities as "future."
- `.env.example`: configuration template; F25 also covers its stale "future" labels.
- `docs/refactoring/ai-coach-intent.md`: desired refactoring/product direction only.
- `docs/athlete-trends.md`: detailed `AthleteDataService` behavior; F25 covers its stale references
  to a "future" Coach.
- `docs/activity-backfill.md`, `docs/health-backfill.md`, `docs/garmin-data-inventory.md`: ingestion
  and source-data context.
- `docs/athlete-profile.md`, `docs/athlete-history-architecture.md`: useful historical/current mix;
  verify statements against code before relying on them.
- `docs/plans/ai-coach-implementation-masterplan.md`, phase completion files, and gate matrices:
  implementation history and time-specific evidence, not current architecture or current test
  results.
- `docs/research/garmin-ai-running-coach-consolidated-report.md`: research and prior target design,
  subordinate to code, migrations, and current intent.
