# AI Coach Refactor Specification

**Status:** Proposed. This document defines the desired future state and must be
approved before implementation planning begins.

## Authority And Scope

This specification translates `docs/refactoring/ai-coach-intent.md` into
observable behavior and responsibility boundaries. It uses
`docs/refactoring/ai-coach-current-state.md`, the current application, and the
characterization tests as evidence of the starting point. When they disagree:

1. this specification defines the desired future behavior;
2. the intent document defines the product direction;
3. application code and migrations define current behavior; and
4. characterization tests identify behavior that must be deliberately kept or
   changed rather than changed accidentally.

The AI Coach is one product capability: a continuous adaptive coaching
relationship. Data explanation, goals, feedback, progress, workout creation,
planning, and adaptation are parts of that relationship, not separately
deployed subsystems. A capability map and additional architecture layer would
therefore add ceremony without clarifying ownership.

## Objective

The future AI Coach gives an authenticated user one direct conversational entry
point for understanding personal training data and managing training. It uses
durable goals, plans, completed activity, recovery, and feedback to make useful
recommendations; turns bounded user requests into deterministic workout or plan
artifacts; and adapts later guidance as the athlete's state changes.

Success means that a user can complete this loop without changing Coach modes:

```text
goal + personal data + current plan
  -> recommendation or draft
  -> explicit user-controlled commitment where required
  -> completed training + feedback
  -> progress evaluation
  -> changed recommendation or plan
```

The refactor is not a general AI platform, a medical service, an autonomous
training scheduler, or a rewrite of analytics, planning, workout, and Garmin
domains that already provide useful deterministic behavior.

## Assumptions And Decisions

- PacePilot remains a Python 3.12, single-process FastAPI application with local
  SQLite, server-rendered German UI, and optional OpenRouter configuration.
- Multiple persisted conversations may remain, but all conversations for a user
  share the same durable coaching facts. Starting a new conversation does not
  reset goals, plans, progress, or feedback.
- The Coach is user initiated. It does not make background plan changes, publish
  workouts, or push to a device without an explicit request.
- Established workout templates and deterministic planning methods remain the
  source of executable training content. The model interprets intent and
  explains results; it does not invent unchecked workout steps.
- Training history, frequency, consistency, recovery, and health data inform the
  recommendation and its warnings; they do not determine whether a supported
  workout or training-plan draft is available.
- Draft creation and analysis do not require confirmation. Explicit commands
  are required for accepting a revision, local scheduling, replacing an
  accepted workout, committing a plan, and publishing or pushing to Garmin.
- Detailed workout and plan pages remain useful management and inspection
  surfaces. They are not separate Coach modes, and they must call the same
  commands as conversational artifact controls.
- A single request-scoped coaching date is resolved once from the application
  process's local calendar date and passed through all analytics and planning
  operations in that turn. This preserves the current deployment clock policy
  while removing within-turn disagreement. This refactor does not add a new
  per-user timezone model.

## Capability Classification

| Capability | Decision | Desired future-state summary |
| --- | --- | --- |
| Authentication, onboarding, and user isolation | **KEEP** | Every Coach read, command, conversation, and artifact is scoped to the authenticated, onboarded user. |
| Persistent conversation and streaming | **KEEP** | Questions and completed answers persist, answers stream, and failed partial answers are not reused as history. |
| Bounded conversational history | **KEEP** | The latest 20 completed prior messages, bounded to 12,000 characters, plus the current question form prose history. |
| Personal data questions and explanations | **KEEP** | Recovery, health, training, activities, feedback, and upcoming-workout data remain queryable with dates, units, coverage, and missingness intact. |
| Deterministic workout and planning methods | **KEEP** | Executable artifacts continue to come from validated templates and deterministic services rather than model-authored steps. |
| Workout acceptance, local scheduling, and Garmin safeguards | **KEEP** | Exact-revision acceptance, explicit local scheduling, and `draft -> confirmed -> published -> pushed` remain enforced; prose is never authorization. |
| Provider and streaming orchestration | **SIMPLIFY** | One short execution path adapts provider events into answer, state, and artifact events without exposing framework internals elsewhere. |
| Coaching data access and schemas | **SIMPLIFY** | Shared deterministic data contracts replace duplicate literals, wrappers, labels, and Coach-specific reconstructions. |
| Conversation execution persistence and lineage | **SIMPLIFY** | The assistant message owns execution state; artifacts point one way to their source message; duplicate run state disappears. |
| Proposal validation and workout orchestration | **SIMPLIFY** | Each invariant has one owner, and local lifecycle concerns are separated from Garmin transport and uncertain-outcome handling. |
| Coach HTTP and UI responsibilities | **SIMPLIFY** | Coach routes adapt HTTP/SSE only, while one presentation contract renders both live and persisted messages and artifacts. |
| Coaching configuration | **SIMPLIFY** | Provider availability and one coherent Coach capability replace scattered construction checks and capability policy. |
| Data-, goal-, plan-, and feedback-aware recommendations | **CHANGE** | Every material recommendation uses relevant durable context and changes when that context changes. |
| Goal and planning-profile interaction | **CHANGE** | The conversation can read and update goals, availability, and relevant planning preferences through validated commands. |
| Progress tracking | **CHANGE** | The Coach compares planned work and goals with observed activities and feedback and explains evidence gaps. |
| Structured feedback in conversation | **CHANGE** | User feedback can be recorded durably and is incorporated into later guidance and adaptations. |
| Workout creation and modification | **CHANGE** | The Coach can create and revise deterministic draft workouts, while accepted content remains active until a replacement is explicitly confirmed. |
| Training eligibility and health safeguards | **CHANGE** | History, frequency, load, recovery, and health signals become advice and warnings rather than hard workout/plan creation gates; genuinely severe personal-health signals require informed confirmation before a consequential same-day action. |
| Daily adaptation | **CHANGE** | The existing keep/rest/reduce/replace assessment is available in conversation and never increases planned load. |
| Weekly and multiweek planning | **CHANGE** | The Coach can create, explain, and revise plan drafts in conversation using the existing deterministic planners. |
| Uncertainty and clarification | **CHANGE** | The Coach asks one focused question only when the missing answer could materially change advice; otherwise it proceeds with explicit assumptions. |
| Date, concurrency, and failure behavior | **CHANGE** | A turn uses one date, only one answer can start atomically per conversation, and failures have a clear durable outcome. |
| Direct Coach workout-proposal form and alias | **REMOVE** | The duplicate structured form and its `/easy-run` compatibility endpoint disappear. |
| Coach planning-shadow preview | **REMOVE** | The duplicate read-only weekly-plan mode and its presentation policy disappear. |
| User-visible per-tool activity timeline | **REMOVE** | Internal tool start/finish rows and UI steps disappear; users see concise working, result, artifact, or failure states. |
| Separate assistant-run entity | **REMOVE** | `CoachAssistantRun` and its duplicated lifecycle and reverse workout link disappear after required provenance is preserved. |
| Persistent blocking contextual-validation runs | **REMOVE** | `WorkoutValidationRun` no longer stores reusable health-based permission; structural reports remain with revisions, while fresh fit assessments and exact acknowledgements are recorded with the consequential workout event. |
| Unused compatibility and protocol contracts | **REMOVE** | Unused proposal APIs, analytics wrappers, aliases, and dead SSE fields disappear with their tests. |
| Per-capability Coach rollout, hard-history-gate, and deferred-template flags | **REMOVE** | The future Coach exposes every supported format without rollout, training-frequency eligibility, or development-only availability flags. |

