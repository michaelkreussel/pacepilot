# AI Coach Refactoring Implementation Plan

**Status:** Proposed implementation breakdown. No task in this document is an
approval to implement the refactor.

## Planning Basis

This plan decomposes `docs/refactoring/ai-coach-refactor-spec.md`. The current
implementation map in `docs/refactoring/ai-coach-current-state.md` is used only
to identify likely files and safe sequencing; the specification remains the
authority for future behavior.

Tasks are grouped by change type, not by execution order. Dependencies are the
authoritative ordering. In particular, expand migrations may precede runtime
cutovers, while destructive migrations must follow them.

Every implementation task must also preserve these standing invariants:

- authenticated, onboarded, user-scoped access with CSRF and rate limiting;
- deterministic workout and plan content, immutable revisions, and explicit
  transaction ownership;
- explicit exact-revision acceptance and local scheduling;
- `draft -> confirmed -> published -> pushed`, with no model prose acting as
  authorization;
- per-account Garmin serialization and unknown-outcome preservation;
- no live OpenRouter or Garmin dependency in automated tests; and
- German user-facing copy and safe text insertion for streamed prose.

Per-task verification commands are additive to the repository definition of
done. Any task that changes a template or `app/static/css/tailwind.input.css`
must run `npm run build:css` and update the stylesheet cache key where required,
even when the task-specific verification line does not repeat that command.

## Dependency Overview

```text
BP01 -> BP04 -> M01 -> BP10 -> M02 -> BC01

BP03 -> BC05 -> BC06 -> BC07/BC08 -> BC06A
BC08 -> BC09
BC06A + BC09 -> BC10 -> BC11 -> BC12 -> BC14
BP07C + BC09 + BC12 + M05 -> BC13

BP07A -> M04 -> BC15 -> BC16 -> BC16A -> BC17
M01 + BP07A -> M03 -> BC17

BP06 + BC03 -> DD04A
BP09 + DD04A -> DD04B -> M06
BP10 + M03 + M06 -> M07 -> CM01
DD05 + M05 + M07 -> M08 -> CM02

BC11 -> DD02
BC17 -> DD03
BC10 + BC13-BC17 -> DD06A/DD06B
```

The migration IDs above refer to plan task IDs, not Alembic revision numbers.
New Alembic revisions must be based on the repository head at implementation
time.

## 1. Behavior-Preserving Refactoring

### BP01: Rebase Coach Characterization Tests On Public Boundaries

**Goal:** Preserve every KEEP conversation outcome while making later internal
changes possible without tests depending on private route or LangGraph shapes.

**Exact scope:** Reorganize characterization coverage around authenticated route,
repository, provider-adapter, and persistence outcomes. Cover the exact latest
20-message and 12,000-character history rules, suffix truncation, active-response
exclusion, stale recovery, failed/interrupted partial exclusion, durable
artifacts after provider failure, and completed-answer reload. Do not alter
application behavior.

**Likely files/modules involved:**

- `tests/test_coach_characterization.py`
- `tests/test_training_agent.py`
- shared Coach test fixtures in `tests/conftest.py`, if needed

**Behavior that must remain:** Existing conversation CRUD, history ordering and
bounds, persistence timing, failure semantics, authentication, onboarding,
CSRF, rate limiting, and user isolation.

**Acceptance criteria:**

- Each KEEP conversation outcome has a focused behavioral test.
- Stable tests no longer import `_stream_answer`, instantiate the agent with
  `__new__`, or assert LangGraph node names and legacy stream modes.
- Existing behavior passes before any production refactor begins.

**Verification/tests:** `uv run pytest tests/test_coach_characterization.py
tests/test_training_agent.py`

**Dependencies on previous tasks:** None.

### BP02: Consolidate Domain-Owned Coaching Contracts

**Goal:** Give registered workout formats, health metrics, labels, and bounded
analytics schemas one domain owner without changing returned data.

**Exact scope:** Make the knowledge registry authoritative for supported workout
format IDs and metadata; move health metric choices into analytics; use existing
Plans/workout presentation labels instead of Coach literals; remove only exact
duplicate literals in consumers. Keep payloads selected and bounded rather than
creating a load-all-athlete-data contract.

**Likely files/modules involved:**

- `app/services/planning/registry.py`
- `app/services/planning/registry_models.py`
- `app/services/analytics/health_trends.py`
- `app/services/analytics/athlete_data.py`
- `app/services/coach/tools.py`

**Behavior that must remain:** The same currently enabled formats, dates, units,
coverage states, missing-value omission, deterministic metric values, and
model-visible payload bounds.

**Acceptance criteria:**

- A format or health-metric addition has one authoritative declaration.
- Coach schemas expose user choices only; user ID, date, sessions, and origin
  remain runtime supplied.
- Existing analytics and proposal serialization is unchanged.
- Missing, partial, unsupported, unsynchronized, and synchronized-empty data
  fixtures preserve their distinct coverage states and never become zero.
- Recovery/readiness source distinction, health trends/baselines, training
  load/frequency/consistency/volume, recent/selected activities, effective
  feedback, upcoming workouts, and sync coverage retain bounded wire tests.

**Verification/tests:** `uv run pytest tests/test_knowledge_registry.py
tests/test_athlete_trends.py tests/test_training_agent.py
tests/test_production_hardening.py`

**Dependencies on previous tasks:** BP01.

### BP03: Introduce Planning-Owned Query Boundaries

**Goal:** Let callers read goals, profile, availability, anchors, current plans,
and accepted cycles without reconstructing planning state or importing Coach.

**Exact scope:** Add typed, user-scoped planning queries and move equivalent
query/projection code from planner or route callers behind them. This task is
read-only and must not add Coach operations or change planner decisions.

**Likely files/modules involved:**

- `app/services/planning/planning_queries.py` (new)
- `app/services/planning/weekly_planner.py`
- `app/services/planning/multiweek_planner.py`
- `app/routes/plans.py`
- `tests/test_athlete_planning_inputs.py`

**Behavior that must remain:** Current goal/profile/availability semantics,
accepted-cycle identity, query ordering, reference integrity, and cross-user
isolation.

**Acceptance criteria:**

- Every query requires an explicit user boundary and, where time-sensitive, an
  explicit date.
- Analytics and Coach can consume typed planning facts without importing route
  presentation code.
- Weekly and multiweek candidates are byte-for-byte or structurally equivalent
  for existing fixtures.

**Verification/tests:** `uv run pytest tests/test_athlete_planning_inputs.py
tests/test_weekly_planner.py tests/test_multiweek_planner.py tests/test_routes.py`

**Dependencies on previous tasks:** BP01.

### BP04: Extract Conversation History And Execution Preparation

**Goal:** Make `app/routes/coach.py` an HTTP adapter while retaining exact
conversation behavior.

**Exact scope:** Move bounded prose-history construction, title derivation,
message execution inputs, and trusted runtime-context assembly into a small
framework-neutral Coach conversation module. Repositories still own scoped
queries and do not commit; the route still owns the HTTP transaction boundary.

**Likely files/modules involved:**

- `app/services/coach/conversation.py` (new)
- `app/routes/coach.py`
- `app/repositories/coach.py`
- `app/services/coach/agent.py`
- `tests/test_coach_characterization.py`

**Behavior that must remain:** Exact history limits and suffix truncation,
completed-message-only history, durable user messages before provider execution,
current question placement, and conversation title output.

**Acceptance criteria:**

- The route no longer implements history truncation or title policy.
- The extracted boundary has no FastAPI, SSE, LangChain, or template dependency.
- Characterization tests pass unchanged in outcome.

**Verification/tests:** `uv run pytest tests/test_coach_characterization.py
tests/test_training_agent.py`

**Dependencies on previous tasks:** BP01.

### BP05: Separate Provider Availability From Construction

**Goal:** Avoid constructing an OpenRouter client or agent graph while rendering
Coach pages or checking availability.

**Exact scope:** Replace agent construction as a configured-state check with a
credential/configuration predicate. Construct the provider adapter only after a
valid answer request has claimed an execution.

**Likely files/modules involved:**

- `app/services/coach/dependencies.py`
- `app/routes/coach.py`
- `app/config.py`
- `tests/test_training_agent.py`

**Behavior that must remain:** Provider availability still requires configured
credentials/model; unavailable pages and answer requests retain current German
errors, authentication, and status codes.

**Acceptance criteria:**

- Coach GET and conversation-selection requests do not instantiate provider
  objects.
- One provider adapter is constructed only for an accepted answer request.
- Configuration tests do not require a network call.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_config.py`

**Dependencies on previous tasks:** BP01.

### BP06: Contain Provider Framework Details In One Adapter

**Goal:** Expose only `answer text`, `artifact available`, `completed`, and
`failed` events to conversation execution.

**Exact scope:** Move OpenRouter/LangChain construction, prompt wiring,
framework-event decoding, timeout/model limits, and provider-specific tool
wrapping behind one adapter. Replace tests of node names, message dictionaries,
legacy stream modes, and tool-result encodings with adapter contract tests.

**Likely files/modules involved:**

- `app/services/coach/provider.py` (new, or a reduced `agent.py`)
- `app/services/coach/agent.py`
- `app/services/coach/dependencies.py`
- `app/services/coach/tools.py`
- `tests/test_training_agent.py`

**Behavior that must remain:** Streamed answer order, deterministic artifact
detection, reasoning exclusion, timeouts, safe logs, model/tool limits, and no
logging of prompts, answer text, or personal metrics.

**Acceptance criteria:**

- Provider types and framework event shapes do not escape the adapter module.
- Fake local events can drive all route/execution tests.
- Provider-specific tests exist only at the adapter boundary.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_production_hardening.py`

**Dependencies on previous tasks:** BP02, BP04, BP05.

### BP07A: Expose A Public Transaction-Neutral Weekly Persistence Seam

**Goal:** Let cycle and conversational planning persist weekly candidates without
depending on private functions or hidden commits.

