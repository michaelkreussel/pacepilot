# AI Coach Architecture

## Product Boundary

The AI Coach is one optional, user-initiated conversational capability. When `LLM_API_KEY` and
`LLM_MODEL` configure OpenRouter, an authenticated and onboarded user can use the Coach to:

- inspect bounded personal recovery, health, activity, training, feedback, and progress evidence;
- manage explicit goals, planning profile data, availability, and performance anchors;
- record structured pre-session and post-session feedback;
- create or revise deterministic workout, weekly-plan, and training-cycle drafts; and
- assess daily adaptation choices.

The model interprets intent and explains deterministic results. It does not invent executable
workout steps or treat chat text as consent. Drafts remain unaccepted. Acceptance, scheduling,
replacement, plan commitment, Garmin publication, and device push are explicit authenticated
commands against the displayed artifact and exact revision. Elevated same-day health concerns also
require a fresh acknowledgement bound to the user, revision, date, policy version, and assessment
fingerprint.

## Responsibility And Dependency Direction

```text
routes/templates/browser
  -> Coach conversation execution
       -> analytics queries
       -> planning and feedback commands
       -> workout lifecycle commands
            -> Garmin operation boundary
```

- `app/routes/coach.py` owns authenticated HTTP, conversation selection, message submission, SSE
  delivery, and artifact action adaptation.
- `app/services/coach/conversation.py` owns bounded completed-message history, one active response,
  stale-response repair, trusted request context, and execution preparation.
- `app/services/coach/agent.py` defines the provider-neutral streaming contract.
- `app/services/coach/provider.py` is the only OpenRouter/LangChain/LangGraph adapter. It owns prompt
  policy, model/tool limits, framework event decoding, and provider failures.
- `app/services/coach/tools.py` adapts trusted runtime context to user-scoped deterministic analytics,
  planning, feedback, and workout commands.
- `app/services/coach/presentation.py` projects durable messages and artifacts for both initial page
  rendering and streamed completion.

Analytics, planning, workout, and Garmin services do not import Coach implementation details. They
own their calculations, state transitions, transactions, structural validation, external-operation
recovery, and user/date scoping. The Coach calls those boundaries rather than reproducing their
policy.

## Persistence And Failure Semantics

`CoachMessage` is the durable assistant-execution and provenance record. It stores status, completed
prose, request/model/contract versions, concise failure category, and artifact provenance. Only one
assistant message may be streaming per conversation. Completed history is bounded to the latest 20
messages and 12,000 characters before the current question.

Failed or interrupted partial prose is not retained as a completed answer or reused as model
history. A deterministic artifact committed before a later provider failure remains durable. The
removed assistant-run, tool-call timeline, and contextual-validation entities have no runtime,
configuration, UI, export, or observability authority. Historical migrations retain only the schema
needed to upgrade older databases and preserve durable conversations, artifacts, revisions, plans,
workout events, and Garmin operation state.

## Configuration And Privacy

The Coach has two availability states: OpenRouter is configured through `LLM_API_KEY`, `LLM_MODEL`,
and `LLM_TIMEOUT_SECONDS`, or the Coach is unavailable. `COACH_RATE_LIMIT_PER_MINUTE` limits
user-initiated Coach mutations. There are no per-capability rollout users, history eligibility gates,
or development-only template controls.

OpenRouter receives the prompt, bounded completed conversation history, and only the selected
athlete context needed for the question. Model-visible operation arguments cannot choose user ID,
database access, request ID, authoritative coaching date, or artifact ownership. Results omit
credentials, token and raw-file paths, sampled GPS tracks, and unbounded Garmin payloads. Application
logs may contain request/user/message identifiers, provider model, operation names, timings, and
failure categories, but not question text, answer text, or health values. Per-tool records are not
stored in SQLite or shown to the user.

## Verification

Deterministic CI coverage is organized around stable boundaries:

- `tests/test_coach_characterization.py` protects authentication, conversation durability, history
  bounds, concurrency, stale recovery, failures, and cross-user isolation.
- `tests/test_training_agent.py` uses fake providers and synthetic athlete data for data access,
  trusted context, operations, artifacts, uncertainty, and adaptive behavior.
- `tests/test_workout_proposals.py`, `tests/test_daily_adaptation.py`,
  `tests/test_weekly_plan_service.py`, and `tests/test_multiweek_planner.py` protect advisory draft
  availability, versioned training fit, revision boundaries, and explicit actions.
- `tests/test_feedback.py`, `tests/test_workout_service.py`, and Garmin service tests protect
  acknowledgement, lifecycle, external-operation, and unknown-outcome behavior.
- `tests/coach_sse.test.mjs` executes the browser SSE and artifact contract.
- `tests/test_migrations.py` verifies forward upgrades and durable-data preservation while obsolete
  tables and origins are removed.
- `tests/test_coach_architecture.py` enforces provider isolation and downward dependency direction.

Automated tests never require OpenRouter, Garmin credentials, or network access.

## Manual Model Evaluation

Answer usefulness, language quality, and whether a clarification is appropriately focused require a
configured model and human judgment. This optional evaluation does not replace CI:

1. Use a non-production test account with synthetic or deliberately non-sensitive data and configure
   OpenRouter locally.
2. Ask one data-explanation question with missing coverage, one material next-session recommendation,
   one workout-draft request with sparse history, and one ambiguous request missing an essential
   choice.
3. Confirm each answer is direct, proportional to the question, grounded in dated evidence, explicit
   about missing data and assumptions, and does not fabricate metrics or claim an unperformed action.
4. Confirm a representable draft remains available with warnings rather than a history-based refusal,
   while the ambiguous request asks exactly one focused question.
5. Confirm generated cards remain drafts and no acceptance, scheduling, publication, or push occurs
   until the corresponding explicit UI action is used.

Do not record prompts, responses, screenshots, or exported account data in the repository when they
contain personal information.