## KEEP Requirements

### Authentication And Data Isolation

- Coach routes require `CurrentUser` and completed onboarding.
- Conversation, analytics, planning, feedback, workout, and artifact lookups are
  user scoped at the query or command boundary, not filtered after loading.
- Unsafe requests retain session-bound CSRF protection and rate limiting.
- Trusted user ID, request ID, coaching date, database access, and artifact
  origin are supplied by the application and are never model-controlled tool
  arguments.
- Model-visible results never include credentials, token paths, raw file paths,
  sampled GPS tracks, or unbounded Garmin payloads.

### Conversation Behavior

- Users can create, select, and delete their own conversations.
- User messages are durable before provider execution starts.
- Completed assistant prose is durable and appears after reload.
- A conversation permits only one active assistant response.
- A response still active after ten minutes is treated as interrupted before a
  later response starts. The implementation must also repair this state when
  rendering the conversation, rather than only on a later mutation.
- Failed or interrupted partial assistant prose is neither persisted as a
  completed answer nor supplied to later model history. Reload shows a concise
  German incomplete-answer state.
- A deterministic artifact committed before a later provider failure remains
  durable and visible. The failure must not roll back valid training data.
- Prose history contains only completed user and assistant messages. It includes
  at most the latest 20 prior messages and 12,000 prior characters; when the
  oldest included message crosses the character boundary, only its newest
  suffix is included. The current user question is appended after this history.

These are the desired outcomes characterized by
`tests/test_coach_characterization.py`. The test may be reorganized around new
public boundaries, but these outcomes must not be lost during simplification.

### Personal Data Access

The Coach must retain bounded access to:

- current recovery state, including the distinction between Garmin Training
  Readiness and PacePilot Readiness;
- dated health trends and personal baselines;
- training summaries, load, frequency, consistency, and sport-specific volume;
- recent activities and an explicitly selected activity's bounded detail;
- effective subjective feedback;
- upcoming scheduled workouts; and
- synchronization coverage needed to distinguish missing, unsupported,
  unsynchronized, and confirmed-empty data.

Missing values are omitted or described as unavailable, never converted to
zero. Values retain source dates and units. The model may summarize or explain
deterministic results but must not recalculate Garmin metrics or fabricate
classifications.

### Deterministic Training Artifacts

- Workout and plan content comes from versioned knowledge, validated formats,
  athlete context, and deterministic planning services.
- Registered format constraints and structural validity continue to bound
  executable content. Recent history, training frequency, quality spacing,
  recovery, and health affect fit, assumptions, and warnings; they do not block
  a supported draft. An explicit time or volume budget remains an input to the
  requested artifact, not an eligibility judgment.
- Immutable workout, weekly-plan, and cycle revisions remain durable.
- Draft generation is idempotent for the same command identity where retries are
  possible.
- Editing an accepted workout creates a new current revision. The previously
  accepted revision remains executable until the user explicitly accepts the
  replacement.

### Consequential Actions And Garmin

- Chat text such as "ja", "mach das", or "sende es" is not by itself an
  acceptance, scheduling, publication, or push command.
- Confirmation is an explicit, authenticated command against a displayed
  artifact and exact revision. It may be initiated from a conversational card
  or the normal workout/plan page, but both use the same command boundary.
- Local scheduling remains an explicit command against an accepted revision and
  a displayed date. The Coach may present that command on an artifact, but it
  does not silently schedule a generated workout or plan member.
- Before an accept, schedule, replace, keep, publish, or push command affecting
  today's session, command orchestration obtains a fresh versioned training-fit
  assessment. If it reports an elevated personal-health concern, the Coach
  clearly recommends against or changing the session and, if no matching
  acknowledgement exists, asks whether the user still wants to proceed with
  that exact workout revision on that date.