**Exact scope:** Replace the multiweek planner's import of private weekly
persistence with one public, transaction-neutral operation supporting caller-
owned commit/rollback. Do not change candidate generation or persistence data.

**Likely files/modules involved:**

- `app/services/planning/weekly_plan_service.py`
- `app/services/planning/multiweek_planner.py`
- `tests/test_weekly_plan_service.py`
- `tests/test_multiweek_planner.py`

**Behavior that must remain:** Plan idempotency and rollback, immutable
revisions, current revision selection, child workout creation, and cycle atomicity.

**Acceptance criteria:**

- Multiweek planning no longer imports a private weekly persistence helper.
- Callers can compose a larger transaction without an unexpected service
  commit.
- Existing weekly and cycle persistence rows remain unchanged for current
  fixtures.

**Verification/tests:** `uv run pytest tests/test_weekly_plan_service.py
tests/test_multiweek_planner.py`

**Dependencies on previous tasks:** BP01, BP03.

### BP07B: Expose Transaction-Neutral Feedback Commands And Queries

**Goal:** Give conversational feedback a small planning-owned boundary without
changing validation, precedence, or persistence behavior.

**Exact scope:** Separate feedback writes/deletes from queries/export where
needed; make transaction ownership explicit; preserve contextual-validation
invalidation until DD05 replaces it.

**Likely files/modules involved:**

- `app/services/planning/feedback_service.py`
- `app/routes/feedback.py`
- `app/services/account_lifecycle.py`
- `tests/test_feedback.py`

**Behavior that must remain:** Existing field validation, user scoping,
Garmin/manual precedence, invalidation behavior, deletion return values, account
export, and route transactions.

**Acceptance criteria:**

- Commands and queries are callable without importing Coach or presentation.
- No command hides a commit from a caller composing a larger atomic operation.
- Existing feedback route and export outcomes are unchanged.

**Verification/tests:** `uv run pytest tests/test_feedback.py
tests/test_account_lifecycle.py tests/test_routes.py`

**Dependencies on previous tasks:** BP01.

### BP07C: Isolate Garmin Transport From Workout Lifecycle Orchestration

**Goal:** Keep local workout lifecycle decisions separate from Garmin request,
attempt, retry, reconciliation, and uncertain-outcome behavior.

**Exact scope:** Move or delegate provider request construction, durable attempt
handling, reconciliation, and outcome mapping to `garmin` boundaries while the
workout service retains accepted-revision, structural, schedule, and transaction
orchestration. This is a behavior-preserving move; do not add fit
acknowledgements yet.

**Likely files/modules involved:**

- `app/services/planning/workout_service.py`
- `app/services/garmin/workout_operations.py`
- `app/services/garmin/workout_export.py`
- `tests/test_workout_service.py`

**Behavior that must remain:** Exact accepted revision, structurally valid
content only, per-account serialization, pre-network durable operations,
idempotency, retries, reconciliation, and unknown outcome preservation.

**Acceptance criteria:**

- Garmin modules do not interpret model output or chat text.
- Workout lifecycle code does not implement provider request/retry/outcome
  details.
- Existing publish, schedule, push, retry, reconciliation, and unknown-outcome
  tests pass without behavioral changes.

**Verification/tests:** `uv run pytest tests/test_workout_service.py
tests/test_workout_revisions.py`

**Dependencies on previous tasks:** BP01.

### BP08: Use Canonical Artifact And Lifecycle Projections

**Goal:** Stop reconstructing workout and plan state in Coach routes and browser
code.

**Exact scope:** Add a user-scoped, server-owned presentation projection for
workout/plan artifact identity, exact revision, lifecycle label, duration, date,
warning outcome, dated personal evidence, coverage, recommendation, safer
alternative, and available explicit actions. Include a distinct warning-
acknowledgement presentation state separate from lifecycle action controls.
Initially preserve current workout card output; adaptation and plan artifacts
are added by later behavior tasks.

**Likely files/modules involved:**

- `app/services/planning/workout_views.py`
- `app/services/coach/presentation.py` (new)
- `app/routes/coach.py`
- `app/templates/workouts/_coach_proposal_card.html`
- `tests/test_training_agent.py`

**Behavior that must remain:** User scoping, current versus accepted revision
distinction, existing German labels, server-rendered artifact HTML, and explicit
workout lifecycle controls. A warning does not hide or disable a draft.

**Acceptance criteria:**

- `app/routes/coach.py` no longer derives lifecycle status from raw flags.
- Cross-user artifact IDs never render.
- Existing draft, accepted, scheduled, published, pushed, rejected, and failed
  projections remain correct.
- Warning acknowledgement and lifecycle action are represented as separate,
  exact revision/date controls.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_workout_revisions.py tests/test_workout_service.py`

**Dependencies on previous tasks:** BP02, BP04.

### BP09: Unify Live And Persisted Message Presentation

**Goal:** Use one presentation contract for working, prose, artifacts,
completion, failure, and reload.

**Exact scope:** Introduce a server-rendered message partial and reduce browser
logic to safe text appending, artifact insertion, and server-defined terminal
states. Extract the incremental SSE parser so UTF-8 chunking, multiline events,
malformed JSON, and terminal handling can be executed in tests using the Node
built-in test runner; do not add a frontend framework.

**Likely files/modules involved:**

- `app/templates/coach/_message.html` (new)
- `app/templates/coach.html`
- `app/static/js/coach.js`
- `tests/coach_sse.test.mjs` (new)
- `tests/test_training_agent.py`

**Behavior that must remain:** Prose is inserted as text, artifact HTML is
server-rendered and user-scoped, controls restore after failure, mobile/desktop
markup remains functional, and CSRF headers remain present.

**Acceptance criteria:**

- The same message state has equivalent live and reloaded markup.
- Browser code does not implement domain status, title, duration, or lifecycle
  label policy.
- Executable parser tests replace JavaScript source-substring assertions.

**Verification/tests:** `node --test tests/coach_sse.test.mjs`; `uv run pytest
tests/test_training_agent.py tests/test_static_theme.py`

**Dependencies on previous tasks:** BP06, BP08.

### BP10: Cut Runtime Execution And Lineage Over To CoachMessage

**Goal:** Make the assistant message the only runtime owner of execution state
and the canonical source for generated artifacts.

**Exact scope:** After M01, create/complete/fail assistant messages directly;
derive idempotency and artifact lookups from the assistant message; dual-write
the transitional source columns required by M01; remove planning imports of
`CoachAssistantRun` and Coach prompt constants. Do not drop tables yet.

**Likely files/modules involved:**

- `app/repositories/coach.py`
- `app/services/coach/conversation.py`
- `app/services/coach/tools.py`
- `app/services/planning/workout_service.py`
- `app/routes/coach.py`

**Behavior that must remain:** One durable user/assistant pair per accepted
request, artifact idempotency, artifact survival after provider failure,
privacy-safe provenance, and user/conversation ownership checks.

**Acceptance criteria:**

- Production execution neither creates nor updates `CoachAssistantRun`.
- Planning and workout modules import no Coach model, prompt constant, provider
  type, or SSE event.
- One assistant message can source zero, one, or multiple artifacts.
- Each deterministic artifact and its source-message link commit atomically;
  assistant completion/failure remains one lifecycle update.

**Verification/tests:** `uv run pytest tests/test_coach_characterization.py
tests/test_training_agent.py tests/test_workout_proposals.py`

**Dependencies on previous tasks:** BP04, BP06, BP08, M01.

## 2. Intentional Behavior Changes

### BC01: Claim One Active Response Atomically And Repair Stale State On Read

**Goal:** Prevent concurrent responses for one conversation and make stale
state self-healing during rendering.

**Exact scope:** Use the M02 database invariant and a repository claim operation
to atomically insert the user and pending assistant messages. Return a conflict
without persisting the losing user message. Mark responses older than ten
minutes interrupted before render and before mutations.

**Likely files/modules involved:**

- `app/repositories/coach.py`
- `app/services/coach/conversation.py`
- `app/routes/coach.py`
- `tests/test_coach_characterization.py`

**Behavior that must remain:** Ten-minute threshold, one active response,
completed history filtering, user scoping, and durable accepted submissions.

**Acceptance criteria:**

- Two simultaneous submissions start exactly one provider execution.
- The losing request returns a stable conflict and leaves no orphan user row.
- Loading a conversation repairs and displays a stale response as incomplete.

**Verification/tests:** Add a file-backed SQLite concurrency test; run `uv run
pytest tests/test_coach_characterization.py tests/test_training_agent.py`.

**Dependencies on previous tasks:** BP10, M02.

### BC02: Propagate One Authoritative Coaching Date Per Turn

**Goal:** Ensure all analytics, proposal, adaptation, and planning decisions in a
turn use one process-local calendar date.

**Exact scope:** Resolve the date once at answer start and require it in trusted
runtime context and called services. Remove implicit `date.today()` from Coach
turn paths only; this task does not introduce user timezones.

**Likely files/modules involved:**

- `app/routes/coach.py`
- `app/services/coach/conversation.py`
- `app/services/coach/tools.py`
- `app/services/planning/workout_proposals.py`
- `app/services/planning/daily_adaptation.py`
- `app/services/planning/weekly_planner.py` and `multiweek_planner.py`

**Behavior that must remain:** Process-local date policy, trusted runtime
authority, historical query bounds, and current non-Coach route date behavior.

**Acceptance criteria:**

- No service called in a Coach turn resolves a second implicit current date.
- Persisted generation context and warning evidence use the turn date.
- Crossing midnight during a fake request cannot produce mixed dates.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_workout_proposals.py tests/test_daily_adaptation.py
tests/test_weekly_planner.py tests/test_multiweek_planner.py`

**Dependencies on previous tasks:** BP03, BP04.

### BC03: Make Completion And Failure Terminal Outcomes Explicit

**Goal:** Persist one comprehensible terminal assistant state for provider
failure, interruption, or missing final answer text.

