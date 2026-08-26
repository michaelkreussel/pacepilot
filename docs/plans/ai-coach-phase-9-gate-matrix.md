# AI Coach Phase 9 Gate Matrix

**Status:** Completed  
**Scope:** One LangChain Easy Run proposal tool with durable server-side artifacts

| Gate | Implementation | Verification |
|---|---|---|
| Single mutation boundary | Conditional tool registry adds only `create_running_workout_proposal`; no accept, schedule, Garmin, or generic mutation tool exists | `test_agent_registers_exactly_one_bounded_mutation_tool` |
| Minimal typed schema | Model supplies only `suggested_for` and `available_minutes`; runtime IDs and workout content remain injected/server-side | `test_proposal_tool_schema_exposes_no_runtime_or_workout_definition` |
| Relative date context | Each run prepends trusted ISO values for today, tomorrow, and the day after tomorrow before conversation messages | `test_langchain_backend_maps_tokens_and_tool_lifecycle` |
| Expected domain failure | Past or otherwise rejected proposal inputs return `not_created`, emit no artifact, persist no workout, and let SSE complete normally | `test_invalid_proposal_date_returns_completed_stream_without_artifact`, `test_coach_tool_creates_one_durable_server_rendered_proposal` |
| Durable local run | User message, assistant message, and `CoachAssistantRun` commit before streaming; completion/failure/interruption update both message and run | `test_coach_streams_and_persists_conversation`, `test_proposal_survives_stream_failure_after_commit` |
| User-scoped runtime | Tool validates authenticated user, conversation, triggering message, assistant message, and run before mutation | `test_coach_tool_creates_one_durable_server_rendered_proposal`, existing runtime scope tests |
| Deterministic delegation | Tool calls `RunningProposalService`; Phase 8 baseline, safety, template, validation, HR target, and no-calendar rules remain authoritative | `test_coach_tool_creates_one_durable_server_rendered_proposal`, full `tests/test_workout_proposals.py` |
| Stable mutation slot | Key derives from local assistant run and tool version, never provider call ID; duplicate and stale concurrent prechecks resolve to one workout/event | `test_coach_tool_creates_one_durable_server_rendered_proposal`, `test_stale_idempotency_precheck_rolls_back_duplicate_proposal` |
| Changed retry arguments | Canonical request fingerprint rejects changed arguments for the same run and leaves one workout | `test_coach_tool_creates_one_durable_server_rendered_proposal` |
| Atomic provenance | Proposal transaction binds conversation/message provenance and the unique run-to-workout link with the proposal ledger event | `test_coach_tool_creates_one_durable_server_rendered_proposal` |
| Trusted artifact mapping | LangChain emits an internal artifact signal only for the exact successful structured tool result | `test_langchain_backend_maps_only_valid_proposal_artifact` |
| SSE deduplication | Route resolves the persisted artifact and emits one `proposal.created` event even after duplicate internal signals | `test_coach_tool_creates_one_durable_server_rendered_proposal` |
| Server-rendered card | User-scoped endpoint renders trusted Jinja; chat JS fetches it and never renders model HTML | `test_coach_tool_creates_one_durable_server_rendered_proposal`, `test_coach_stream_renders_model_text_safely` |
| Failure survival | Provider error or disconnect after commit preserves exactly one proposal and makes it visible after reload | `test_proposal_survives_stream_failure_after_commit[provider-failed]`, `test_proposal_survives_stream_failure_after_commit[disconnect-interrupted]` |
| Cross-user fail closed | Card endpoint requires matching user, conversation, run, workout, and provenance; mismatches return 404 | `test_coach_tool_creates_one_durable_server_rendered_proposal` |
| Feature rollout | Disabled flag omits mutation tool and retains read-only coach; proposal service also fails closed | `test_agent_registers_exactly_one_bounded_mutation_tool`, Phase 8 feature-gate tests |
| Schema migration | Alembic creates run table, indexes, unique artifact links, and matches ORM metadata | `tests/test_migrations.py` |
| Existing coach regression | Conversation history, all read-only tools, bounded model context, telemetry, and SSE answer lifecycle remain intact | full `tests/test_training_agent.py` |

## Product Policy

- The LLM may decide whether to request the one bounded tool after date and available time are known.
- The LLM never chooses workout internals and cannot claim success without the committed artifact.
- The card is informational and links to the existing review surface; chat language never accepts or
  schedules it.
- A failed or interrupted response does not invalidate a proposal already committed by the tool.
- Provider tool-call IDs are observability metadata, not business command identifiers.
- Easy Run remains the only exposed proposal intent in this phase.

## Commands

```text
uv run pytest tests/test_training_agent.py tests/test_workout_proposals.py
uv run pytest tests/test_migrations.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```
