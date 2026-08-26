# Phase 8 Completion - First Proposal Vertical Slice without LangChain

**Status:** Completed  
**Completed:** 2026-08-24  
**Next phase:** Phase 9 - LangChain integration as a structured artifact

## Delivered

- A feature-gated Easy Run form on the coach page asks only for a suggested date and available
  time. It operates independently of LLM configuration.
- `RunningProposalService` connects the running baseline, deterministic safety triage, active
  `easy_run` template, V2 generator, load estimate, and candidate validation.
- The deterministic duration policy honors the requested time up to the template's 90-minute
  ceiling (`min(90, available_minutes)` since `easy-run-candidate-v2`; originally capped at
  45 minutes and corrected after real-world 60-minute requests produced 45-minute workouts).
- Proposal creation requires observed running history in the preceding 56 days. Sparse or
  low-confidence performance data never produces a fabricated pace or distance.
- If Garmin supplies a structurally valid personal heart-rate profile, the proposal uses the
  concrete personalized aerobic range as its Garmin device target. A valid Running profile takes
  precedence over a valid Default profile; invalid profiles are skipped.
- The selected BPM bounds, profile sport, training method, source, and synchronization date are
  frozen in revision guidance and generation context. The synchronization date remains visible
  instead of relying on an arbitrary, unsupported expiry threshold.
- The Garmin principal fingerprint is frozen with the target. Generated edits, acceptance, and
  synchronization fail closed after a Garmin account switch and require a new proposal.
- RPE 2-3 and the full-sentence Talk Test remain local guidance with or without a heart-rate target.
  Without a valid personal profile, the proposal explicitly falls back to those local cues and no
  device target.
- Safety outcomes `clarify` and `safety_stop` block persistence. `warn` remains visible and does not
  silently become an allow result.
- The result is the existing `Workout` aggregate in `proposed` state with an immutable current
  revision. It has no accepted revision and no local or Garmin calendar mutation.
- The revision freezes data quality, baseline and intensity context, feedback references, units,
  request parameters, load uncertainty, and all generator/template/rule/knowledge versions.
- Creation persists separate structural and contextual validation runs. Acceptance always records a
  fresh append-only acceptance run and audits its exact context fingerprint.
- Request fingerprints and event keys make proposal creation replay safe even if safety or baseline
  context changes after the first success.
- The shared editor supports generated edits with exact parent identity, optimistic locking,
  idempotency, complete revalidation, recalculated load/context, and a visible parent-to-child diff.
- Generated Easy Run edits cannot change sport, leave the 20-90 minute/time-budget range, replace
  the selected personal device target, or remove RPE 2-3 and the full-sentence Talk Test.
- Accept, reject, and schedule are explicit revision-scoped commands. Scheduling is separate from
  acceptance and only permits the reviewed suggested date.
- Generated Garmin operations require the separate Garmin feature flag and reuse the existing
  accepted-revision, idempotent Garmin service.
- Disabling proposal generation freezes edit, accept, and schedule commands for existing generated
  proposals while preserving view, reject, delete, and unschedule cleanup paths.
- No database migration was needed because Phase 3 already supplied the aggregate, revision,
  validation, audit, and Garmin-operation schema.

## User Flow

1. Enable `COACH_WORKOUT_PROPOSALS_ENABLED`.
2. Open `/coach` and expand **Easy Run vorschlagen**.
3. Submit a suggested date and at least 20 available minutes.
4. Review rationale, data quality, the personal BPM device target and its source when available,
   RPE/Talk Test, unknown distance, safety state, and any Garmin degradation on the shared workout
   detail page.
5. Optionally edit the duration in the shared editor; PacePilot creates and validates a new revision.
6. Explicitly accept the exact current revision.
7. Explicitly schedule that accepted revision on its reviewed suggested date.
8. If `COACH_GARMIN_SYNC_ENABLED` is also enabled, use the existing Garmin upload/push flow.

## Deliberate Boundaries

- The coach chat and its tools remain read-only.
- Phase 8 does not add LangChain mutations, an SSE proposal artifact, or conversation provenance.
- It does not create a separate `WorkoutProposal` table or copy a proposal into another workout.
- It does not offer Recovery, Long Run, Strides, Threshold, or VO2 proposal selection in the UI.
- It does not add self-assessment intake, daily adaptation, plans, weather, AQI, or automatic
  replanning.
- It does not accept, schedule, upload, or push from chat.

## Verification

```text
uv run pytest                         301 passed
uv run ruff check .                  passed
uv run ruff format --check .         passed
uv run ty check                      passed
Agent Browser desktop/mobile         0 WCAG A/AA violations
```

## Phase 9 Entry Point

Phase 9 may expose exactly one idempotent LangChain tool that requests this deterministic service.
The LLM must not construct workout definitions, accept revisions, schedule dates, or call Garmin.
The resulting server-side proposal artifact should be linked to its conversation/run and announced
over SSE without rendering model-generated HTML.