**Exact scope:** Record privacy-safe failure categories on the message; map
provider errors and empty completion to failed; map disconnects to interrupted;
emit a terminal local failure event with concise retryable German copy; preserve
already committed artifacts and discard partial prose.

**Likely files/modules involved:**

- `app/services/coach/conversation.py`
- `app/repositories/coach.py`
- `app/routes/coach.py`
- `app/static/js/coach.js`
- `tests/test_coach_characterization.py`

**Behavior that must remain:** No provider internals in UI/logs, no failed
partial prose in history, durable valid artifacts, and completed prose reload.

**Acceptance criteria:**

- Every started execution reaches completed, failed, or interrupted durably.
- Missing final text cannot become an empty completed answer.
- Live and reloaded failure output is the same concise German state.

**Verification/tests:** `node --test tests/coach_sse.test.mjs`; `uv run pytest
tests/test_coach_characterization.py tests/test_training_agent.py`.

**Dependencies on previous tasks:** BP06, BP09, BP10, M01.

### BC04: Bound Rendered Conversation History

**Goal:** Prevent unbounded page payloads and repeated artifact queries while
keeping older messages explicitly accessible.

**Exact scope:** Load a bounded newest message window in chronological order,
eager-load its artifact projections, and add a user-scoped older-message page or
cursor control. This display window is separate from the 20-message/12,000-
character model-history contract.

**Likely files/modules involved:**

- `app/repositories/coach.py`
- `app/routes/coach.py`
- `app/templates/coach.html`
- `app/templates/coach/_message.html`
- `tests/test_coach_characterization.py`

**Behavior that must remain:** Conversation order, selected conversation,
active/incomplete display, artifact visibility, model history bounds, and
cross-user isolation.

**Acceptance criteria:**

- Default render executes a bounded query count independent of conversation
  length.
- Paging older messages has no gaps or duplicates.
- The newest window always contains the current active or terminal response.

**Verification/tests:** `uv run pytest tests/test_coach_characterization.py
tests/test_training_agent.py`

**Dependencies on previous tasks:** BP08, BP09, BC01.

### BC05: Add Validated Planning-Input Commands

**Goal:** Provide planning-owned commands for goals, profile, availability, and
performance anchors.

**Exact scope:** Add user-scoped create/update/deactivate commands with typed
inputs and explicit transaction ownership. Require artifact-level confirmation
before replacing or deactivating a goal referenced by an accepted cycle. Free
notes remain non-executable.

**Likely files/modules involved:**

- `app/services/planning/planning_commands.py` (new)
- `app/services/planning/planning_queries.py`
- `app/models/planning.py`
- `tests/test_athlete_planning_inputs.py`

**Behavior that must remain:** Existing model constraints, accepted-cycle goal
references, immutable plan/cycle revisions, user scoping, and Plans route
behavior.

**Acceptance criteria:**

- Clear structured changes persist and return deterministic summaries.
- Invalid dates, unavailable fields, and cross-user IDs fail specifically.
- Referenced-goal replacement requires an explicit exact artifact command.

**Verification/tests:** `uv run pytest tests/test_athlete_planning_inputs.py
tests/test_multiweek_planner.py tests/test_routes.py`

**Dependencies on previous tasks:** BP03.

### BC06: Expose Planning Input Commands As Conversation Artifacts

**Goal:** Let users read and update goals, profile, availability, and anchors
through validated conversational operations with visible durable results.

**Exact scope:** Add bounded read/update operation schemas over BP03/BC05.
Model-visible schemas contain only user choices. Render successful goal/profile/
availability/anchor changes as concise server-owned artifacts. A materially
ambiguous value returns one focused clarification result and persists nothing.

**Likely files/modules involved:**

- `app/services/coach/operations.py` (new, or reduced `tools.py`)
- `app/services/coach/provider.py`
- `app/services/coach/presentation.py`
- `app/services/planning/planning_commands.py`
- `tests/test_training_agent.py`

**Behavior that must remain:** Trusted runtime context, deterministic planning
commands, no fabricated values, explicit referenced-goal confirmation, and no
autonomous/background changes.

**Acceptance criteria:**

- Unambiguous changes persist; materially ambiguous changes persist nothing and
  produce one focused question.
- Operation schemas cannot supply runtime authority.
- Live and reloaded goal/profile/availability/anchor results use the same
  user-scoped artifact projection.
- Every mutation has authentication, onboarding, CSRF, rate-limit, and cross-
  user isolation coverage.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_production_hardening.py tests/test_athlete_planning_inputs.py`

**Dependencies on previous tasks:** BP03, BP06, BP08, BP09, BP10, BC02, BC05.

### BC06A: Select Adaptive Context And Apply Focused Uncertainty Policy

**Goal:** Make material recommendations use the relevant durable coaching state
and change when that state changes.

**Exact scope:** Add a question-specific bounded context selector across goals,
profile, plans, scheduled/completed work, progress, recovery/health/load,
coverage, and effective feedback. Provider instructions identify important
evidence and assumptions, proceed on non-material assumptions, and request one
focused clarification only when the answer could materially alter advice or
executable content.

**Likely files/modules involved:**

- `app/services/coach/conversation.py`
- `app/services/coach/operations.py`
- `app/services/coach/provider.py`
- `app/services/analytics/athlete_data.py`
- `tests/test_training_agent.py`

**Behavior that must remain:** Selected/bounded dated data, missing-value
honesty, deterministic analytics, no raw context dump, no second model review,
and no broad refusal for supported training questions.

**Acceptance criteria:**

- The same material question receives changed relevant context after a goal,
  feedback, completed activity, or plan-progress change.
- Missing, partial, unsupported, and synchronized-empty data are distinguished
  and never converted to zero or evidence for a conclusion.
- Deterministic tests cover context selection and focused-question outcomes; a
  small optional configured-model evaluation covers answer usefulness and does
  not replace CI.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_coach_characterization.py tests/test_progress.py
tests/test_athlete_trends.py`; run the documented manual evaluation only when a
provider is intentionally configured.

**Dependencies on previous tasks:** BC06, BC07, BC08.

### BC07: Add A Deterministic Progress Read Model And Coach Operation

**Goal:** Compare planned work and goals with observed activities and feedback
without relying on model memory.

**Exact scope:** Add an analytics-owned typed result for period, planned versus
completed sessions/volume, adherence where defensible, goal date/plan phase,
matched and unmatched work, feedback/interruptions, synchronization coverage,
linkage confidence, recent trend, consistency, recovery constraints, and
uncertainty; expose it as a bounded Coach read operation.

**Likely files/modules involved:**

- `app/services/analytics/progress.py` (new)
- `app/services/analytics/athlete_data.py`
- `app/services/planning/planning_queries.py`
- `app/services/coach/operations.py`
- `tests/test_progress.py` (new)

**Behavior that must remain:** Source dates and units, missing versus confirmed
empty data, activity/workout ownership, deterministic calculations, and no
stored conversation score.

**Acceptance criteria:**

- Results expose coverage and uncertainty with matched/unmatched counts.
- Projected plan impact is never reported as observed progress.
- Goal, activity, feedback, or plan changes deterministically change later
  progress context where relevant.
- Missing recovery/trend/linkage data remains unavailable rather than becoming
  zero adherence, completion, or progress.
- The Coach read is bounded, authenticated/onboarded, and rejects cross-user
  planning, workout, activity, or feedback references.

**Verification/tests:** `uv run pytest tests/test_progress.py
tests/test_athlete_trends.py tests/test_training_agent.py`

**Dependencies on previous tasks:** BP02, BP03, BC02, BC06.

### BC08: Record Structured Feedback From Conversation

**Goal:** Persist unambiguous pre-session and post-session feedback and use it in
later guidance.

**Exact scope:** Expose validated feedback commands with exact user-owned
workout/activity identity. Map only explicit effort, feel, completion, pain,
illness, stopped reason, availability, and notes; ambiguous health statements
produce one focused question or remain prose without invented fields. Render a
successful feedback result as a concise server-owned artifact.

**Likely files/modules involved:**

- `app/services/planning/feedback_service.py`
- `app/services/coach/operations.py`
- `app/services/coach/presentation.py`
- `tests/test_feedback.py`
- `tests/test_training_agent.py`

**Behavior that must remain:** Existing field validation, Garmin/manual
precedence, user scoping, feedback deletion/export, and current effective-
feedback queries.

**Acceptance criteria:**

- Clear feedback is durable and appears in later context.
- Ambiguous pain/illness never produces invented structured severity.
- Cross-user workout/activity references and model-supplied authority fail.
- Live and reloaded feedback results use the same user-scoped artifact; the
  mutation has authentication, onboarding, CSRF, rate-limit, and isolation tests.

**Verification/tests:** `uv run pytest tests/test_feedback.py
tests/test_training_agent.py tests/test_production_hardening.py`

**Dependencies on previous tasks:** BP06, BP07B, BP08, BP09, BC02, BC06.

### BC09: Implement One Versioned Training-Fit Policy

**Goal:** Produce deterministic Normal, Caution, or Elevated advice from dated
personal evidence without making structural-validity decisions.

**Exact scope:** Add a typed assessment containing outcome, policy version,
evaluation time, effective workout date, warning codes, dated evidence,
coverage, feedback IDs, and an authoritative-input fingerprint. Elevated is
limited to explicit serious feedback or two independent severe, recent,
adequately sampled personal-baseline deviations.

**Likely files/modules involved:**

- `app/services/planning/training_fit.py` (new)
- `app/services/planning/safety_triage.py`
- `app/services/analytics/health_trends.py`
- `knowledge/constraints/safety.yaml`
- `tests/test_training_fit.py` (new)

**Behavior that must remain:** Deterministic source metrics, personal baselines,
dated evidence, effective feedback precedence, no diagnosis, and structural
validation independence.

**Acceptance criteria:**

- Missing data, one anomaly, ordinary poor recovery, mild illness, sparse
  history, and one low readiness score produce at most Caution.