- An informed acknowledgement is bound to the authenticated user, exact workout
  revision, effective workout date, assessment policy version, and assessment
  fingerprint. It remains usable for later explicit actions on that same
  revision and date only while the assessment fingerprint and local calendar
  date remain unchanged. New feedback, changed health inputs, a new revision,
  rescheduling, or the next day requires reassessment and, when still elevated,
  a new acknowledgement. It is recorded atomically with the first authorized
  state transition so stale or cross-revision replay cannot authorize an
  action.
- The acknowledgement applies to the warned-about session, not to a particular
  lifecycle action. Acceptance, scheduling, replacement, publication, and push
  still each require their own normal explicit command; acknowledgement of the
  health warning does not invoke or imply any of them.
- The elevated signal does not prevent draft creation, silently cancel existing
  work, or permanently prohibit the action after informed acknowledgement.
- Only accepted, structurally valid workout content can be published. Health,
  recovery, training history, and training-fit warnings never make an accepted
  revision structurally invalid. Only known published/calendar state can be
  pushed to a device.
- Garmin operations remain serialized per account and recorded before network
  calls where needed for recovery.
- Ambiguous external outcomes are preserved as unknown and are not blindly
  retried.
- Accepting a cycle does not implicitly accept, schedule, publish, or push its
  child workouts.

## SIMPLIFY Requirements

### Provider Execution

Provider-specific LangChain, LangGraph, and OpenRouter details must be contained
inside one adapter. The rest of the application consumes a small local event
contract:

```text
answer text | artifact available | completed | failed
```

Model node names, provider message dictionaries, legacy stream modes, and tool
result encodings are not application contracts. Status and timing logs may
remain, but prompts, answer text, and personal metric values remain excluded
from logs.

Rendering a Coach page or checking whether the Coach is configured must not
construct a model client or agent graph. Provider construction happens only
when an answer is requested.

### Coaching Context And Operations

The Coach should use the existing deterministic boundaries instead of parallel
Coach-only interpretations:

- `AthleteDataService` remains the bounded athlete-data read boundary.
- Planning-owned queries expose goals, profile, availability, current plans,
  and cycles without moving those models into `coach`.
- Analytics owns the derived progress read model and combines planning facts
  with observed activities and feedback without mutating either domain.
- Planning-owned commands validate goals, profile, feedback, proposal,
  adaptation, and plan-generation intent. Workout lifecycle commands alone
  persist workout revisions and perform acceptance, scheduling, replacement,
  and rejection transitions.
- Model-facing operation schemas contain only the user choices needed for the
  operation. Runtime authority remains injected by the application.

Duplicate template literals, health metric literals, goal/role labels, status
projection, and one-line analytics aliases must be replaced by one domain-owned
contract or deleted. Consolidation must not become one unbounded "load all
athlete data" payload: context remains selected, dated, and bounded to the
question.

### Persistence And Lineage

`CoachMessage` is the durable record of an assistant execution. It owns the
single status, model identifier, request identifier, prompt/operation contract
version, timestamps, and a privacy-safe failure category needed to understand
the result. The same lifecycle state must not be writable in a second entity.

Generated workouts and plans may retain one nullable source assistant-message
reference. User and conversation ownership are derived and validated through
that message and the artifact's own user ID. There is no reverse one-artifact
slot on a run, allowing one answer to present zero, one, or several artifacts
without bidirectional synchronization.

Durable records are for user value, lifecycle integrity, external-operation
recovery, or concise decision provenance. They are not a transcript of every
provider/framework event.

### Validation And Transactions

Each rule has one canonical owner:

- template selection belongs to proposal or planning generation;
- one planning/feedback-owned versioned policy produces training-fit assessments
  for generation, adaptation, and fresh action-time checks;
- structural workout validity and exact revision transitions belong to the
  workout lifecycle;
- Garmin request/retry/reconciliation rules belong to Garmin operations; and
- user and conversation ownership belong to authenticated command/query
  boundaries.

Calling a lower-level boundary may enforce its own invariant, but the same
training-fit/warning calculation or origin graph must not be rebuilt repeatedly
in one synchronous operation. Training-fit warnings are not recast as structural
validation failures at a lower boundary.

Transaction outcomes are explicit:

1. the user message and pending assistant message are created atomically;
2. each deterministic mutation commits its complete artifact and source link
   atomically;
3. for a local action, an elevated-warning acknowledgement and its exact
   authorized workout transition commit atomically after a fresh assessment;
4. for publish or push, the acknowledgement commits atomically with the durable
   external authorization/operation attempt before the network call; the final
   published/pushed transition is recorded separately only from the known
   external outcome, and an ambiguous result remains unknown; and
5. assistant completion or failure is one lifecycle update.

Repositories continue to query and mutate sessions without hidden commits.
The operation that defines an atomic outcome owns the transaction.

### HTTP And Presentation

`app/routes/coach.py` becomes an HTTP adapter for conversation CRUD, message
submission, streaming, and artifact projection. It does not parse planner
generation context, build weekly-plan presentation policy, implement workout
proposal rules, or reconstruct lifecycle labels.

Live and persisted assistant messages use one presentation contract for prose,
artifact cards, completion, and failure. The browser must not maintain a second
implementation of domain statuses, duration labels, or title rules. Long
conversation pages load a bounded recent window with explicit access to older
messages rather than eagerly rendering the full history.

### Configuration

Coach availability has two states: unavailable because the provider is not
configured, or available with the complete future-state coaching operation
set. Capability enforcement happens at command boundaries, not independently
in dependency construction, template context, routes, and lifecycle services.

Temporary deployment controls may exist during implementation, but they are not
part of the desired architecture and must not survive as permanent ownership or
policy boundaries.

