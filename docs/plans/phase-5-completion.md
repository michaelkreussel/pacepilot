# Phase 5 Completion - Running Baseline and Intensity Shadow Model

**Status:** Completed  
**Date:** 22 August 2026  
**Next phase:** Phase 6 - Subjective Feedback and Safety Triage

## Delivered

- Shared, versioned sport-family, calendar-window, and hard-activity semantics extracted from the
  existing training trends instead of duplicated.
- Deterministic `AthleteDataService.get_running_baseline()` output for exact 7, 28, 56, and 180 day
  windows, strictly isolated from cycling, mixed sports, and other users.
- Running frequency, active days, duration, distance, per-run and weekly median/MAD, weekly typical
  long run, absolute longest run, hard days, quality density, spacing, and sRPE.
- Explicit activity-history source, sync state and age, metric-level coverage, invalid-value counts,
  latest-run age, confidence level, and machine-readable confidence reasons.
- Reproducible interruption, current inactivity, 14 day re-entry observation, and prior-30-day
  single-distance-spike facts.
- Conservative intensity source priority through typed ephemeral performance anchors, fresh Garmin
  lactate threshold, RPE/Talk Test fallback, or athlete clarification for insufficient data.
- Garmin race predictions, VO2max, endurance score, and hill score retained as secondary context
  that cannot independently create a pace anchor.
- Critical Speed available only from two consistent, reliable, fresh multi-distance performance
  anchors and an adequate running baseline.
- Canonical JSON-safe `running_generation_context.v1` plus stable input and context fingerprints for
  later storage in `WorkoutRevision.generation_context_json`.
- Shadow-only integration: no workout generation, database write, Garmin request, LLM call, route,
  coach tool, or UI was added.
- Phase gate matrix recorded in `docs/plans/ai-coach-phase-5-gate-matrix.md`.

## Verification

```text
uv run pytest                    254 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
git diff --check                 passed
```

The remaining pytest warnings are the existing Starlette `TestClient`/httpx deprecation and Python
3.12 SQLite datetime-adapter deprecations exercised by migration tests.

No database schema, migration, Garmin payload, feature flag, route, template, or CSS changed in this
phase.

## Phase 6 Entry Point

Add explicit German pre-session and post-session feedback, deterministic safety triage, export and
deletion behavior, and connect newer safety context to the existing acceptance and pre-sync
validation hooks. Positive wearable data must never override a safety stop.