- Serious explicit feedback or the required two severe signals can produce
  Elevated only within the specified two-day window and coverage minimums.
- Fingerprints change when authoritative health, feedback, revision, date, or
  policy inputs change.
- Future-dated sessions never use today's health as a same-day acknowledgement
  gate; ordinary poor recovery remains Caution for workouts and plans.

**Verification/tests:** `uv run pytest tests/test_training_fit.py
tests/test_feedback.py tests/test_knowledge_registry.py`

**Dependencies on previous tasks:** BP02, BP07B, BC02, BC08.

### BC10: Make Supported Workout Draft Generation Advisory

**Goal:** Create every registered, representable workout format regardless of
history depth, frequency, consistency, spacing, recovery, or health.

**Exact scope:** Consume BC09 in proposal/template generation; retain only
registered format, effective date, positive supported budget/default,
executable schema, ownership, and lifecycle requirements. Persist warnings and
alternatives separately from structural validation. A poorly fitting requested
format must not be silently replaced.

**Likely files/modules involved:**

- `app/services/planning/workout_templates.py`
- `app/services/planning/workout_proposals.py`
- `app/services/planning/workout_service.py`
- `app/services/planning/training_fit.py`
- `tests/test_workout_proposals.py`

**Behavior that must remain:** Deterministic template expansion, format
constraints, positive budgets, immutable revisions, idempotency, structural
validation, and unaccepted/unscheduled draft creation.

**Acceptance criteria:**

- Every registry format produces a draft under sparse-history fixtures.
- Former frequency, consistency, quality-spacing, recovery, health, and
  `SAFETY_STOP` gates produce warnings rather than creation failure.
- Unsupported formats, missing essential choices, invalid chronology/structure,
  and ownership failures still block specifically.
- Artifact warnings expose dated evidence, coverage, recommendation, and an
  alternative without hiding the requested draft.

**Verification/tests:** `uv run pytest tests/test_workout_templates.py
tests/test_workout_proposals.py tests/test_workout_revisions.py`

**Dependencies on previous tasks:** BP02, BC02, BC06A, BC09.

### BC11: Add Bounded Conversational Workout Revision

**Goal:** Let the Coach revise deterministic drafts and request replacements of
accepted workouts without authoring arbitrary steps.

**Exact scope:** Add bounded format-supported revision commands using exact
workout/revision identity. Draft edits need no confirmation. Editing accepted
content creates a new current revision while the prior accepted revision remains
executable until an explicit replacement command.

**Likely files/modules involved:**

- `app/services/planning/workout_proposals.py`
- `app/services/planning/workout_revision.py`
- `app/services/planning/workout_service.py`
- `app/services/coach/operations.py`
- `tests/test_workout_revisions.py`

**Behavior that must remain:** Immutable revisions, accepted-revision execution,
structural validation, exact ownership, idempotency, and separate scheduling/
Garmin actions.

**Acceptance criteria:**

- Supported bounded edits create a deterministic new revision.
- Unsupported edits return a concrete supported alternative and persist no
  arbitrary steps.
- Accepted content remains active until exact replacement confirmation.
- Conversational create/revise commands have authentication, onboarding, CSRF,
  rate-limit, runtime-authority, and cross-user isolation tests.

**Verification/tests:** `uv run pytest tests/test_workout_revisions.py
tests/test_workout_proposals.py tests/test_training_agent.py`

**Dependencies on previous tasks:** BP10, BC06, BC10.

### BC12: Enforce Fresh Acknowledgement On Consequential Local Actions

**Goal:** Require informed confirmation for Elevated same-day accept, schedule,
replace, or keep actions without making the workout invalid.

**Exact scope:** Reassess at command time; bind acknowledgement to user, exact
revision, effective date, policy version, fingerprint, and current local date;
record it atomically in the workout event with the first authorized transition.
Normal/Caution need no acknowledgement. Acknowledgement never invokes the
lifecycle action by itself.

**Likely files/modules involved:**

- `app/services/planning/workout_service.py`
- `app/services/planning/workout_revision.py`
- `app/services/planning/workout_views.py`
- `app/routes/workouts.py`
- `app/templates/workouts/detail.html`

**Behavior that must remain:** Explicit exact lifecycle commands, structural
validity, accepted revision checks, scheduling date checks, atomic local
transactions, and unchanged future-dated action behavior.

**Acceptance criteria:**

- Stale, cross-user, cross-revision, cross-date, policy-version, and fingerprint
  mismatches cannot authorize an action.
- Matching acknowledgement is reusable only for the same revision/date while
  inputs and the local date remain unchanged.
- New feedback/health, rescheduling, a new revision, or the next day triggers
  reassessment and, if Elevated, a new acknowledgement.
- UI and Coach artifacts show a distinct acknowledgement control followed by a
  separate exact lifecycle action; neither generic prose nor acknowledgement
  alone performs the action.
- Coach cards and detailed workout pages invoke the same authenticated
  lifecycle command boundaries.

**Verification/tests:** `uv run pytest tests/test_workout_revisions.py
tests/test_daily_adaptation.py tests/test_feedback.py tests/test_routes.py`

**Dependencies on previous tasks:** BC09, BC11.

### BC13: Enforce Fresh Acknowledgement Before Garmin Publication Or Push

**Goal:** Apply the same exact warning contract to delayed same-day external
actions while preserving unknown external outcomes.

**Exact scope:** Reassess before publish/push; atomically persist authorization
on the durable Garmin operation before network access; retain it on failure or
unknown outcome; record published/pushed state only from a known success.

**Likely files/modules involved:**

- `app/services/planning/workout_service.py`
- `app/services/garmin/workout_operations.py`
- `app/routes/workouts.py`
- `app/templates/workouts/detail.html`
- `tests/test_workout_service.py`

**Behavior that must remain:** Accepted and structurally valid content only,
per-account locks, pre-network durable attempts, idempotency, reconciliation,
and no blind retry of ambiguous mutations.

**Acceptance criteria:**

- A newly Elevated delayed action requests acknowledgement even if an earlier
  local action was Normal/Caution.
- Matching acknowledgement permits the explicit action without changing
  structural validity.
- Failed/unknown calls retain authorization and attempt evidence but never
  claim success.

**Verification/tests:** `uv run pytest tests/test_workout_service.py
tests/test_workout_revisions.py tests/test_feedback.py`

**Dependencies on previous tasks:** BP07C, BC09, BC12, M05.

### BC14: Make Daily Adaptation Advisory And Conversational

**Goal:** Present keep, rest, reduce-volume, and easy-replacement choices for
today's accepted scheduled run without forced rest from health/history gates.

**Exact scope:** Consume BC09 with the turn date; keep all structurally
representable choices available; retain non-increasing automatic load; leave
changed/replacement content unaccepted; expose the assessment and choices as a
server-owned Coach artifact whose controls call the existing adaptation
boundary.

**Likely files/modules involved:**

- `app/services/planning/daily_adaptation.py`
- `app/services/planning/workout_service.py`
- `app/services/coach/operations.py`
- `app/services/coach/presentation.py`
- `tests/test_daily_adaptation.py`

**Behavior that must remain:** Today's exact accepted/scheduled ownership,
deterministic candidates, no automatic load increase, explicit application,
unaccepted replacements, and Garmin reconciliation safeguards.

**Acceptance criteria:**

- Former `SAFETY_STOP` and health `CLARIFY` inputs leave all representable
  choices available with warnings.
- Elevated keep/replacement follows BC12; health uncertainty does not suppress
  draft choices.
- Chat prose alone applies no adaptation.
- The conversational operation has authentication, onboarding, CSRF,
  rate-limit, runtime-authority, and cross-user isolation coverage.

**Verification/tests:** `uv run pytest tests/test_daily_adaptation.py
tests/test_training_agent.py tests/test_workout_revisions.py`

**Dependencies on previous tasks:** BP08, BC02, BC09, BC12.

### BC15: Make Weekly Draft Generation Advisory And Explicitly Acceptable

**Goal:** Generate, revise, explain, and explicitly accept representable weekly
plans without a preview/review mode.

**Exact scope:** Require a start week and at least one persisted or supplied
availability slot; use BC09 warnings for sparse history, low frequency,
re-entry, recovery, and quality density; persist direct immutable drafts; add
exact current/accepted revision semantics using M04. An active goal is optional;
generation is direct with no shadow preview or second AI review. Child workouts
remain independent.

**Likely files/modules involved:**

- `app/services/planning/weekly_planner.py`
- `app/services/planning/weekly_plan_service.py`
- `app/routes/plans.py`
- `app/models/planning.py`
- `tests/test_weekly_plan_service.py`

**Behavior that must remain:** Deterministic composition and placement,
availability/chronology/structural checks, idempotency, immutable plan/workout
revisions, and no implicit workout acceptance/scheduling/Garmin action.

**Acceptance criteria:**

- Sparse or incomplete wearable history produces a valid draft plus explicit
  confidence and warnings with dated evidence, coverage, recommendation, and an
  alternative.
- Exact plan-revision acceptance is explicit and leaves child lifecycle state
  unchanged.
- A newer draft does not replace the accepted revision until accepted.

**Verification/tests:** `uv run pytest tests/test_weekly_planner.py
tests/test_weekly_plan_service.py tests/test_migrations.py`

**Dependencies on previous tasks:** BP03, BP07A, BC09, BC10, M04.

### BC16: Make Multiweek Draft Generation Representability-Based

**Goal:** Generate and revise cycles from a goal or explicit general purpose
without treating sparse history or aggressive targets as eligibility failures.

**Exact scope:** Require chronological start/target dates, recurring
availability, and a goal or explicit purpose. Convert history, re-entry,
aggressiveness, horizon, and quality-density rejection to warnings where a
structurally valid cycle remains representable. Preserve explicit exact cycle
acceptance.

**Likely files/modules involved:**

- `app/services/planning/multiweek_planner.py`
- `app/services/planning/weekly_plan_service.py`
- `app/services/planning/planning_queries.py`
- `app/services/planning/training_fit.py`
- `tests/test_multiweek_planner.py`