## CHANGE Requirements

### Adaptive Coaching Context

Before making a material recommendation, the Coach selects the relevant subset
of:

- current goals and target dates;
- planning profile, availability, and performance anchors;
- accepted cycle and current weekly plan;
- scheduled and recently completed workouts;
- recovery, health, training load, consistency, and data coverage;
- recent pre-session and post-session feedback; and
- observed progress and interruptions.

The answer identifies the important evidence and assumptions in user-facing
language. It does not dump raw context or claim that missing data supports a
conclusion. A later turn with materially changed feedback, recovery, completed
training, goal, or plan state must be able to produce different guidance
without the user restating that information.

### Goals And Planning Inputs

The Coach can read and explain active and future goals, availability, experience
or re-entry status, and relevant performance anchors. It can create or update
those values from conversation through validated, user-scoped commands.

- A clear change can be stored directly and summarized back to the user.
- An ambiguous date, target, or availability change triggers one focused
  question before storage.
- Replacing or deactivating the goal used by an accepted cycle requires an
  explicit artifact-level confirmation and must respect existing reference
  integrity.
- Free-form notes are not executable policy. Only validated structured fields
  influence deterministic planning.

### Progress

Progress is a deterministic read model, not model memory and not a copied score
stored on every conversation. It compares, where data permits:

- planned versus completed sessions and volume;
- adherence and completion percentage;
- goal date and current plan phase;
- recent trend and consistency;
- feedback, pain, interruptions, and recovery constraints; and
- activity-to-workout linkage confidence.

The result exposes its period, source coverage, matched/unmatched work, and
material uncertainty. Projected plan impact is not reported as observed
progress. Incomplete Garmin linkage or synchronization is reported explicitly.

### Feedback

The conversation accepts pre-session availability/readiness feedback and
post-session effort, feel, completion, pain, stopped reason, and notes. The
Coach maps only unambiguous statements to existing structured feedback fields;
otherwise it asks one focused question or retains the statement as prose
without inventing structured values.

Stored feedback is visible in later context and invalidates affected
training-fit/warning or adaptation assessments through the existing feedback
service. Existing Garmin versus manual feedback precedence remains
deterministic.

### Advisory Training Safeguards

Workout and plan availability must be separated from coaching judgment. Every
registered workout format and deterministic plan generator remains available
when the user has supplied the essential choices needed to construct a
structurally valid artifact.

The following are coaching signals, not creation eligibility gates:

- fewer recent workouts, low weekly frequency, inconsistent weeks, or re-entry;
- missing or incomplete wearable history;
- a requested quality session close to another quality session;
- a large change from recent duration, volume, or intensity;
- an aggressive goal or short target horizon;
- poor recovery, sleep, HRV, resting heart rate, stress, or Body Battery; and
- subjective fatigue, poor feel, pain, or illness feedback.

These signals may change the recommended workout or plan, reduce confidence,
produce a prominent warning, or cause the Coach to propose a more conservative
alternative. They must not prevent the requested supported draft from being
created. In particular, no minimum based on population medians, twice-weekly
training, a fixed number of consistent weeks, or complete Garmin history may be
required to unlock a workout format or plan.

Warnings have three simple outcomes:

1. **Normal:** create the requested draft and explain its fit.
2. **Caution:** create the requested draft, explain why the data suggests it may
   be a poor fit, and offer a safer or more useful alternative.
3. **Elevated personal-health concern:** create the draft, clearly recommend
   changing or skipping the same-day session, identify the dated personal
   signals behind the concern, and require an explicit informed confirmation
   before accepting, scheduling, replacing, keeping, publishing, or pushing
   that session.

The three outcomes come from one deterministic, versioned training-fit policy,
not model discretion. Its typed result contains the outcome, policy version,
evaluation time and effective workout date, warning codes, dated evidence and
coverage, feedback IDs, and a fingerprint of all authoritative inputs. Proposal,
plan, adaptation, and action-time checks consume this same result.

An elevated concern is limited to:

- explicit current feedback for fever, systemic illness, cardiopulmonary warning
  signs, or pain that alters gait; or
- at least two independent health/recovery signals no older than two calendar
  days that each cross a severe metric-specific deviation from a sufficiently
  sampled personal baseline under the versioned policy.

Exact metric thresholds and minimum baseline samples live in the versioned
policy/knowledge constraints and are test fixtures. A single low readiness
score, one anomalous metric, ordinary poor recovery, mild illness, missing data,
low training frequency, or comparison with another athlete produces at most
**Caution**. Unclear illness or pain triggers one focused question; if the user
does not add detail, the supported draft remains available with a warning, and
a potentially serious same-day action uses informed acknowledgement rather than
a refusal. The Coach does not diagnose a medical condition.

Existing contextual `SAFETY_STOP` results become **Elevated** assessments, not
invalid workouts. Existing health/feedback `CLARIFY` results ask the focused
question described above but do not suppress a draft when the user declines to
answer. Proposal creation, plan generation, adaptation choices, acceptance,
scheduling, publication, and push must not convert either outcome into a
structural-invalid flag. Structural validation remains a separate result.

Representability and essential inputs are deliberately narrow:

- a single workout needs a registered format, an effective date, and a positive
  duration/volume supported by that format or explicit permission to use its
  documented default;
- a weekly plan needs a start week and at least one persisted or supplied
  availability slot; an active goal is optional;
- a multiweek plan needs start and target dates in chronological order, at least
  one recurring availability slot, and either a goal or an explicit general
  training purpose; and
- every artifact must satisfy its executable schema, ownership, and lifecycle
  invariants.

