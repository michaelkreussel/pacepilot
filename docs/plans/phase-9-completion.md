# Phase 9 Completion - LangChain Integration as a Structured Artifact

**Status:** Completed  
**Completed:** 2026-08-24  
**Next phase:** Phase 10 - Daily Adaptation

## Delivered

- The existing LangChain coach now conditionally receives exactly one mutation-capable tool:
  `create_running_workout_proposal`. All eight existing health and training tools remain read-only.
- The tool schema exposes only a desired date and available time. User, conversation, message, run,
  idempotency, workout definition, pace, distance, targets, validation, calendar, and Garmin fields
  remain server controlled.
- The tool delegates to `RunningProposalService`; it cannot construct workout content or bypass the
  Phase 8 baseline, safety, template, generator, validation, personal HR, and feature-flag rules.
- Every response receives a durable `CoachAssistantRun` linked to the conversation, triggering user
  message, assistant message, request ID, and model. The run is committed before streaming starts.
- One stable server-derived mutation slot is allocated per assistant run. Provider tool-call IDs are
  retained only as telemetry and never influence business idempotency.
- Proposal aggregate, immutable revision, validation, `WorkoutEvent(action="propose")`, conversation
  provenance, and the run-to-workout link commit in the existing proposal transaction.
- Repeated tool calls with different provider call IDs return the same proposal. Changed arguments
  for the same run fail with the existing canonical fingerprint conflict and create no second workout.
- `proposal.created` is emitted only after the route resolves the committed artifact through the
  authenticated user, conversation, run, and workout provenance. No model-produced artifact payload
  or HTML is trusted.
- The browser fetches a user-scoped server-rendered Jinja card. Model answer text continues to use
  text nodes, and the card only links to the shared workout review/editor flow.
- Persisted cards are discovered from assistant runs during conversation rendering. Provider failure
  or disconnect after proposal commit marks the response failed/interrupted but preserves and shows
  the proposal after reload.
- Prompt and UI explain the exact boundary: the coach may request an unaccepted Easy Run proposal;
  acceptance, rejection, editing, scheduling, Garmin upload, calendar sync, and device push remain
  explicit existing UI actions.
- Every model run receives a trusted server-date message that states the exact ISO dates for today,
  tomorrow, and the day after tomorrow. Relative dates must be resolved from this context before the
  typed proposal tool is called.
- Expected proposal-domain failures such as a past date, missing history, safety stop, or idempotency
  conflict return `status: not_created` with a safe code/message. They create no artifact and no
  longer terminate the SSE response as an unhandled request error.
- `COACH_WORKOUT_PROPOSALS_ENABLED` remains disabled by default. When disabled, the mutation tool is
  absent and the existing read-only coach behavior remains available.

## Persistence

Migration `20260824_25` adds `coach_assistant_runs` with:

- conversation, user-message, and assistant-message foreign keys with conversation cleanup;
- one run per assistant message;
- at most one linked workout per run and at most one run per workout;
- run status, model, request ID, and lifecycle timestamps;
- a deletion-safe workout reference using `ON DELETE SET NULL`.

The existing nullable workout provenance fields store conversation and message IDs. Application
validation proves that all IDs belong to the authenticated user and same conversation before the
proposal transaction can bind them.

## SSE Contract

```text
event: run.started
data: {"message_id": ..., "run_id": ...}

event: proposal.created
data: {"workout_id": ..., "run_id": ..., "card_url": "/coach/.../proposal-card"}
```

Duplicate successful tool signals in one stream produce only one `proposal.created` event. The card
endpoint repeats the full ownership and provenance checks and returns 404 for invalid or cross-user
identifiers.

## Deliberate Boundaries

- No model-authored workout definition, pace, distance, HR target, load estimate, or safety result.
- No chat tools for edit, accept, reject, schedule, upload, Garmin calendar, push, or deletion.
- No Recovery, Long Run, Strides, Threshold, VO2, daily adaptation, or plan generation tool.
- No model-generated HTML and no artifact discovery that depends only on a transient SSE event.
- No new proposal aggregate; Phase 9 continues to use `Workout` and immutable `WorkoutRevision`.

## Verification

```text
uv run pytest tests/test_migrations.py  passed
uv run pytest                           309 passed
uv run ruff check .                    passed
uv run ruff format --check .           passed
uv run ty check                        passed
Agent Browser desktop/mobile           0 WCAG A/AA violations
```