**Behavior that must remain:** Phase progression, taper, interruption handling,
target boundaries, deterministic weekly composition, idempotency, immutable
revisions, and independent child workout lifecycle.

**Acceptance criteria:**

- Goal and explicit-purpose paths both generate representable drafts.
- Sparse/re-entry/aggressive fixtures produce assumptions, confidence, warnings,
  and alternatives instead of history-only refusal.
- Invalid chronology, missing recurring availability/purpose, ownership, or
  structural failure still blocks specifically.

**Verification/tests:** `uv run pytest tests/test_multiweek_planner.py
tests/test_weekly_planner.py tests/test_weekly_plan_service.py`

**Dependencies on previous tasks:** BP03, BP07A, BC09, BC15.

### BC16A: Add Bounded Weekly And Cycle Revision Commands

**Goal:** Let users revise deterministic plan drafts without editing generated
memberships or workout steps arbitrarily.

**Exact scope:** Add exact plan/cycle revision commands for supported date,
purpose, availability, target, and planner-input changes. Regenerate through the
deterministic weekly/multiweek planners, persist an immutable current revision,
and leave the accepted revision active until explicit acceptance. Explain
unsupported edits without partial mutation.

**Likely files/modules involved:**

- `app/services/planning/planning_commands.py`
- `app/services/planning/weekly_planner.py`
- `app/services/planning/weekly_plan_service.py`
- `app/services/planning/multiweek_planner.py`
- `tests/test_multiweek_planner.py`

**Behavior that must remain:** Deterministic composition, immutable revisions,
idempotency, accepted-revision identity, user ownership, structural validity,
and independent child workout acceptance/Garmin lifecycle.

**Acceptance criteria:**

- A supported bounded change creates a deterministic new current revision.
- An unsupported, ambiguous, unauthorized, or structurally invalid revision
  persists nothing and returns a concrete reason.
- Previously accepted weekly/cycle content remains accepted until the exact new
  revision is explicitly accepted.

**Verification/tests:** `uv run pytest tests/test_weekly_planner.py
tests/test_weekly_plan_service.py tests/test_multiweek_planner.py`

**Dependencies on previous tasks:** BC15, BC16.

### BC17: Expose Weekly And Cycle Drafts As Conversational Artifacts

**Goal:** Let one answer create, revise, explain, and display weekly or cycle
drafts through deterministic services.

**Exact scope:** Add bounded generation and revision operations over
BC15/BC16/BC16A, persist source assistant message references from M03, and
render server-owned cards with exact revision links and acceptance controls. One
message may display several artifacts. Explanations identify material planning
evidence, assumptions, warnings, confidence, and unsupported choices.

**Likely files/modules involved:**

- `app/services/coach/operations.py`
- `app/services/coach/presentation.py`
- `app/services/planning/weekly_plan_service.py`
- `app/services/planning/multiweek_planner.py`
- `app/templates/coach/_message.html`

**Behavior that must remain:** Explicit plan/cycle acceptance, no child workout
acceptance or Garmin action, user scoping, CSRF, idempotency, and artifact
survival after later provider failure.

**Acceptance criteria:**

- Direct conversational generation persists and reloads the same artifact.
- Cross-user plan/cycle IDs do not render or mutate.
- Chat prose cannot accept a plan/cycle; controls target the exact revision.
- Coach cards and detailed Plans pages call the same exact-revision planning
  command boundaries.
- Supported revisions regenerate deterministic artifacts; unsupported revisions
  are explained without mutation.
- Warning cards expose dated evidence, coverage, recommendation, alternative,
  and confidence; generation/revision commands have authentication, onboarding,
  CSRF, rate-limit, runtime-authority, and cross-user tests.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_weekly_plan_service.py tests/test_multiweek_planner.py tests/test_routes.py`

**Dependencies on previous tasks:** BP09, BP10, BC06, BC06A, BC15, BC16,
BC16A, M03, M04.

## 3. Deletion And Deprecation

### DD01: Inventory Removed Deployment Contracts

**Goal:** Identify deployment-specific consumers before deleting internal routes
and environment flags.

**Exact scope:** Search deployment scripts, Compose configuration, documented
automation, and operational runbooks for the direct proposal routes,
`/coach/planning-shadow`, removed Coach flags, and internal proposal APIs. Record
the canonical replacement or explicitly record that no consumer exists.

**Likely files/modules involved:**

- `compose.yaml`
- `.env.example`
- `README.md`
- deployment/runbook files found during implementation
- `docs/refactoring/ai-coach-plan.md` or a linked deployment checklist

**Behavior that must remain:** Provider credentials, Coach rate limiting, and
concrete external-service incident controls remain configurable.

**Acceptance criteria:**

- Every removed route/variable has a deployment disposition.
- Any real automation is moved before the corresponding deletion task.
- No compatibility parser or alias is proposed for unused internal contracts.

**Verification/tests:** Repository search for all removed names; review the
deployment checklist before DD02, DD03, DD06A, or DD06B starts.

**Dependencies on previous tasks:** None.

#### DD01 deployment disposition

Repository inventory covered `compose.yaml`, `.env.example`, the active README
deployment/configuration guidance, historical Coach rollout documents, `.github/`,
`scripts/`, `Dockerfile`, and `justfile`. No deployment automation or operational
runbook calls a removed route or internal proposal API. No automation outside the
application reads the removed Coach flags; their deployment consumers are Compose
forwarding, the environment example, and README configuration guidance.

| Contract | Current consumer | Canonical replacement | Migration required before deletion |
| --- | --- | --- | --- |
| `POST /coach/workout-proposals/running` | The Coach-page form posts to it; route tests and historical proposal docs reference it. No deployment automation consumer was found. | Conversational creation through `POST /coach/{conversation_id}/messages`; detailed artifact management remains in the normal workout UI. | DD02 removes the form, handler, route tests, and active documentation together. No route alias or deprecation period. |
| `POST /coach/workout-proposals/easy-run` | No production caller was found; it is an alias exercised by tests and described by historical proposal docs. No deployment automation consumer was found. | The same conversational creation path as every supported workout format. | DD02 deletes the unused alias and its tests. Do not redirect or retain an alias. |
| `GET /coach/planning-shadow` | Linked by the Coach page, the cycle-generation error state, and its own week navigation; route tests and historical planning docs reference it. No deployment automation consumer was found. | Weekly and cycle drafts are created in conversation and managed in the Plans UI through the existing planning services. | DD03 removes all links, the route/template/presentation policy, and its tests together. No plan data migration. |
| `EasyRunProposalRequest` | No direct production request consumer was found; `RunningProposalRequest` inherits its fields and tests instantiate it directly. | `RunningProposalRequest` is the canonical deterministic request. | DD02 defines the canonical request without the compatibility base and migrates/removes coupled tests. No compatibility type alias. |
| `RunningProposalService.create_easy_run()` | Tests only; production callers use `RunningProposalService.create()`. | `RunningProposalService.create(RunningProposalRequest, ...)`. | DD02 migrates surviving tests to `create()` and deletes the wrapper. No method alias. |
| `COACH_WORKOUT_PROPOSALS_ENABLED` | Forwarded by Compose and documented in `.env.example`/README; runtime consumers gate Coach tools, proposal creation, lifecycle actions, and presentation. Historical phase 8/9 docs describe rollout. | No replacement variable. Conversational deterministic proposal creation is part of the coherent Coach capability, with normal ownership, validation, and workout lifecycle safeguards. | The corresponding DD06 deletion task removes deployment forwarding/docs and runtime gates before deployment; operators delete the variable from environment files. |
| `COACH_GARMIN_SYNC_ENABLED` | Forwarded by Compose and documented in `.env.example`/README; runtime consumers gate generated-workout Garmin actions. Historical phase 8 docs describe rollout. | No replacement variable. Explicit accepted-revision Garmin actions use the existing publish/push safeguards and concrete Garmin incident controls. | The corresponding DD06 deletion task removes deployment forwarding/docs and generated-only gates before deployment; operators delete the variable. |
| `COACH_DAILY_ADAPTATION_ENABLED` | Forwarded by Compose and documented in `.env.example`/README; runtime consumers gate adaptation routes/services and generated-workout lifecycle actions. Historical phase 10 docs describe rollout. | No replacement variable. Daily adaptation is a supported conversational capability backed by the deterministic adaptation service. | The corresponding DD06 deletion task removes deployment forwarding/docs and runtime gates before deployment; operators delete the variable. |
| `COACH_PLAN_GENERATION_ENABLED` | Forwarded by Compose and documented in `.env.example`/README; runtime consumers gate Plans routes, planning shadow, lifecycle actions, and presentation. Historical phase 11/12 docs describe rollout. | No replacement variable. Plan creation is available in conversation and plan management remains in the Plans UI. | The corresponding DD06 deletion task removes deployment forwarding/docs and runtime gates before deployment; operators delete the variable. DD03 independently removes planning shadow. |
| `COACH_ROLLOUT_USER_IDS` | Forwarded by Compose and documented in `.env.example`/README; `coach_feature_enabled()` applies it to every per-capability gate. Historical phase 13 docs describe rollout. | No replacement variable. Authenticated user scope, onboarding, ownership, and lifecycle checks remain the access boundary. | The corresponding DD06 deletion task removes deployment forwarding/docs, the parser/helper policy, and tests before deployment; operators delete the variable. No compatibility parser. |
| `COACH_PLANNER_HISTORY_GATES_ENABLED` | Forwarded by Compose and documented in `.env.example`/README; configuration validation and proposal/weekly planning enforce it. | No replacement variable. History, frequency, consistency, and spacing signals become non-blocking training-fit warnings. | The corresponding DD06 deletion task removes deployment forwarding/docs, validators, hard gates, and gate-specific tests before deployment; operators delete the variable. No compatibility parser. |
| `COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED` | Forwarded by Compose and documented in `.env.example`/README; the development-only helper affects proposal, workout, weekly, and multiweek template availability. | No replacement variable. Every supported format uses the canonical template registry without a development bypass. | The corresponding DD06 deletion task removes deployment forwarding/docs, validators/helper, availability branches, and flag-specific tests before deployment; operators delete the variable. No compatibility parser. |

Deployment controls explicitly outside this removal remain unchanged:

- `LLM_API_KEY`, `LLM_MODEL`, and `LLM_TIMEOUT_SECONDS` keep provider
  availability configurable.
- `COACH_RATE_LIMIT_PER_MINUTE` keeps Coach mutation rate limiting configurable.
- Garmin credentials, cooldown, stale-operation handling, serialization, and
  reconciliation remain the concrete external-service controls and safeguards.

Deletion-task gate checklist:

- [x] DD01 repository inventory completed; DD01 has no predecessor dependency.
- [x] No deployment automation migration is required for DD02 or DD03.
- [ ] Before DD02, remove each in-application direct-proposal consumer in the
  same change as its route/API deletion.
- [ ] Before DD03, remove each in-application planning-shadow link in the same
  change as the route/template deletion.
- [ ] Before DD06A or DD06B starts, confirm that task's removed variables are
  covered by this inventory. Before each deletion deploys, remove those variables
  from Compose, `.env.example`, active README guidance, runtime
  settings/consumers, and flag-specific tests; update historical rollout
  documents where they could be mistaken for current operational guidance.
- [ ] Re-run a repository search for every removed name in the corresponding
  deletion task. Do not add compatibility aliases or parsers for zero-consumer
  contracts.

### DD02: Remove The Direct Coach Proposal Form And Easy-Run Compatibility API

**Goal:** Leave conversation as the Coach workout-creation entry point.

**Exact scope:** Delete the structured form, form state/error translation,
`POST /coach/workout-proposals/running`, `/easy-run`, `EasyRunProposalRequest`,
and `RunningProposalService.create_easy_run()`. Keep canonical deterministic
proposal services and normal workout management.

**Likely files/modules involved:**

- `app/routes/coach.py`
- `app/templates/coach.html`
- `app/services/planning/workout_proposals.py`
- `tests/test_workout_proposals.py`

**Behavior that must remain:** Existing workouts and revisions, conversational
creation for every supported format, detailed workout pages, explicit lifecycle
actions, CSRF, and user isolation.

**Acceptance criteria:**

- Deleted routes return 404 and no template links/form remain.
- Production code contains no easy-run compatibility request or service method.
- Canonical proposal and conversational tests cover the surviving path.

**Verification/tests:** `uv run pytest tests/test_workout_proposals.py
tests/test_training_agent.py tests/test_routes.py`

**Dependencies on previous tasks:** DD01, BC10, BC11.

### DD03: Remove Coach Planning Shadow

**Goal:** Eliminate the duplicate read-only weekly-plan mode and Coach-owned
planning presentation policy.

**Exact scope:** Delete `/coach/planning-shadow`, its template, navigation,
Coach-owned goal/role labels, generation-context decoding, related error links,
and route tests. Keep canonical planning generation and Plans management pages.

**Likely files/modules involved:**

- `app/routes/coach.py`
- `app/templates/coach/planning_shadow.html` (delete)
- `app/templates/coach.html`
- `app/templates/plans/cycle_new.html`
- `tests/test_routes.py`

**Behavior that must remain:** Weekly/cycle data, deterministic planners,
conversation generation, Plans UI management, exact acceptance, and all child
workout state.

**Acceptance criteria:**

- The shadow route returns 404 and no navigation/error link targets it.
- Coach routes contain no weekly generation-context presentation policy.
- Plans and conversational generation remain fully covered.

**Verification/tests:** `uv run pytest tests/test_routes.py
tests/test_training_agent.py tests/test_weekly_plan_service.py
tests/test_multiweek_planner.py`

**Dependencies on previous tasks:** DD01, BC17.

### DD04A: Remove Tool Activity Events, Persistence, And Dead SSE Fields

**Goal:** Stop exposing provider tool execution as an application event or
durable product record.

**Exact scope:** Stop emitting/persisting tool start/finish events; remove
`run.started`, unused `run_id`, tool labels/input summaries, persistence
sessions, and exact provider event-order tests. Keep the table/model temporarily
for M06 and leave browser timeline deletion to DD04B.

**Likely files/modules involved:**

- `app/services/coach/provider.py`
- `app/routes/coach.py`
- `app/repositories/coach.py`
- `tests/test_training_agent.py`

**Behavior that must remain:** Answer/artifact/completed/failed events, safe text
streaming, server-rendered artifacts, privacy-safe internal timing logs, and the
existing browser state until DD04B.

**Acceptance criteria:**

- Runtime creates no new `CoachToolCall` row.
- The server/provider local event contract contains only the four specified
  event meanings.
- Provider tool ordering and encodings are no longer route/test contracts.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_production_hardening.py`.