Creation may stop only when one of those inputs remains materially ambiguous
after one focused question, the requested format is unsupported, chronology or
the executable structure is invalid, or ownership/lifecycle invariants fail.
Representability never includes training frequency, history depth, consistency,
quality density or spacing, load progression, recovery, health, or target
aggressiveness. The Coach explains the exact missing or unsupported choice and
offers the nearest supported path.

### Workout Creation And Modification

The Coach can:

- create a dated, time-bounded running workout draft from every registered
  training format regardless of recent training frequency or history depth;
- explain why the selected session fits the athlete's goal, plan, recovery, and
  recent training, or why it is not currently recommended;
- revise an existing draft within the parameters supported by its format;
- request deterministic replacement or reduction of an accepted workout; and
- show the resulting revision and validation state as an artifact.

The model chooses intent and bounded parameters. Deterministic planning expands
the workout definition and validates it; the workout lifecycle persists the
resulting draft or revision. Unsupported edits are explained rather than
translated into arbitrary steps. A requested but poorly fitting supported
workout is still created with its warning and must not be silently replaced by
the Coach's preferred alternative. Draft creation and draft editing need no
confirmation. Replacing an accepted revision requires explicit confirmation;
an elevated same-day health warning also requires acknowledgement. Scheduling
an accepted revision requires an explicit date-bearing command; publishing and
pushing remain separate explicit actions.

### Daily Adaptation

For today's user-owned accepted and scheduled running workout, the Coach can
invoke the deterministic assessment and explain keep, rest, reduce-volume, or
easy-replacement outcomes. Health and history do not remove these choices. The
operation:

- uses the same request-scoped date and current feedback context as the answer;
- never increases target, volume, or weekly load as an automatic adaptation;
  the user may separately request a more demanding supported draft;
- creates no executable candidate only when the requested adaptation action or
  an essential structural input is ambiguous; health/feedback uncertainty asks
  one question but leaves the choices available with the applicable warning;
- leaves changed or replacement content unaccepted until explicit user action;
- keeps the current session available under an elevated health warning after
  explicit informed confirmation rather than forcing rest; and
- preserves current Garmin reconciliation and unknown-state safeguards.

### Weekly And Multiweek Plans

The Coach can generate and explain weekly and multiweek draft plans from the
active goal or requested purpose, profile, availability, history, and current
state. Once the essential dates, purpose where required, and availability
choices are known, sparse history, low frequency, incomplete wearable data, an
aggressive target, or re-entry status does not block a draft. Draft generation
is direct and does not require a separate preview screen or AI review pass.

The user can request bounded revisions through the same conversation. The
deterministic planner uses phase progression, taper, quality density, re-entry
behavior, interruptions, and target boundaries to produce its recommended
composition. Those methods generate warnings and alternatives rather than
rejecting a representable user-requested plan solely because it exceeds the
recommendation. Workout membership and structural plan validity remain
deterministic. Accepting a plan or cycle is explicit. Child workouts retain
their independent acceptance and Garmin lifecycle.

### Uncertainty And Refusal

The Coach distinguishes three outcomes:

1. proceed and state a non-material assumption;
2. ask one focused question because the answer could materially change advice
   or executable content; or
3. stop a structurally invalid, unauthorized, or invalid external operation and
   explain the concrete reason.

It does not use broad refusals for ordinary training questions, ask several
questions when one would unblock progress, or run a second model review to
simulate reliability. Medical emergencies and symptoms outside training advice
may still receive a concise appropriate boundary; that boundary must not grow
into a general-purpose safety framework. A workout or plan that is possible but
not recommended receives a warning under the advisory policy; it is not treated
as an invalid operation.

### Date, Concurrency, And Failures

- The route resolves the application process's local calendar date once at turn
  start and passes it to all data, proposal, adaptation, and planning calls. No
  called service resolves a second implicit `date.today()` for that operation.
- Starting an assistant response uses an atomic database claim so two concurrent
  submissions cannot both begin for one conversation.
- Stale responses are repaired on read and before mutation.
- Provider failure produces one durable failed assistant state, preserves
  already committed valid artifacts, and exposes a concise retryable German
  result without leaking provider internals.
- Missing final answer text is a failed or incomplete execution, not a completed
  empty answer.

## REMOVE Requirements

### Direct Workout-Proposal Form

Remove the Coach-page structured proposal form, its handler, the
`/coach/workout-proposals/running` endpoint, the `/easy-run` alias, form-specific
error translation, template state, and route tests. Conversational creation is
the Coach entry point; the normal workout UI remains the place for detailed
artifact management.

No data migration is required. Workouts previously created through either path
remain normal workout aggregates with immutable revisions.

### Planning Shadow

Remove `/coach/planning-shadow`, its template, Coach-owned labels and generation
context decoding, navigation, and tests. Weekly and cycle drafts are created in
conversation and managed in the Plans UI through the same planning services.

No plan data is deleted and no data migration is required.

### Tool Activity Timeline And Telemetry Rows

Remove the user-facing per-tool activity timeline and `CoachToolCall` persistence:

- `CoachToolCall` model, relationships, repository functions, and database table;
- tool-start/tool-finish SSE events and persistence sessions;
- tool labels, input summaries, durations, and failure rows shown in templates
  or JavaScript;
- account-export enumeration for the removed table; and
- tests coupled to exact provider tool-event ordering.

Keep only concise answer state, meaningful generated artifacts, privacy-safe
application logs/metrics, and durable external-operation records. A forward
Alembic migration drops `coach_tool_calls`; no user-data backfill is needed
because these rows are neither coaching memory nor lifecycle authority.

### Separate Assistant Runs And Duplicate Origin Graph

