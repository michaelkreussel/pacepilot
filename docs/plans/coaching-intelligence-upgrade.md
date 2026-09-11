# Coaching Intelligence Upgrade

## Status And Execution

**Planned, not implemented — 11 September 2026.** This document contains the agreed execution split
and two copy-ready prompts. The documentation refresh is preparation, not a third engineering
package. Update package status only after implementation and verification.

Read [current-state context](../refactoring/ai-coach-current-state.md) for the inspected code and
[coaching intent](../refactoring/ai-coach-intent.md) for the desired product outcome. Current code
remains authoritative for runtime behavior.

| Package | Goal/result | Scope | Dependency | Complexity | Reasoning | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Individualized daily recommendation with a visible Heute card, personal reasons and appropriate adaptation | Running analysis/intensity, proposal dose, adaptation, coach tool and minimal UI | Independent | Large | Astra High | Pending |
| 2 | Consistent daily/weekly intelligence, exact preview persistence and training-first coach UX | Weekly/cycle integration, overview, alternatives and secondary chat | Package 1 | Large, primarily integration | Astra Medium | Pending |

Package 1 earns High because training history, recovery, intensity evidence and requested-draft
semantics interact. Package 2 reuses its decision policy. Each package delivers a working vertical
slice; neither is architecture cleanup. Do not add speculative time estimates or more phases.

**No new libraries, databases, external services or runtime dependencies. No schema migration is
planned.** Use existing models and revision JSON fields. Ask before any concrete blocker would
require a dependency or schema change. Research sources are development knowledge, not production
integrations.

Copy the entire appropriate fenced prompt below into OpenCode. Each prompt is self-contained about
constraints and verification. Execute Package 1 before Package 2.

## Prompt 1 — Individualized Daily Recommendations