**Dependencies on previous tasks:** BP06, BC03.

### DD04B: Remove The Browser Tool Timeline

**Goal:** Show concise working/result/artifact/failure states instead of tool
rows, durations, and step counts.

**Exact scope:** Remove persisted and live tool rows, browser-only `error`
handling, activity labels, durations, expandable history, and unused timeline
animations. Keep answer text insertion and server-rendered artifact insertion.

**Likely files/modules involved:**

- `app/templates/coach/_message.html`
- `app/templates/coach.html`
- `app/static/js/coach.js`
- `app/static/css/tailwind.input.css` and generated `tailwind.css`
- `tests/test_static_theme.py`

**Behavior that must remain:** Safe text streaming, CSRF headers, explicit
terminal states, user-scoped artifact HTML, reload equivalence, and functional
desktop/mobile layout.

**Acceptance criteria:**

- No tool timeline appears live or after reload.
- Concise working, completed, failed, and incomplete states remain accessible.
- Browser parsing handles only answer/artifact/completed/failed events.

**Verification/tests:** `node --test tests/coach_sse.test.mjs`; `npm run
build:css`; update the stylesheet cache key; run `uv run pytest
tests/test_training_agent.py tests/test_static_theme.py`.

**Dependencies on previous tasks:** BP09, DD04A.

### DD05: Stop Using Contextual Validation Runs As Permission

**Goal:** Remove all runtime creation and lookup of reusable health-based
validity after fresh training-fit authorization is complete.

**Exact scope:** Stop writing/reading `WorkoutValidationRun` during proposal,
acceptance, scheduling, adaptation, publication, and push. Structural reports
stay on immutable revisions; fresh BC09 assessments and BC12/BC13 authorization
replace cache permission. Keep the table/model temporarily for M08.

**Likely files/modules involved:**

- `app/services/planning/workout_service.py`
- `app/services/planning/feedback_service.py`
- `app/services/planning/workout_views.py`
- `app/services/garmin/workout_operations.py`
- `tests/test_workout_revisions.py`

**Behavior that must remain:** Structural validation, accepted/current revision
identity, workout events, Garmin state/unknown outcomes, and fresh feedback/
health influence.

**Acceptance criteria:**

- No runtime command uses historical `valid` as authorization.
- Feedback changes alter fresh assessment fingerprints without updating a
  validation-run cache.
- Delayed Garmin commands use structural/lifecycle checks plus fresh fit only.

**Verification/tests:** `uv run pytest tests/test_feedback.py
tests/test_workout_revisions.py tests/test_workout_proposals.py
tests/test_workout_service.py tests/test_production_hardening.py`

**Dependencies on previous tasks:** BP07C, BC10, BC12, BC13, BC14, BC15, BC16.

### DD06A: Remove Permanent Capability And Rollout Flags

**Goal:** Make provider configuration the only normal Coach availability state
and expose every completed Coach operation to every configured user.

**Exact scope:** Remove proposal, generated-Garmin, adaptation, plan-generation,
and rollout-user settings/helpers/checks from dependency construction, routes,
services, templates, Compose, and environment examples. Do not remove Coach rate
limits, provider credentials, or concrete external-service incident controls.

**Likely files/modules involved:**

- `app/config.py`
- `app/services/coach/dependencies.py`
- `app/web.py`
- `app/routes/plans.py` and `app/routes/workouts.py`
- `.env.example` and `compose.yaml`

**Behavior that must remain:** Provider unavailable/available distinction,
explicit command authorization, deterministic capability constraints, and
external safety/unknown-state controls.

**Acceptance criteria:**

- Removed capability/rollout variables have no parser, helper, route, template,
  or service effect.
- Every completed Coach operation is available when the provider is configured.
- Generated workout Garmin operations still use normal lifecycle safeguards.

**Verification/tests:** Repository search for removed capability/rollout names;
`uv run pytest tests/test_config.py tests/test_training_agent.py
tests/test_routes.py tests/test_workout_service.py`.

**Dependencies on previous tasks:** DD01, BC10, BC13, BC14, BC15, BC16, BC17.

### DD06B: Remove History Gates And Deferred-Template Policy

**Goal:** Make all registered formats ordinarily supported while retaining
history, spacing, recovery, and health as advisory inputs.

**Exact scope:** Remove `COACH_PLANNER_HISTORY_GATES_ENABLED`,
`COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED`, deferred template helpers/metadata,
hard-gate branches, and development-only threshold/VO2 availability. Update
knowledge definitions and generation/persistence consumers without deleting the
underlying training signals.

**Likely files/modules involved:**

- `app/config.py`
- `app/services/planning/workout_templates.py`
- `app/services/planning/workout_proposals.py`
- `app/services/planning/weekly_planner.py`
- `knowledge/workouts/threshold_cruise.yaml` and `knowledge/workouts/vo2_intervals.yaml`

**Behavior that must remain:** Registered-format constraints, deterministic
composition, structural validation, advisory training-fit evidence, warnings,
alternatives, and explicit consequential actions.

**Acceptance criteria:**