Remove `CoachAssistantRun`, duplicated message/run status updates, its singular
reverse `workout_id`, run-specific repository code, and route/tool dependencies
on run IDs. Remove redundant workout origin conversation and user-message links
after source provenance has one owner.

This requires a data-preserving migration:

1. add any missing request, prompt-version, or failure provenance to assistant
   messages;
2. backfill one source assistant-message reference for existing generated
   workouts from current origin/run relationships;
3. verify artifact and message user ownership during migration;
4. update account export and deletion coverage;
5. rebuild affected SQLite tables where foreign-key removal requires it; and
6. only then drop `coach_assistant_runs` and redundant origin columns.

Existing conversations, completed messages, workouts, revisions, generation
metadata, plans, and Garmin state must survive. Per-tool telemetry may be
discarded as specified above.

### Blocking Contextual Validation Runs

Remove `WorkoutValidationRun` as reusable permission for acceptance,
scheduling, adaptation, publication, or push. Remove its model/table,
relationships, repository and cache lookups, feedback invalidation updates,
observability queries, account export/deletion enumeration, and tests that treat
health or training context as a durable `valid = false` state.

Structural validation reports remain on immutable workout revisions and are
rechecked where required by lifecycle invariants. Training fit is assessed
fresh under the versioned advisory policy. When an elevated warning is
acknowledged, the existing local workout event or durable Garmin
operation/attempt record, as appropriate, stores the policy version, assessment
fingerprint, exact revision/date, and acknowledgement without creating another
reusable validation entity. A failed or unknown external result retains this
authorization record but does not claim publication or push succeeded.

A forward Alembic migration drops `workout_validation_runs`. Its historical
rows require no backfill and may be discarded because they are neither workout
content nor accepted-revision authority. Existing revisions,
`validation_report_json`, accepted state, workout events, and Garmin operation
state must remain. After migration, delayed Garmin commands use structural and
lifecycle validity plus the fresh action-time warning contract; they never use
an old contextual `valid` value.

### Compatibility And Dead Contracts

Remove these internal and same-application compatibility contracts:

- `EasyRunProposalRequest` and `RunningProposalService.create_easy_run()`;
- identical or unused analytics forwarding methods such as the duplicate weekly
  running-volume/load-trend wrapper;
- dead `run.started`, browser-only `error`, and unused `run_id` SSE fields; and
- contract tests that protect those private or unused forms rather than a
  desired behavior.

No database migration or deprecation period is required. These Python APIs and
routes are not supported external contracts. If deployment-specific automation
uses one, that automation must move to the canonical service or route before
the refactor is deployed; the compatibility contract is not retained.

### Permanent Feature Flags And Development Bypasses

Remove the future-state use of:

- separate proposal, daily-adaptation, plan-generation, and generated-Garmin
  Coach flags;
- the Coach rollout-user allowlist;
- hard recent-history, frequency, consistent-week, and quality-spacing
  eligibility rejections and `COACH_PLANNER_HISTORY_GATES_ENABLED`; and
- deferred quality-template availability checks and
  `COACH_DEFERRED_QUALITY_TEMPLATES_ENABLED`.

Remove related settings, environment examples, Compose forwarding,
Coach-prefixed feature helpers, template checks, hard-gate error paths, and
tests that require rejection based only on training history or spacing. The
same signals remain as warning inputs under **CHANGE**; they are not discarded.
Provider credentials and operational kill switches for an actual
external-service incident are not capability rollout flags and may remain where
they have a concrete operational owner. No database migration is required.
Removed environment variables have no effect and receive no compatibility
parser; deployment configuration and documentation must delete them before
deployment.

## Desired Responsibilities And Module Boundaries

### Dependency Direction

```text
routes/templates/browser
  -> coach conversation execution
       -> analytics queries
       -> planning and feedback commands
       -> workout lifecycle commands
            -> Garmin operation boundary

models/repositories support their owning domain
```

Dependencies do not point back upward. In particular:

- planning and workout services never import Coach models, prompt constants,
  provider types, or SSE events;
- analytics never imports Coach or presentation code;
- Garmin operations never interpret chat text or model output; and
- routes do not reproduce planning, workout, or Garmin policy.

### Responsibility Table

| Boundary | Owns | Does not own |
| --- | --- | --- |
| Coach routes and browser UI | Authenticated HTTP, CSRF/rate-limit integration, conversation selection, message submission, SSE delivery, artifact links/actions | Planning policy, analytics calculations, workout validation, prompt policy, Garmin calls |
| Coach conversation execution | Bounded prose history, relevant operation selection, provider call, local event translation, assistant completion/failure | Durable athlete facts, workout definitions, plan algorithms, external mutation rules |
| Coach provider adapter | OpenRouter/LangChain construction, prompt, framework event decoding, timeout/model limits | HTTP, database transactions, domain validation, presentation labels |
| Analytics | User/date-scoped recovery, health, activity, training, feedback, upcoming-workout, and progress read models | Recommendations, mutations, provider schemas, UI rendering |
| Planning and feedback | Goals/profile/availability commands, feedback commands, deterministic workout candidates, training-fit warnings, adaptation, weekly and cycle generation | Conversation lifecycle, workout revision persistence or transitions, provider behavior, Garmin transport |
| Workout lifecycle | Immutable revisions, structural validity, exact acceptance, scheduling, replacement, required warning acknowledgement, lifecycle state, artifact events | Training-fit calculations, Coach runs, prompt provenance policy, Garmin client details |
| Garmin operations | Publication, calendar/device operations, serialization, attempts, uncertain outcomes, reconciliation | Whether model prose implies consent, plan generation, conversation state |
| Repositories | User-scoped query and persistence primitives, eager-loading contracts needed by callers | Commits, provider calls, cross-capability orchestration, presentation |