```text
Implement Package 1 of the PacePilot coaching upgrade end-to-end.

GOAL

Opening /coach should show an athlete-specific recommendation for today without
requiring an LLM call or a conversation. Workout selection, duration, work volume,
and intensity must be explainable and respond to recent training.

Deliver a minimal visible “Heute” card in this package. Do not stop at backend
scaffolding. Use Astra High for the cross-cutting training decisions.

REPOSITORY CONTEXT

Read AGENTS.md, docs/refactoring/ai-coach-current-state.md and
docs/refactoring/ai-coach-intent.md. Treat current code as authoritative.

Relevant current code:
- app/services/analytics/athlete_data.py:
  AthleteDataService, get_running_shadow_analysis(), get_adaptive_context()
- app/services/analytics/running_baseline.py:
  7/28/56/180-day running windows, medians, coverage, interruptions, reentry
- app/services/analytics/running_intensity.py:
  get_running_intensity_guidance(), PerformanceAnchorLike, _critical_speed()
- app/services/analytics/activity_semantics.py:
  is_running_sport(), is_hard_activity(), hard_activity_data_available()
- app/services/analytics/subjective_feedback.py:
  effective_activity_feedback()
- app/services/planning/planning_queries.py:
  goals, profile, availability, performance anchors, accepted cycle state
- app/services/planning/workout_proposals.py:
  RunningProposalService.create(), revise(), _build_candidate(), _metadata()
- app/services/planning/workout_templates.py:
  TemplateParameters, expand_workout_template()
- app/services/planning/training_fit.py: assess_training_fit()
- app/services/planning/daily_adaptation.py:
  DailyAdaptationService, generate_daily_adaptation_candidates(), reduce_volume()
- app/services/planning/constraints.py
- app/services/planning/workout_service.py:
  create_proposal(), revision/acceptance/scheduling/Garmin lifecycle
- app/services/planning/workout_definition.py and workout_views.py
- app/services/coach/tools.py and provider.py
- app/routes/coach.py
- app/templates/coach.html and app/templates/workouts/_coach_proposal_card.html

The six existing registry formats are sufficient. Reuse knowledge/workouts/*.yaml.
Do not add another workout taxonomy.

IMPORTANT EXISTING BEHAVIOR

Current explicit requested drafts remain available with advisory history/recovery
warnings. Tests intentionally assert this. Preserve that capability.

Add automatic recommendation selection to the existing planning/proposal code;
do not globally turn template eligibility metadata into refusal gates.

Share candidate expansion, intensity personalization, validation, metadata, and
persistence between explicit drafts and automatic recommendations. Do not build
a parallel workout generator.

IMPLEMENTATION

1. Add a small read-only recommendation result and public operation in the
existing proposal/planning area. It should describe:
- state: workout, rest, completed, existing scheduled workout, or clarification;
- chosen format and exact structured definition where applicable;
- total duration, work volume, intensity guidance;
- dated decision reasons, source/confidence, assumptions;
- context fingerprint and any referenced workout/revision.

Use one trusted as_of date throughout. Page views must not write proposals or
call Garmin/OpenRouter.

2. Load meaningful inputs:
- running-only recent and historical duration/distance/frequency;
- typical session and long-run size;
- actual hard activity dates, with effective manual/Garmin feedback;
- recent long runs and interruptions;
- accepted scheduled work, using accepted revisions;
- active goal and cycle phase where available;
- explicit availability, including days marked unavailable;
- current recovery evidence and recent subjective reports;
- stored performance anchors.

get_planning_inputs() returns available days only. Use list_availability() where
distinguishing “unavailable” from “not configured” is necessary.

Keep other-sport fatigue separate from running volume. Do not count cycling
kilometers or duration as established running capacity. Missing sync coverage,
missing RPE, or absent linkage is not zero training or a proven missed session.

3. Automatic selection precedence:
- If a run is completed today, show it; do not automatically prescribe a second.
- If an accepted scheduled session exists, present that exact accepted content
  and an adaptation recommendation, not an unrelated new session.
- Existing accepted plan intent takes precedence over generic goal selection.
  An unaccepted child workout is still a proposal, not scheduled training.
- Explicit unavailability or zero available time yields rest.
- Serious current illness/pain feedback or corroborated poor recovery favors rest.
- Recent demanding activity, recent long-run stress, reentry, or unusually high
  observed volume suppresses new quality work.
- Otherwise select from existing formats according to goal, recent exposure,
  consistency, frequency, preferred long-run day, and available time.

Use the existing 48-hour spacing heuristic conservatively. Stored scheduling is
date-based: do not claim precise hourly recovery when only dates are known.
Check completed activities across the week boundary, plus accepted planned work.
Short strides are not equivalent to a full VO2 session.

Start automatic quality allocation conservatively: at most one sustained
threshold/VO2 session in a rolling seven-day window. Do not automatically add
a recovery run that would increase an athlete's habitual frequency.

For general fitness prioritize easy running and an appropriately scaled long run.
For established race-focused runners allow threshold work; allow VO2 work for
appropriate 5K/10K context and adequate recent quality experience. Use previous
exposure to avoid repeating the same stimulus blindly. Do not infer a specific
interval archetype solely from a high Garmin training-effect score.

When no availability is stored, use a clearly labeled provisional time budget
derived from normal session size, with an editable 30-minute fallback. A lack
of health metrics alone must not block an ordinary easy recommendation.

4. Athlete-specific dose:
Treat available_minutes as a ceiling for automatic recommendations.

Use robust recent session/weekly/longest-run statistics, consistency, reentry,
and previous comparable completions to determine dose. Historical fitness must
not override a recent interruption.

Extend TemplateParameters only with bounded fields needed to individualize
interval work duration and, if necessary, recovery/warmup/cooldown. Validate
against existing registry numeric ranges and total-work bounds.

Both total time and hard work must fit. Preserve appropriate warmup/cooldown.
If the minimum valid quality session does not fit, select an easier archetype
instead of squeezing preparation or exceeding the budget.

Behavioral examples:
- An inconsistent runner around two 25-minute runs/week should receive a modest
  easy recommendation, not the same intervals as a consistent high-volume runner.
- Eligible lower-volume and higher-volume runners should receive materially
  different threshold doses, such as 3x5 versus 5x6 minutes when evidence and
  time permit, rather than only different paces.
- A two-hour free slot must not cause a two-hour long run for an athlete whose
  recent longest run is 40 minutes.
- If long-run history is below the registry's 60-minute minimum, fall back to an
  appropriately sized easy run; do not force the long-run minimum.

Use named, versioned conservative heuristics. Any percentage growth ceiling is
a product limit, not an injury-safety claim or a mandatory weekly increase.
Do not implement an ACWR “safe zone” or rigid intensity-distribution percentage.

Progress only one load axis at a time, and only when observed training and
comparable successful completion support it. Do not catch up missed work.
Without reliable completion evidence, maintain rather than claim progression.

5. Intensity:
Pass stored performance anchors into single-workout analysis.

Produce executable PaceRangeTarget or HeartRateRangeTarget only when justified.
Preserve RPE/talk-test instructions alongside device targets.

Use a distance-aware hierarchy:
- recent reliable race/time-trial performance appropriate to the target;
- fresh plausible Garmin threshold speed;
- cautiously used recent Garmin PR evidence, explicitly labeled as a PR rather
  than a confirmed independent maximal test;
- validated personal HR profile/threshold-HR guidance where appropriate;
- RPE/talk test.

An arbitrary 1km PR, easy-run average pace, or Garmin predicted race result must
not become threshold pace. A single 5K estimate must not be called Critical Speed.
VO2max and race predictions remain corroborating context, not precise pace authorities.

For this package, do not use Critical Speed to prescribe workouts. Tighten
availability/reporting in _critical_speed() so unsuitable pairs are not reported
as robust CS. Test inadequate durations, duplicated/same-event evidence, stale
or inconsistent anchors, and a single performance. If source independence cannot
be established from stored data, report uncertainty rather than invent it.

For a supported single race estimate, use a documented distance-aware conversion
and a rounded range; never reuse raw race pace indiscriminately across formats.
Keep freshness, source, confidence, and calculation provenance in guidance.
Reject nonfinite, implausible, or inverted targets.

Reuse existing Garmin HR-profile validation and account-principal binding.
Garmin zone 2 is a configured device zone, not proof of a measured physiological
threshold. Do not create precise HR targets for short strides/VO2 repetitions.

6. Recovery/adaptation:
Reuse assess_training_fit() evidence and coverage. Separate actual adverse
evidence from coverage-only warnings. Missing health data must not automatically
trigger a shorter/easier workout.

Use dated recent training in adaptation decisions, not only in fingerprints.
An unscheduled demanding activity yesterday must affect today's planned quality
session. An unknown-intensity activity must not be labeled easy.

Normal evidence: keep appropriate work.
Mild supported fatigue: reduce dose.
Clearly poor corroborated recovery: easier work or rest.
Serious/persistent adverse evidence: rest with a clear explanation.

Preserve existing explicit adaptation actions and accepted-revision boundaries.
Improve volume reduction for generated intervals by reducing repetitions/work
first instead of proportionally shrinking all preparation and recovery steps.
When no valid reduced session fits, offer easy/rest.

7. Explanation and persistence:
Generate concise German reasons from the actual decision: why this stimulus,
today, intensity and dose, and how recent training/goal affected it.

Persist selected parameters, target provenance, reasons, and relevant context
in existing revision JSON fields. Old revisions must remain readable.

Expose read-only preview and an idempotent POST to save the recommendation as
an ordinary unaccepted, unscheduled proposal through WorkoutService.
Use current context on POST; if the preview materially changed, return the
updated preview rather than silently saving a different session.
Do not invent assistant-message provenance for a non-chat recommendation.

A repeated unchanged submission should reuse its proposal. A rejected proposal
must not be silently resurrected, and accepted content must not be overwritten.

8. Coach integration and minimal frontend:
Add a compact prominent “Heute” card to /coach, outside the provider-configured
condition. It must work with no LLM API key.
Show recommendation/rest/completed/scheduled state, duration, intensity, structured
steps, and two or three personal reasons.
Offer “Als Vorschlag speichern” for a new recommendation and existing detail/
adaptation actions for scheduled work.

Keep this package's layout change small; Package 2 makes chat secondary.

Update coach tools/provider prompts so “What should I do today?” reads the same
deterministic recommendation and explicit requests to create it save that result.
Remove the automatic easy_run fallback for recommendation intent.
Keep explicit format requests supported. The LLM cannot supply numerical workout
parameters or bypass the deterministic result.
Return enough decision summary to explain the result, not only an artifact ID.

CONSTRAINTS

Preserve the existing architecture unless a change is necessary for this feature.
Prefer modifying and reusing existing code over introducing parallel systems.
Do not introduce abstractions, services, repositories, interfaces, agents,
infrastructure, or migrations unless they solve a concrete problem identified
in the existing implementation.
Complete the requested implementation end-to-end rather than stopping after
scaffolding or creating TODOs.
External datasets and APIs mentioned in this task are research and reference
sources by default. Do not introduce runtime dependencies on them without
a concrete need.
Keep the scope focused on measurable improvements to coaching quality.

Do not add libraries, databases, external services, or runtime dependencies.
Implement this upgrade using the existing stack, database schema, and workout
models. If a concrete blocker appears to require a dependency or schema change,
explain it and ask before proceeding. No migration is planned.

Preserve dashboard behavior, authentication, CSRF, user scoping, Garmin sync,
historical data, workout builder, and explicit Garmin authorization.
No model/provider replacement, background coaching job, or broad refactor is needed.

TESTS AND DEFINITION OF DONE

Add meaningful behavioral tests using existing in-memory fixtures:
- different training levels produce different dose at the same available time;
- recent hard/long work changes selection and scheduled-work adaptation;
- missing health data alone does not force reduction;
- corroborated poor recovery and explicit serious feedback produce appropriate
  easy/rest recommendations;
- reentry, incomplete history, short time budgets, and long-run bounds;
- performance-anchor wiring, source-aware fallbacks, finite ordered pace/HR
  targets, and no fake CS;
- exact structured duration/work totals and valid Garmin compilation;
- same inputs give the same recommendation;
- GET creates no proposal and needs no provider;
- POST idempotency, stale preview, user isolation, CSRF, and accepted-revision
  preservation;
- existing requested-draft behavior remains usable.

Relevant suites:
tests/test_running_baseline.py
tests/test_workout_templates.py
tests/test_workout_proposals.py
tests/test_training_fit.py
tests/test_daily_adaptation.py
tests/test_training_agent.py
tests/test_workout_revisions.py
tests/test_workout_service.py

Add tests/test_daily_recommendation.py if this clarifies behavioral test ownership.

Run focused tests during implementation. Review the resulting diff once.
Rebuild CSS with npm run build:css and update relevant static cache keys.
Then run final checks once after code changes:
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check

If an explicitly approved model/migration change occurred, also run:
uv run pytest tests/test_migrations.py

Verify the user flow through route tests; visual browser verification is optional.
Report backend/frontend impact, exact manual steps and expected results, and the
public recommendation/parameterization operations Package 2 should reuse.
Update package status in docs/plans/coaching-intelligence-upgrade.md after required
verification, and refresh current-state documentation for the shipped behavior.
Do not commit unless requested.
```