- History/deferred variables and helpers have no runtime or deployment effect.
- Threshold and VO2 formats are available through the normal registry path.
- Sparse history, frequency, consistency, and spacing still produce relevant
  warnings rather than disappearing from context.

**Verification/tests:** Repository search for removed history/deferred names;
`uv run pytest tests/test_config.py tests/test_knowledge_registry.py
tests/test_workout_templates.py tests/test_workout_proposals.py
tests/test_weekly_planner.py tests/test_weekly_plan_service.py
tests/test_multiweek_planner.py`.

**Dependencies on previous tasks:** DD01, BC09, BC10, BC15, BC16.

### DD07: Remove Unused Analytics And Private Protocol Contracts

**Goal:** Delete compatibility wrappers and tests that no longer protect desired
behavior.

**Exact scope:** Remove identical/unused weekly volume/load forwarding methods,
obsolete tool-description helpers, old prompt/tool contract fixtures that encode
removed operations, and private framework/source-substring assertions. Update
current analytics documentation; retain historical plans as historical records.

**Likely files/modules involved:**

- `app/services/analytics/athlete_data.py`
- `app/services/coach/provider.py`
- `tests/test_athlete_trends.py`
- `tests/test_production_hardening.py`
- `docs/athlete-trends.md`

**Behavior that must remain:** Canonical analytics calculations and payloads,
runtime-authority protection, provider adapter behavior, current prompt/
operation version provenance, and all public behavioral tests.

**Acceptance criteria:**

- No production caller references removed wrappers/contracts.
- Behavioral adapter and authority tests replace private-shape contracts.
- Current docs name the canonical analytics boundary.

**Verification/tests:** `uv run pytest tests/test_athlete_trends.py
tests/test_training_agent.py tests/test_production_hardening.py`

**Dependencies on previous tasks:** BP02, BP06, DD04A, DD04B.

## 4. Migrations

### M01: Expand Assistant Message Execution And Artifact Lineage

**Goal:** Add the future execution owner and canonical artifact source without
destroying current run/origin data.

**Exact scope:** Add request ID, prompt/operation contract version, and
privacy-safe failure category columns to assistant messages; add a nullable
`source_assistant_message_id` to workouts; backfill message metadata and source
from consistent run/origin relationships; validate conversation/user/role/
workout ownership and fail on conflicts. Retain old run and origin columns for
dual-write cutover.

**Likely files/modules involved:**

- `migrations/versions/<new>_expand_coach_message_lineage.py`
- `app/models/coach.py`
- `app/models/workout.py`
- `app/models/__init__.py`
- `tests/test_migrations.py`

**Behavior that must remain:** Existing conversations, completed/failed
messages, workouts, immutable revisions, accepted revision, generation
metadata, plans, events, and Garmin state.

**Acceptance criteria:**

- Valid historical rows are backfilled deterministically.
- Conflicting ownership or role lineage aborts rather than silently linking the
  wrong artifact.
- Existing non-Coach/manual artifacts retain a null source.

**Verification/tests:** `uv run pytest tests/test_migrations.py`; migration test
from the current pre-refactor head with representative run/workout data.

**Dependencies on previous tasks:** BP01, BP04.

### M02: Add The Single-Active-Response Database Invariant

**Goal:** Make simultaneous streaming assistant messages unrepresentable for one
conversation.

**Exact scope:** Add the smallest SQLite-compatible partial unique index or
equivalent claim representation for assistant messages with active status.
Repair or reject invalid preexisting duplicate active rows deterministically in
the migration; do not rely on an in-process lock.

**Likely files/modules involved:**

- `migrations/versions/<new>_unique_active_coach_response.py`
- `app/models/coach.py`
- `tests/test_migrations.py`

**Behavior that must remain:** Multiple completed/failed/interrupted assistant
messages per conversation, exact status history, and user/conversation
ownership.

**Acceptance criteria:**

- The database rejects a second active assistant row in one conversation.
- Different conversations can execute concurrently.
- Upgrade handles a stale/duplicate fixture without losing completed prose.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_coach_characterization.py`

**Dependencies on previous tasks:** M01, BP10.

### M03: Add Source Assistant Message To Plan And Cycle Revisions

**Goal:** Preserve conversational provenance for plan artifacts without run IDs
or reverse artifact slots.

**Exact scope:** Add nullable user-safe source assistant-message references to
weekly-plan and cycle revisions, including ownership constraints feasible in
SQLite. Existing plan/cycle rows remain null; new conversational persistence
uses the columns atomically.

**Likely files/modules involved:**

- `migrations/versions/<new>_plan_message_lineage.py`
- `app/models/planning.py`
- `app/services/planning/weekly_plan_service.py`
- `app/services/planning/multiweek_planner.py`
- `tests/test_migrations.py`

**Behavior that must remain:** Existing plans/cycles, immutable revisions,
memberships, current/accepted cycle identity, child workouts, and account
deletion behavior.

**Acceptance criteria:**

- Existing rows survive with null provenance.
- New source links resolve to assistant messages owned by the artifact user.
- Deleting a conversation does not delete a plan/cycle artifact.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_weekly_plan_service.py tests/test_multiweek_planner.py`

**Dependencies on previous tasks:** M01, BP07A.

### M04: Add Accepted Revision Identity To Weekly Plans

**Goal:** Support explicit weekly-plan acceptance without conflating current
draft and accepted content.

**Exact scope:** Add nullable `accepted_revision_id` and same-plan ownership/
revision constraints to `training_plans`; existing rows remain unaccepted unless
current data already has an unambiguous accepted marker defined by current
behavior. Do not accept child workouts.

**Likely files/modules involved:**

- `migrations/versions/<new>_weekly_plan_accepted_revision.py`
- `app/models/planning.py`
- `app/services/planning/weekly_plan_service.py`
- `tests/test_migrations.py`

**Behavior that must remain:** Existing current revision, immutable revisions,
week uniqueness, memberships, and independent workout lifecycle.

**Acceptance criteria:**

- Accepted revision, when set, must belong to the same plan and user.
- Existing plans and cycle-week links survive migration.
- New drafts can become current without changing accepted identity.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_weekly_plan_service.py`

**Dependencies on previous tasks:** BP07A.

### M05: Add Durable Garmin Training-Fit Authorization Metadata

**Goal:** Persist exact informed authorization before an external network call.

**Exact scope:** Add nullable policy version, assessment fingerprint, effective
date, acknowledging user/time, and exact revision authorization fields to the
durable Garmin operation or its existing safe metadata representation. Do not
create a reusable acknowledgement table. Existing operations remain valid with
null metadata.

**Likely files/modules involved:**

- `migrations/versions/<new>_garmin_fit_authorization.py`
- `app/models/workout.py`
- `app/services/garmin/workout_operations.py`
- `tests/test_migrations.py`
- `tests/test_workout_service.py`

**Behavior that must remain:** Existing operation/attempt status, idempotency,
remote identities, schedules, unknown outcomes, reconciliation, and account
locks.

**Acceptance criteria:**

- Authorization can be committed before network access and retained on failed
  or unknown outcomes.
- Fields identify exact user/revision/date/policy/fingerprint.
- Existing Garmin rows and constraints survive upgrade.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_workout_service.py`

**Dependencies on previous tasks:** BC09.

### M06: Drop Coach Tool-Call Telemetry

**Goal:** Remove persistence that is neither coaching memory nor lifecycle
authority.

**Exact scope:** Drop `coach_tool_calls` after runtime writes and UI reads are
gone. No backfill is required. Align ORM metadata and migration head inventory
in the same implementation change.

**Likely files/modules involved:**

- `migrations/versions/<new>_drop_coach_tool_calls.py`
- `app/models/coach.py`
- `app/models/__init__.py`
- `app/services/account_lifecycle.py`
- `tests/test_migrations.py`

**Behavior that must remain:** Conversations, messages, execution outcomes,
artifacts, privacy-safe application logs/metrics, account deletion, and export
of all surviving user data.

**Acceptance criteria:**

- Upgraded databases contain no `coach_tool_calls` table.
- Conversation deletion and account deletion succeed.
- No production import/query/relationship references `CoachToolCall`.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_account_lifecycle.py tests/test_training_agent.py`

**Dependencies on previous tasks:** DD04A, DD04B.

### M07: Contract Assistant Runs And Redundant Workout Origins

**Goal:** Remove duplicated execution state and bidirectional origin graph after
message/source cutover.

**Exact scope:** Verify M01 backfill, then drop `coach_assistant_runs` and rebuild
SQLite `workouts` to remove originating conversation/user/assistant compatibility
columns while preserving canonical `source_assistant_message_id`. Recreate every
workout constraint, index, revision pointer, replacement relation, and Garmin
relationship.

**Likely files/modules involved:**

- `migrations/versions/<new>_drop_coach_runs_and_origins.py`
- `app/models/coach.py`
- `app/models/workout.py`
- `app/models/__init__.py`
- `tests/test_migrations.py`

**Behavior that must remain:** All conversations/messages, workouts/revisions,
accepted/current/materialized identities, schedules, replacements, plans,
events, generation metadata, and Garmin bindings/operations/attempts.

**Acceptance criteria:**

- Migration fails on unresolved/conflicting provenance before destructive DDL.
- Upgraded schema has one nullable workout source-message reference and no run
  table or redundant origin columns.
- One message can source multiple artifacts without a reverse singular slot.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_training_agent.py tests/test_workout_revisions.py
tests/test_workout_service.py`

**Dependencies on previous tasks:** BP10, M01, M03, M06.

### M08: Drop Contextual Validation Runs

**Goal:** Remove historical health-based permission rows after all runtime
consumers use fresh training-fit assessments.

**Exact scope:** Drop `workout_validation_runs`; align ORM metadata and expected
head tables. No backfill is required. Explicitly prove preservation of revision
structural reports, accepted/current identities, workout events, schedules,
replacement links, and all Garmin state.

**Likely files/modules involved:**