These are responsibility boundaries, not a requirement to create one class or
file per table row. Existing modules should be retained, reduced, moved, or
split only where needed to make these directions true. Do not add a command bus,
event bus, workflow engine, generic tool framework, or separate service merely
to express these boundaries.

## Data Ownership And Compatibility

The following durable user value must be preserved through forward migrations:

- conversations and completed messages;
- athlete goals, profile, availability, and performance anchors;
- pre-session and post-session feedback;
- activities, health history, and synchronization coverage;
- workouts, immutable revisions, accepted revision identity, schedules, and
  replacement relationships;
- plans, cycles, immutable revisions, acceptance, and memberships; and
- Garmin content, calendar, device, operation, and attempt state.

Internal IDs and telemetry have no compatibility guarantee except where needed
to preserve artifact provenance or external-operation recovery. Model, table,
or column removal must include an Alembic migration, `app/models/__init__.py`
updates where relevant, account export coverage, and `tests/test_migrations.py`.
Database and filesystem account cleanup remain coordinated.

## User Interface Requirements

- User-facing copy remains German.
- The default Coach surface is conversation, not a dashboard of forms or modes.
- Relevant goal, feedback, workout, adaptation, and plan results appear as
  concise server-owned artifacts embedded in the conversation.
- Workout and plan artifacts show advisory warnings with the dated personal
  evidence, data coverage, recommendation, and alternative. A warning does not
  hide or disable draft creation.
- Artifact controls state the exact action and consequence. Acceptance,
  scheduling, replacement, publication, and push are never hidden behind a
  generic confirmation.
- An elevated same-day health warning has a distinct acknowledgement control
  that asks whether the user still wants to proceed with the exact revision on
  that date. It is separate from the explicit lifecycle-action control and is
  not represented as a disabled button or an unavoidable refusal.
- The UI distinguishes advice, draft, accepted, published, pushed, failed, and
  incomplete states without reconstructing status from several raw flags in
  JavaScript.
- Streaming inserts answer text as text, not trusted HTML. Artifact HTML remains
  server rendered and user scoped.
- Desktop and mobile remain functional; the refactor does not introduce a new
  frontend framework.

## Code Style

Boundaries use explicit user and date context and return typed deterministic
results. Hidden globals and implicit dates are not acceptable in coaching
operations. The desired shape is direct:

```python
athlete_data = AthleteDataService(
    session,
    current_user.id,
    as_of=coaching_date,
)
```

Names describe domain outcomes rather than Coach phases or provider mechanics.
German belongs in user-facing presentation and prompt text; domain statuses and
code identifiers remain English. New abstractions require demonstrated reuse or
a clear reduction in responsibility, dependency, or test complexity.

## Testing Strategy

Tests protect stable behavior at public service, route, and persistence
boundaries rather than private LangGraph shapes or decorated tool internals.

### Required Behavioral Coverage

- Preserve every KEEP outcome in `tests/test_coach_characterization.py`,
  including exact history bounds, active-response exclusion, stale recovery,
  and failed-partial exclusion.
- Demonstrate atomic active-response claiming with concurrent submissions.
- Demonstrate authentication, onboarding, CSRF, rate limiting, and cross-user
  isolation for every new conversational command.
- Demonstrate that trusted runtime values cannot be supplied by model-visible
  operation arguments.
- Demonstrate data explanation with missing, partial, unsupported, and
  synchronized-empty fixtures without converting missing values to zero.
- Demonstrate the adaptive loop: the same user question receives different
  relevant context after goal, feedback, completed-activity, or plan-progress
  state changes.
- Demonstrate conversational goal/profile updates, structured feedback,
  workout draft/revision, daily adaptation, and weekly/cycle draft generation
  through deterministic services.
- Demonstrate that every registered workout format can produce a draft with
  sparse history, fewer than two weekly sessions, inconsistent weeks, or close
  quality-session spacing, while returning the relevant warning.
- Demonstrate that weekly and multiweek drafts remain available with sparse or
  incomplete wearable history, re-entry, and aggressive but representable
  goals, while clearly stating confidence, assumptions, and poor fit.
- Demonstrate that severe recent personal-health deviations and explicit serious
  feedback do not block draft creation, but do require informed acknowledgement
  before accepting, scheduling, replacing, or keeping the affected same-day
  workout. Normal or missing data must not trigger that acknowledgement.
- Demonstrate that ordinary poor recovery, one anomalous metric, mild illness,
  and a single low readiness score produce **Caution**, not **Elevated**, for
  both workouts and plans. Future-dated workouts do not use today's health as a
  same-day confirmation gate.
- Demonstrate action-time reassessment after health or feedback changes, and
  reject stale, cross-user, cross-revision, cross-date, policy-version, or
  fingerprint-mismatched acknowledgements. A matching acknowledgement may be
  reused only for the same revision/date while inputs and the local date remain
  unchanged.
- Demonstrate that keep, reduce, replace, and rest adaptation choices remain
  available under former `SAFETY_STOP`/health `CLARIFY` inputs and that an
  elevated keep/replacement follows the informed-confirmation contract.
- Demonstrate that delayed same-day publish or push refreshes the assessment,
  requests acknowledgement when newly elevated, and proceeds after a matching
  acknowledgement without changing structural validity. A provider failure or
  unknown result retains the durable authorization/attempt but does not record a
  successful published/pushed transition.
- Demonstrate that missing essential user choices, unsupported formats,
  structurally invalid content, ownership violations, and invalid lifecycle
  transitions remain blocked with a specific explanation.
- Demonstrate that draft prose cannot accept, schedule, replace, publish, or
  push and that explicit actions target the exact artifact revision and, for
  scheduling, the displayed date.
