# Phase 7 Completion - Evidence, Template, and Constraint Registry

**Status:** Completed  
**Date:** 24 August 2026  
**Next phase:** Phase 8 - First Proposal Vertical Slice without LangChain

## Delivered

- Added a bundled knowledge registry for evidence, workout templates, and constraint metadata.
- Added pinned PyYAML 6.0.3, duplicate-key-rejecting safe loading, strict Pydantic validation,
  reference-integrity checks, explicit supported implementation IDs, and deterministic complete-
  document fingerprints.
- Added evidence records with population, limitations, allowed and forbidden uses, review date, and
  source metadata. Exact ranges are separately marked as versioned Product Heuristics.
- Added six report-backed running template families. Easy, Recovery, conservative Long Run, and
  Strides are active; Threshold Cruise and VO2 Intervals remain deferred.
- Added typed eligibility input. Templates cannot expand without sufficient history, available time,
  required baseline facts, an explicit safety state, and absence of declared contraindications.
- Added deterministic template expansion with stable UUID5 block IDs and typed load estimates. A
  time-based workout leaves distance unknown instead of manufacturing pace or distance.
- Added registered Python constraint execution for no catch-up stacking, single-session distance
  review, one-axis progression, quality spacing, and no-escalation daily adaptation. Safety triage
  consumes registry thresholds, and the knowledge fingerprint now contributes to its rule-set
  version.
- Added `WorkoutDefinition` V2 with RPE ranges and up to five local instructions per step while
  preserving strict V1 parsing. Format version, RPE, and instructions participate in revision hashes.
- Extended the existing editor and shared preview for RPE and Talk Test content, including actionable
  validation errors and V1-to-V2 promotion only when V2 content is introduced.
- Added explicit Garmin compilation warnings. RPE is sent as no device target, and local instructions
  remain visible in PacePilot rather than silently pretending to reach the watch.
- Added Hypothesis as a development dependency, registry and template unit tests, property tests, and
  route-level V2 round-trip tests.
- Added `knowledge/` to the production image and registry validation to application startup.

## Research Alignment

- The implementation follows the Phase 7 sequence in the master plan and the taxonomy, evidence
  model, template schema, and test strategy in the consolidated market-research report.
- DOI metadata was checked for the principal training-distribution, interval-design, and
  single-session-distance sources used by the initial registry.
- Scientific sources support principles and permissible rule direction. They do not claim that the
  selected duration, repetition, RPE, or recovery defaults are universally optimal.
- Garmin remains an export target, never the canonical workout model.

## Verification

```text
uv run pytest                    293 passed
uv run ruff check .              passed
uv run ruff format --check .     passed
uv run ty check                  passed
Agent Browser desktop/mobile     0 WCAG A/AA violations
```

The remaining pytest warnings are the existing Starlette `TestClient`/httpx deprecation and Python
3.12 SQLite datetime-adapter deprecations exercised by migration tests.

## Phase 8 Entry Point

Connect the active Easy Run template first to the existing baseline, current safety context, and
validator. Persist the result as a proposed workout revision with template, knowledge, rule,
generator, and performance-model versions. Keep proposal creation deterministic and UI-driven before
adding a mutating coach tool.