- `migrations/versions/<new>_drop_workout_validation_runs.py`
- `app/models/workout.py`
- `app/models/__init__.py`
- `app/services/account_lifecycle.py`
- `tests/test_migrations.py`

**Behavior that must remain:** Structural validation and all workout/plan/user
value, event provenance, external authorization, operation attempts, unknown
outcomes, and coordinated account cleanup.

**Acceptance criteria:**

- Upgraded schema contains no `workout_validation_runs` table.
- Migration preservation fixtures retain every listed workout and Garmin field.
- No application model or export inventory treats old contextual validity as
  authority.

**Verification/tests:** `uv run pytest tests/test_migrations.py
tests/test_account_lifecycle.py tests/test_workout_service.py`

**Dependencies on previous tasks:** DD05, M05, M07.

## 5. Cleanup After Migrations

### CM01: Remove Transitional Run And Origin Compatibility Code

**Goal:** Delete dual writes, fallback reads, repository helpers, fixtures, and
exports retained only for the M01-to-M07 migration window.

**Exact scope:** Remove `CoachAssistantRun` runtime/model references, old origin
arguments and fallback resolution, run-specific repository APIs, account export
enumeration, and test factories. Keep canonical message/source lineage only.

**Likely files/modules involved:**

- `app/repositories/coach.py`
- `app/services/coach/conversation.py`
- `app/services/coach/operations.py`
- `app/services/account_lifecycle.py`
- Coach/workout test factories

**Behavior that must remain:** Message-owned execution, artifact provenance,
idempotency, user scoping, conversation deletion without artifact deletion, and
all account cleanup.

**Acceptance criteria:**

- Repository search finds no `CoachAssistantRun`, run ID, or old originating
  column outside historical migrations/docs.
- New tests create artifacts using source assistant messages only.
- Export/deletion tests enumerate all and only surviving tables.

**Verification/tests:** `uv run pytest tests/test_training_agent.py
tests/test_account_lifecycle.py tests/test_migrations.py`

**Dependencies on previous tasks:** M07.

### CM02: Remove Validation-Run Cleanup And Observability Code

**Goal:** Eliminate cache invalidation, purge, export, deletion, and telemetry
logic that referred to the dropped validation table.

**Exact scope:** Remove feedback invalidation/purge methods, Garmin-account data
deletion references, validation-run repository/query helpers, and observability
metrics. Build decision traces from structural revision reports, fresh
assessment/acknowledgement metadata, workout events, and Garmin operations.

**Likely files/modules involved:**

- `app/services/planning/feedback_service.py`
- `app/services/garmin/account_data.py`
- `app/services/observability.py`
- `app/routes/observability.py`
- `app/services/account_lifecycle.py`

**Behavior that must remain:** Feedback CRUD/precedence, fresh fingerprint
changes, Garmin-data cleanup, account cleanup, privacy-safe observability, and
traceability of structural/lifecycle/external outcomes.

**Acceptance criteria:**

- Repository search finds no runtime validation-run query or invalidation.
- Observability distinguishes structural failure, advisory warning,
  acknowledgement, lifecycle transition, and unknown external outcome.
- Feedback changes immediately affect the next fresh assessment.

**Verification/tests:** `uv run pytest tests/test_feedback.py
tests/test_account_lifecycle.py tests/test_routes.py tests/test_workout_service.py
tests/test_production_hardening.py`

**Dependencies on previous tasks:** M08.

### CM03: Remove Migration-Era Fixtures And Obsolete Contract Tests

**Goal:** Leave tests focused on stable public behavior and current schema.

**Exact scope:** Retain migration fixtures only where needed to prove upgrades
from supported historical revisions; remove current-state factories for deleted
models, exact provider event-order tests, source-substring JavaScript tests,
history-gate rejection tests, and compatibility-route tests. Replace each with
the specified behavioral boundary where not already covered.

**Likely files/modules involved:**

- `tests/test_training_agent.py`
- `tests/test_production_hardening.py`
- `tests/test_static_theme.py`
- `tests/test_workout_proposals.py`
- `tests/test_migrations.py`

**Behavior that must remain:** All KEEP characterization, authority/isolation,
adaptive-loop, warning, explicit-action, migration-preservation, planner,
workout lifecycle, and Garmin unknown-outcome coverage.

**Acceptance criteria:**

- No current-schema test constructs deleted entities.
- Removed rejection expectations are replaced by draft-plus-warning assertions.
- Browser SSE/artifact behavior is executed rather than source-inspected.

**Verification/tests:** Run all focused commands in the specification, then
`uv run pytest tests/test_migrations.py`.

**Dependencies on previous tasks:** DD02-DD07, including DD04A/DD04B and
DD06A/DD06B, M06-M08, CM01, CM02.

### CM04: Final Architecture, Documentation, And Verification Pass

**Goal:** Prove the refactor satisfies the desired boundaries with no temporary
controls or dead authority left behind.

**Exact scope:** Update current product/privacy/configuration documentation;
review imports and route/service responsibilities; search for removed tables,
routes, flags, event fields, gates, and compatibility APIs; review the complete
diff once; run final verification. Historical phase documents remain historical
and need not be rewritten.

**Likely files/modules involved:**

- `README.md`
- `.env.example`
- `compose.yaml`
- `app/services/coach/__init__.py`
- `docs/athlete-trends.md` and current operational docs

**Behavior that must remain:** Every standing invariant at the top of this plan,
all durable user value, local date policy, supported deployment shape, and no
new dependency/process/framework.

**Acceptance criteria:**

- Coach routes contain HTTP/SSE concerns only; provider details stay in one
  adapter; planning/analytics/workout/Garmin do not depend upward on Coach.
- Removed capabilities have no remaining runtime, schema, config, UI, export,
  observability, or current-test authority.
- All specification acceptance criteria are covered by deterministic tests or
  an explicitly documented manual model evaluation where CI cannot judge answer
  usefulness.

**Verification/tests:**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Also run `npm run build:css` and update the stylesheet cache key if any template
or Tailwind-input change has not already rebuilt the committed asset.

**Dependencies on previous tasks:** All prior tasks.

## Checkpoints

### Checkpoint A: Refactoring Seams

After BP01-BP09, including BP07A-BP07C:

- KEEP characterization passes without private framework assertions.
- Provider construction is lazy and provider details are adapter-contained.
- Planning queries, presentation, and transaction boundaries are usable without
  changing product behavior.

### Checkpoint B: Expand And Cut Over

After M01-M05, BP10, and BC01-BC09, including BC06A:

- Assistant messages own execution and provenance.
- Atomic claim, stale repair, one-date propagation, and durable failures pass.
- Planning inputs, progress, feedback, and training fit are deterministic and
  user scoped.

### Checkpoint C: Adaptive Artifact Loop

After BC10-BC17, including BC16A:

- Workout, adaptation, weekly, and cycle drafts are available through
  conversation.
- Poor fit is advisory; essential structural and authority failures still stop.
- Exact local and Garmin actions enforce fresh informed acknowledgement where
  Elevated.

### Checkpoint D: Removal And Migration

After DD01-DD07, including DD04A/DD04B and DD06A/DD06B, and M06-M08:

- Duplicate form/shadow/timeline/flags are gone.
- Run, tool-call, and validation-run tables are gone with durable data preserved.
- Migration tests prove lineage, revision, event, plan, and Garmin preservation.

### Checkpoint E: Complete

After CM01-CM04:

- No transitional compatibility code remains.
- Full tests, lint, formatting, type checking, and required CSS build pass.
- The complete diff is ready for human review before merge.

## Parallelization Guidance

- BP02, BP03, BP05, and BP07A-BP07C can proceed in parallel after BP01 because they
  own separate contracts.
- BC05 and the read-model portion of BC07 can proceed in parallel with provider
  work once BP03 is stable.
- BC11 and BC14 form a workout dependency chain after BC09/BC10; BC15 and BC16
  form a separate planning chain over the same BC09 assessment contract.
- M03, M04, and M05 are independent expand migrations, but each must use the
  actual Alembic head when implemented; do not create divergent migration heads.
- Destructive migrations M06-M08 are sequential deployment checkpoints and must
  not be parallelized with their runtime cutovers.

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| SQLite concurrency behavior differs from in-memory tests | Two answers may still start | Use file-backed SQLite concurrency tests and a database invariant, not an in-process lock. |
| Provenance backfill links the wrong user's artifact | Privacy and integrity failure | Validate conversation owner, message roles, run links, and workout owner; abort on conflict. |
| Advisory conversion accidentally weakens structural or Garmin safeguards | Invalid/unconfirmed content reaches Garmin | Keep structural validation and lifecycle tests separate from training-fit tests; preserve exact revision commands. |
| Acknowledgement becomes reusable permission | Stale health warning authorization | Bind to user/revision/date/policy/fingerprint and commit only with the consequential transition/attempt. |
| Large route/tool edits obscure behavior changes | Review and regression risk | Land behavior-preserving seams first and one operation/artifact vertical slice per task. |
| Feature-flag removal precedes completed replacements | Capability gap or unsafe exposure | Make DD06A/DD06B depend on all advisory generation and conversational operation tasks. |
| UI has separate live/reload behavior | Inconsistent controls or unsafe rendering | Use one server presentation contract and executable SSE/parser tests. |
| Destructive SQLite rebuild drops constraints | Durable lifecycle corruption | Enumerate and assert every recreated constraint/index and preservation field in migration tests. |

## Open Implementation Checks

These are implementation checks, not unresolved product questions:

- Confirm deployment automation disposition in DD01 before route/config removal.
- Choose the exact bounded display window and cursor shape in BC04 while keeping
  model-history limits unchanged.
- Confirm whether existing weekly plans have any unambiguous accepted state
  suitable for M04 backfill; otherwise leave them unaccepted.
- Place Garmin authorization fields on the existing durable operation record in
  M05 unless inspection proves its metadata representation can enforce the same
  exact contract without a schema addition.
- Assign linear Alembic revision IDs at implementation time from the then-current
  head; never edit historical revisions.
