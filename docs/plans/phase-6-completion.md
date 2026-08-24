# Phase 6 Completion - Subjective Feedback and Safety Triage

**Status:** Completed  
**Date:** 22 August 2026  
**Next phase:** Phase 7 - Evidence, Template, and Constraint Registry

## Delivered

- Added migrations `20260822_22` and `20260822_23` with `PreSessionFeedback` and
  `PostSessionFeedback`, range checks, indexes, cascading account erasure, detachable
  workout/activity links, and database-level same-user ownership constraints. Revision 23 keeps
  databases that had already applied the initial revision-22 schema upgradeable.
- Added explicit German pre-session input for motivation, fatigue, leg freshness, soreness, sleep
  feeling, available time, localized pain, gait change, activity-related worsening, illness signals,
  and optional notes.
- Added explicit German post-session input for completion, session RPE, overall feel, localized
  pain, gait change, stopped reason, and optional notes.
- Added pure, versioned `safety-triage-v1` logic with `allow`, `warn`, `clarify`, and `safety_stop`,
  stable issue/rule codes, non-diagnostic German copy, and conservative severity precedence.
- Connected authoritative safety contexts to exact-revision acceptance and delayed Garmin publish
  and push validation. New feedback changes the fingerprint and expires cached validation runs.
- Persisted failed validation evidence before returning a stop or clarification to the user.
- Added explicit seven-day UTC freshness and separate same-day/future-sync behavior.
- Kept wearable data downstream of safety: no Garmin readiness value enters or overrides triage.
- Added user-scoped JSON export with `no-store`, per-entry deletion, settings-level feedback
  management, and removal of derived validation reports during deletion.
- Preserved PacePilot feedback when imported Garmin activity data is deleted by detaching the
  activity link; account erasure still cascades all feedback.
- Added visible safety status and issue copy on workout detail, blocked unsafe acceptance and Garmin
  controls, post-session feedback on activity detail, responsive controls, rebuilt Tailwind 4.3.3
  CSS, and bumped the committed cache key.
- Added the Phase 6 gate matrix in `docs/plans/ai-coach-phase-6-gate-matrix.md`.

## Verification

```text
uv run pytest                    269 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
git diff --check                 passed
```

The remaining pytest warnings are the existing Starlette `TestClient`/httpx deprecation and Python
3.12 SQLite datetime-adapter deprecations exercised by migration tests.

## Phase 7 Entry Point

Introduce the versioned evidence, workout-template, and constraint registry. Parse only pinned,
safe-loaded YAML into typed schemas; keep executable rules in Python. The first generated workout
should be a time-based easy run with RPE/Talk Test content included in the revision content hash and
shared preview.

## Product Correction - 24 August 2026

User testing showed that the complete internal safety schema was too prominent for routine input.
The normal flow was therefore reduced without removing the deterministic safety boundary:

- Garmin `directWorkoutRpe` and `directWorkoutFeel` are normalized and used as the primary
  post-session feedback source. Manual values are field-level fallbacks and remain separately
  attributable.
- Routine post-session input contains only effort 1-10 and five feel categories. Completion,
  pain, stopped reason, and notes are no longer part of the standard activity form.
- The current coach message provides momentary subjective context. It is not copied into a separate
  daily-check-in record; available Health and Readiness data can be read through coach tools.
- Pain and illness questions remain available through progressive disclosure and continue to feed
  the unchanged deterministic safety triage. Free text is not keyword-classified or treated as a
  confirmed safety fact.
- Effective RPE is consumed by running baselines, hard-session classification, recovery analytics,
  recent-activity coach data, and a read-only activity-feedback coach tool.
- Migration `20260824_24` makes legacy structured fields optional and normalizes stored Garmin feel
  values to the canonical 1-5 domain. Existing feedback remains exportable and deletable.
- A follow-up simplification removed the dashboard check-in and its write endpoint. Existing rows
  remain exportable and deletable. PacePilot stores new daily condition text locally only in the
  coach conversation and removes that local copy when the conversation is deleted; messages are
  also processed according to the configured model provider's retention policy.

Verification for the product correction:

```text
uv run pytest                    275 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
Agent Browser desktop/mobile     0 WCAG A/AA violations
```
