# AI Coach Phase 8 Gate Matrix

**Status:** Completed  
**Scope:** One deterministic Easy Run proposal vertical slice without LangChain

Phase 8 uses the existing `Workout` aggregate and immutable `WorkoutRevision` records. It does
not introduce a second proposal model and does not let chat, an LLM, proposal creation, or
acceptance mutate the calendar or Garmin.

| Gate | Implementation | Verification |
|---|---|---|
| Typed request | `EasyRunProposalRequest` requires a future/today date, at least 20 available minutes, and an idempotency key | `test_proposal_route_is_feature_gated_and_renders_detail` |
| Rollout safety | Creation, generated edit, acceptance, and local scheduling require `COACH_WORKOUT_PROPOSALS_ENABLED`; view, reject, delete, and unschedule remain available for cleanup | `test_generated_edit_and_schedule_enforce_proposal_contract` |
| Observed baseline | Proposal creation requires at least one observed run in the 56-day baseline and persists compact data quality and model versions | `test_proposal_requires_recent_history_and_respects_safety_stop` |
| Deterministic safety | Current structured feedback is evaluated before persistence; `clarify` and `safety_stop` create no proposal | `test_proposal_requires_recent_history_and_respects_safety_stop` |
| Template expansion | Only active `easy_run` is selected; duration is `min(45, available_minutes)`, bounded by 20-90 minutes | `test_easy_run_proposal_is_deterministic_revisioned_and_unscheduled` |
| Personalized device target | A valid Garmin Running HR profile is preferred over Default and compiles its concrete aerobic BPM bounds to Garmin; invalid profiles are skipped | `test_easy_run_uses_personal_garmin_hr_range_as_device_target`, `test_easy_run_hr_target_falls_back_to_valid_default_profile` |
| Garmin principal binding | The target freezes the source Garmin principal; edits, acceptance, and sync fail closed after an account switch | `test_easy_run_uses_personal_garmin_hr_range_as_device_target` |
| Honest intensity | RPE 2-3 and a full-sentence Talk Test remain in the V2 step. Without a valid personal HR profile they are the explicit no-device-target fallback; pace and distance remain unknown | `test_easy_run_proposal_is_deterministic_revisioned_and_unscheduled` |
| Proposal aggregate | Result is one `Workout` with `approval_status=proposed`, no accepted revision, and no local date; requested date stays on `WorkoutRevision.suggested_for` | `test_easy_run_proposal_is_deterministic_revisioned_and_unscheduled` |
| Immutable evidence | Revision persists purpose, guidance, load uncertainty, structural report, frozen generation context, and generator/template/rule/knowledge versions | `test_easy_run_proposal_is_deterministic_revisioned_and_unscheduled` |
| Validation runs | Creation records structural and contextual runs; acceptance always appends a fresh acceptance run and audits its fingerprint | `test_proposal_edit_accept_schedule_and_reject_lifecycle` |
| Creation idempotency | A request key is bound to a canonical request fingerprint and resolved before mutable baseline/safety checks; concurrent unique-key losers load the winner | `test_easy_run_proposal_is_deterministic_revisioned_and_unscheduled` |
| Generated edits | Existing editor appends a running-only Easy Run revision, recalculates load/context, records a parent diff, and uses exact identity, CAS, and replay protection | `test_proposal_edit_accept_schedule_and_reject_lifecycle`, `test_generated_edit_and_schedule_enforce_proposal_contract` |
| Exact acceptance | Revision ID, number, content hash, context fingerprint, and aggregate lock must match; repeated acceptance is a no-op | `test_proposal_edit_accept_schedule_and_reject_lifecycle` |
| Reject | Only an unaccepted generated proposal can be rejected; rejection is exact-revision and replay safe | `test_proposal_edit_accept_schedule_and_reject_lifecycle` |
| Explicit scheduling | Acceptance leaves the calendar untouched; scheduling names the accepted revision and must use its reviewed future suggested date | `test_proposal_edit_accept_schedule_and_reject_lifecycle`, `test_generated_edit_and_schedule_enforce_proposal_contract` |
| Garmin boundary | Generated sync additionally requires `COACH_GARMIN_SYNC_ENABLED` and reuses the accepted-revision Garmin operation service | `test_generated_proposal_uses_shared_idempotent_garmin_service` |
| HTTP flow | CSRF-protected UI creates, edits, accepts, and schedules without direct proposal insertion | `test_proposal_route_is_feature_gated_and_renders_detail` |
| Manual regression | Manual workouts retain their existing editor, lifecycle, and Garmin behavior | Full `tests/test_routes.py`, `tests/test_workout_revisions.py`, and `tests/test_workout_service.py` suites |

## Product Policy

- Proposal creation is deterministic application code, not a LangChain tool.
- No observed run in the last 56 days fails closed; self-assessment intake is not introduced in
  this slice.
- Positive wearable or readiness data cannot reduce a structured safety outcome.
- A generated Easy Run remains running and time based after edits. Its selected personalized HR
  target cannot be replaced, while RPE 2-3 and the Talk Test remain local guidance.
- A requested date is a suggestion until exact acceptance and a separate schedule command.
- A valid personal Garmin Running profile supplies concrete aerobic BPM bounds; a valid Default
  profile is the fallback. Source, training method, and synchronization date remain visible and
  immutable on the revision.
- The target remains bound to the Garmin principal that supplied it. Switching accounts requires a
  newly generated proposal before editing, acceptance, or synchronization.
- If neither profile is valid, RPE compiles to no Garmin device target and local instructions remain
  in PacePilot with visible warnings.
- Pace targets remain deferred until reliable personal threshold, race/time-trial, or Critical Speed
  evidence is connected to the deterministic generator; generic pace values are never invented.
- Threshold, VO2, plans, daily adaptation, and chat-triggered mutations remain deferred.

## Commands

```text
uv run pytest tests/test_workout_proposals.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```