## Prompt 2 — Weekly Consistency And Training-First Coach UX

```text
Implement Package 2 of the PacePilot coaching upgrade end-to-end, after Package 1.
Use Astra Medium: reuse Package 1's established decision policy.

GOAL

Make /coach a training-first page showing today's training, this week's actual
and planned work, useful adjustments, and optional chat.

Make generated plans use Package 1's athlete-specific dose and target logic.
The workout saved from a plan must be exactly the workout that was previewed.

STARTING CONTEXT

Read AGENTS.md and inspect the completed Package 1 implementation and tests.
Read docs/refactoring/ai-coach-current-state.md and ai-coach-intent.md.
Reuse Package 1's public recommendation result, parameterization, intensity,
explanation, and context-fingerprint behavior. Do not redesign that policy.

Relevant areas:
- app/routes/coach.py and app/templates/coach.html
- app/services/coach/presentation.py
- app/templates/coach/_message.html and other coach artifact partials
- app/templates/workouts/_coach_proposal_card.html
- app/static/js/coach.js and existing SSE parser/tests
- app/services/planning/weekly_planner.py:
  WeeklyPlannerSnapshot, PlannedSessionCandidate, compose_week(), plan_shadow_week()
- app/services/planning/multiweek_planner.py:
  compose_training_cycle(), _insert_phase_quality(), _apply_phase(),
  progression/taper helpers
- app/services/planning/weekly_plan_service.py:
  persist_week_candidate(), _persist_week_candidate()
- app/services/planning/planning_queries.py
- app/services/planning/workout_views.py
- app/repositories/workouts.py:
  workouts_between() projects accepted revisions as CalendarWorkout
- app/services/analytics/progress.py
- existing activity and feedback queries
- app/routes/plans.py, workouts.py, feedback.py

Pre-upgrade pitfalls to address:
- Standalone weekly composition mainly produces easy/long/strides sessions.
- Multiweek quality insertion expands default threshold/VO2 sessions.
- Weekly persistence re-expands intervals/strides using default parameters.
- Plan/cycle acceptance does not accept or schedule child workouts.
- Completion matching depends on activity.workout_id and can be incomplete while
  Garmin detail enrichment is pending.

IMPLEMENTATION

1. Share workout dose and intensity across single and planned sessions.

Integrate Package 1's parameterization into weekly and multiweek generation.
Keep existing plan/cycle models, revision flow, phases, and Garmin lifecycle.

Pass enough athlete context and explicit selected parameters through existing
candidate dataclasses to preserve:
- exact definition;
- total/work duration and load estimate;
- personalized targets and provenance;
- purpose and reasons.

Persist the exact validated candidate, rather than reconstructing intervals/
strides from defaults. If carrying a definition on PlannedSessionCandidate is
the simplest solution, use that. No new persistent domain model is needed.
Candidate fingerprints must include executable content and relevant metadata.

Verify preview, persisted revision, card, and Garmin compilation agree.

2. Weekly coherence.

Use Package 1's shared selection/eligibility and dose policy, not another scoring
system. Respect available days, habitual frequency, accepted work, observed running
volume, long-run history, recent quality, goal, and existing cycle phase.
For suitable race-focused athletes, a standalone weekly plan may contain the
appropriate bounded quality session instead of requiring a multiweek cycle.

Assess rolling quality spacing across week boundaries, including completed hard
activities and accepted scheduled sessions. Do not count a linked completed
session twice as both planned and performed. Keep short strides distinct from
sustained hard sessions. Use separate volume and intensity accounting.

Start a generated week from observed sustainable volume. Do not fill every free
minute or assume large availability means capacity. Apply existing progression/
taper limits together with Package 1's dose ceilings. No automatic catch-up.

For a current-week plan, account for completed work and remaining accepted work;
do not create new recommended sessions in the past.
For future weeks, today's recovery is not a prediction of future readiness.
Generate a nominal plan from the as_of snapshot and apply current recovery when
the session becomes today's recommendation.

If planned quality cannot fit the athlete's dose/time envelope, substitute an
appropriate easy session and explain it. Do not retain default-sized intervals
with only a warning.

Historical accepted revisions remain unchanged. New plan revisions use improved
logic and the existing explicit acceptance flow.

3. Training-first /coach page.

Make GET /coach the training overview. Keep existing conversation URLs and message
endpoints working; provide a visible secondary “Coach fragen” entry.

Use existing Jinja/Tailwind patterns and shared partials. No SPA/framework.

Primary sections:
- “Heute”: Package 1's recommendation or exact accepted scheduled session;
  completed/rest/insufficient-data states must be useful too.
- “Warum diese Einheit?”: personal reasons, target source, important uncertainty.
- “Diese Woche”: Monday-Sunday completed running activities and existing planned
  workouts, with links to activity/workout details.
- A compact goal/current-phase/progress summary when real data exists.

Distinguish completed, scheduled, proposed, recommended, and rest states.
Use accepted revisions for execution and show pending edits separately.
Do not call an unlinked workout missed merely because Garmin enrichment or
linkage is incomplete. Do not represent unaccepted plan children as scheduled.

If no plan exists, show actual work and today's recommendation; do not invent
precise future sessions to fill the week. Offer the existing plan-creation flow.

Opening the page must not create conversations, plan revisions, or proposals,
and must not call OpenRouter or Garmin. It must work without an LLM API key.

Keep the existing chat renderer, streaming parser, history, feedback, and tools.
Move chat behind a tab/link or into a secondary section instead of rewriting
its protocol. Scope new layout to the coach; preserve the dashboard layout.

4. Practical alternatives.

Provide “Kürzer” and “Leichter” for today's recommendation.
- Shorter: accept a smaller time ceiling and use Package 1's bounded dose logic.
- Easier: lower stimulus/load; select easy/recovery/rest as appropriate.
- Never exceed original load while presenting an easier/shorter option.
- Preserve adequate interval preparation; use an easier alternative when the
  minimum valid interval session cannot fit.

Preview alternatives read-only. Persist only the chosen option via the shared
proposal path and its idempotency/context checks.

For accepted scheduled work, reuse DailyAdaptationService and existing
/workouts/{id}/adaptation/apply actions. Do not overwrite accepted content or
silently change Garmin content. Show what changes, why, and duration/work
difference before the user applies it.

Retain context when asking chat about a workout through an explicitly scoped
workout/revision reference. Do not rely on an ambiguous “this one” from stale
prose. The coach should read the same fresh recommendation, not invent another.

5. Presentation consistency.

Reuse workout_views.py and existing artifact/lifecycle projections.
Extend target labels to summarize interval pace/HR/RPE targets instead of always
showing “Lokale Intensitätsleitplanken”. Show repeat blocks compactly rather than
displaying every repetition separately. Humanize purpose keys/reasons in German.

Keep persisted guidance and live preview consistent. Existing revisions without
new explanation fields must render with sensible fallbacks.

CONSTRAINTS

Preserve the existing architecture unless a change is necessary for this feature.
Prefer modifying and reusing existing code over introducing parallel systems.
Do not introduce abstractions, services, repositories, interfaces, agents,
infrastructure, or migrations unless they solve a concrete problem identified
in the existing implementation.
Complete the requested implementation end-to-end rather than stopping after
scaffolding or creating TODOs.
External datasets and APIs mentioned in this task are research and reference
sources by default. Do not introduce runtime dependencies on them without
a concrete need.
Keep the scope focused on measurable improvements to coaching quality.

Do not add libraries, databases, external services, or runtime dependencies.
Implement this upgrade using the existing stack, database schema, and workout
models. If a concrete blocker appears to require a dependency or schema change,
explain it and ask before proceeding. No migration is planned.

Preserve Garmin sync, history, dashboard, authentication, CSRF, workout builder,
SQLite, single-worker deployment, and explicit external actions. No scheduler
additions, recommendation database, model replacement, or broad planner refactor.

TESTS AND DEFINITION OF DONE

Add focused behavioral tests proving:
- weekly/cycle sessions reuse athlete-specific parameters and target provenance;
- exact preview-to-persistence-to-compilation agreement, including repetitions;
- novice/experienced plans differ materially;
- completed and accepted work is counted once;
- quality spacing spans Sunday/Monday;
- current-week generation does not prescribe past/catch-up sessions;
- long-run/weekly volume bounds and taper remain coherent;
- future sessions are not reduced solely by today's transient readiness;
- accepted content survives new drafts unchanged;
- overview renders without provider configuration, a plan, or health data;
- completed/proposed/scheduled/rest states and uncertainty are correctly labeled;
- shorter/easier alternatives reduce load and preserve valid structure;
- stale submissions, CSRF, idempotency, and cross-user references are handled;
- old conversations, artifacts, and workout revisions still render.

Relevant suites:
tests/test_weekly_planner.py
tests/test_weekly_plan_service.py
tests/test_multiweek_planner.py
tests/test_progress.py
tests/test_daily_adaptation.py
tests/test_training_agent.py
tests/test_workout_revisions.py
tests/test_routes.py
tests/test_static_theme.py
Package 1 recommendation tests

Add tests/test_coach_overview.py if useful.

Run focused tests during implementation. If changing chat JavaScript, run:
node --test tests/coach_sse.test.mjs

Review the resulting diff once. Rebuild CSS:
npm run build:css
Update relevant static cache keys.

After all code changes, run:
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check

If an explicitly approved model/migration change occurred, also run:
uv run pytest tests/test_migrations.py

Verify the end-to-end flow through route tests. Visual browser testing is optional.

Report backend/frontend changes, commands and verification results, and exact
manual steps: open /coach, inspect today/week, preview shorter/easier, save a
proposal, accept/schedule through existing actions, reopen after new activity/
feedback, and use secondary chat. State expected results, including that page
views do not publish to Garmin.

Update package status in docs/plans/coaching-intelligence-upgrade.md after required
verification, and refresh current-state documentation for the shipped behavior.
Do not commit unless requested.
```
