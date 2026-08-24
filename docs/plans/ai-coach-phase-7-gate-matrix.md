# AI Coach Phase 7 Gate Matrix

## Scope Boundary

Phase 7 provides versioned, deterministic knowledge artifacts that Phase 8 can consume. It does not
create proposals, add mutating coach tools, schedule generated workouts, or change the existing
acceptance boundary.

## Gates

| Gate | Automated evidence | Expected behavior |
|---|---|---|
| Safe registry loading | `tests/test_knowledge_registry.py` | Fixed YAML files use a duplicate-key-rejecting SafeLoader and strict Pydantic models; constructors, non-finite values, extras, duplicate IDs, and unresolved evidence references fail closed |
| Provenance | `test_registry_is_deterministic_and_references_resolve`, `test_constraint_set_version_contributes_to_registry_version` | Complete evidence, template, constraint-set IDs, versions, and content contribute to one deterministic SHA-256 knowledge version |
| Evidence honesty | Registry model validation and committed YAML review | Scientific claims, allowed uses, forbidden uses, limitations, and Product Heuristics remain separate; exact numeric defaults are never presented as universal findings |
| Template activation | `test_active_templates_expand_deterministically`, `test_deferred_and_out_of_range_templates_fail_explicitly` | Easy, Recovery, Long Run, and Strides are active; Threshold and VO2 remain deferred until intensity and quality-density validation is connected |
| Eligibility | `test_template_eligibility_and_time_budget_are_enforced` | Expansion requires explicit running history, time budget, required facts, absence of contraindications, and a clear safety state |
| Determinism | `test_active_templates_expand_deterministically` | Identical knowledge version, eligibility, and parameters produce identical IDs, definition, guidance, estimate, and canonical JSON |
| No false precision | Template and route tests | Time-based RPE workouts do not invent distance, pace, threshold, HRmax, or race performance |
| Load estimate | `tests/test_workout_templates.py` | Intensity-domain time is nonnegative and sums to duration; distance can remain unknown; session RPE and confidence remain explicit |
| Constraint execution | Hypothesis and registered-engine tests in `tests/test_workout_templates.py` | Active implementation IDs resolve to Python code; adaptation cannot increase duration, distance, intensity, or density; missing distance baseline remains unknown |
| V1/V2 compatibility | `test_v1_v2_parsing_and_hash_include_local_guidance`, existing workout tests | Existing V1 definitions remain strict and readable; V2 adds RPE ranges and local instructions; mismatched versions fail closed |
| Shared UI round-trip | `tests/test_phase7_routes.py` | V2 content survives create, validation errors, revisions, preview, and editor rendering and participates in the content hash |
| Garmin degradation | `test_garmin_compiler_degrades_rpe_and_instructions_explicitly`, `test_v2_workout_round_trip_preview_and_garmin_degradation` | RPE degrades to no device target; local instructions remain in PacePilot; both limitations are visible before transfer |
| Deployment | Startup tests and `Dockerfile` | Production images include `knowledge/`; invalid bundled knowledge prevents startup before migrations and scheduler launch |

## Template Policy

- `easy_run`: 20-90 minutes, default 45, RPE 2-3, full-sentence Talk Test.
- `recovery_run`: 15-45 minutes, default 25, RPE 1-2, only when easy running is habitual and genuinely recovery-supportive.
- `long_run`: 60-120 minutes in the initial active registry, RPE 2-3, only with a sufficient recent long-run baseline.
- `strides`: easy running plus 4-8 controlled 15-25 second strides with 60-120 seconds recovery; never all-out.
- `threshold_cruise`: represented but deferred; typical total work is constrained to 15-40 minutes before activation.
- `vo2_intervals`: represented but deferred; typical total hard work is constrained to 10-25 minutes before activation.

All exact ranges are versioned Product Heuristics within report-backed physiological principles. The
future candidate validator remains responsible for determining whether one concrete instance fits
one athlete.