- Preserve deterministic planner composition, adaptation load limits, workout
  lifecycle, and Garmin unknown-outcome tests. Replace tests whose asserted
  outcome is rejection solely because of history, frequency, consistency,
  quality spacing, recovery, or health data; those tests must assert warning and
  artifact availability instead.
- Add migration coverage proving conversation/artifact preservation while run,
  tool-call, and contextual-validation tables and redundant origins are removed;
  accepted revisions, immutable structural reports, workout events, and Garmin
  state must survive.
- Exercise the browser SSE parser and artifact rendering behavior rather than
  asserting JavaScript source substrings.

Automated tests use fake provider adapters and synthetic athlete data. They do
not require OpenRouter, Garmin credentials, or network access. Provider-specific
contract tests belong at the single adapter boundary. A small manually run
evaluation set may assess actual answer usefulness, uncertainty, and focused
questions against a configured model, but it does not replace deterministic CI
coverage.

### Verification Commands

```bash
# Focused characterization and coaching behavior
uv run pytest tests/test_coach_characterization.py
uv run pytest tests/test_training_agent.py
uv run pytest tests/test_workout_proposals.py
uv run pytest tests/test_daily_adaptation.py
uv run pytest tests/test_weekly_plan_service.py tests/test_multiweek_planner.py
uv run pytest tests/test_feedback.py tests/test_workout_service.py

# Required for model or migration changes
uv run pytest tests/test_migrations.py

# Final verification
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check

# Required after template or Tailwind-input changes
npm run build:css
# Then update the stylesheet cache key where required.
```

## Delivery Boundaries

### Always

- Preserve durable user value and the accepted-revision/Garmin safety boundary.
- Keep behavior-preserving simplification separate from intentional behavior
  changes.
- Make each changed capability independently reviewable with focused tests.
- Use forward, data-preserving migrations for schema consolidation.
- Update current product/configuration documentation when behavior changes.

### Ask First

- Adding a dependency, process, service, database, queue, or frontend framework.
- Weakening explicit confirmation for accepted content or external actions.
- Removing informed acknowledgement for an elevated same-day personal-health
  warning or turning advisory training-fit signals back into creation gates.
- Making the Coach autonomous or background initiated.
- Deleting durable athlete, feedback, workout, plan, or conversation content.
- Expanding executable workout generation beyond deterministic supported
  formats.

### Never

- Treat model prose as authorization for a consequential action.
- Send unaccepted or structurally invalid content to Garmin.
- Let model-visible input choose user identity, database/session access, request
  identity, artifact ownership, or the authoritative coaching date.
- Fabricate missing data, progress, completion, or Garmin metrics.
- Block a supported workout or plan draft solely because of training frequency,
  history depth, consistency, quality spacing, recovery, wearable coverage, or
  comparison with population norms.
- Treat missing health data as evidence of danger or an elevated warning.
- Add orchestration layers to preserve obsolete internal APIs or duplicate UI
  workflows.
- Require live external credentials in automated tests.

## Acceptance Criteria

The future state satisfies this specification when:

- all capabilities in the classification table have the specified outcome and
  removed capabilities have no remaining route, UI, model, configuration, or
  test authority;
- a user can query data, manage goals, record feedback, evaluate progress,
  create or revise workouts, request adaptation, and draft plans through one
  conversational surface;
- changed durable context is used in later guidance without requiring the user
  to repeat it;
- recommendations expose material evidence, uncertainty, and assumptions;
- every supported workout format and representable plan remains draftable
  regardless of training frequency or history depth, with poor fit expressed as
  advice, warnings, confidence, and alternatives rather than refusal;
- genuinely severe personal-health signals produce a clear recommendation and
  informed same-day action confirmation without preventing draft creation;
- one versioned deterministic policy separates ordinary caution from elevated
  concern using personal, dated, adequately covered evidence rather than model
  discretion or population norms;
- consequential same-day actions refresh that assessment and accept only an
  acknowledgement bound to the exact user, revision, date, policy version, and
  unchanged assessment fingerprint;
- draft generation is direct while exact-revision acceptance, local scheduling,
  and Garmin actions remain explicit;
- only one response can execute per conversation and failure states are durable,
  comprehensible, and recoverable;
- Coach routes contain HTTP concerns, provider details stay in one adapter, and
  analytics/planning/workout/Garmin packages do not depend on Coach
  implementation details;
- `CoachAssistantRun`, `CoachToolCall`, `WorkoutValidationRun`, hard history and
  contextual safety gates, duplicate proposal entry points, planning shadow,
  dead contracts, and permanent per-capability rollout policy are gone;
- existing conversations and all durable athlete, feedback, workout, plan, and
  Garmin state survive required migrations; and
- focused behavioral tests and the full project verification commands pass.

## Non-Goals

- A full rewrite of the Coach or planning system without implementation evidence
  that incremental change is less safe.
- Autonomous plan regeneration, background coaching messages, or automatic
  Garmin actions.
- Arbitrary model-authored workout steps or a general-purpose workflow engine.
- Replacing deterministic analytics with model calculations.
- Adding a model self-review/revision loop or generic refusal framework without
  a concrete risk boundary.
- Removing structural validation, ownership checks, exact-revision acceptance,
  or Garmin lifecycle safeguards in the name of making training content
  available.
- Preserving private Python APIs, exact framework events, current route layout,
  or current UI steps for compatibility alone.
- Solving detailed sleep-stage timezone presentation or introducing per-user
  timezone settings as part of this refactor.

## Open Questions

No product question blocks approval of the desired state. Implementation
planning must still identify the smallest safe migration sequence and inventory
deployment-specific automation that must move off removed internal routes or
environment flags before deployment.
