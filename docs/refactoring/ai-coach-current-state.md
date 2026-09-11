# AI Coach Current-State Context

## Purpose And Authority

Implementation inspected on **11 September 2026**, before the coaching-intelligence upgrade.
This is a current implementation map, not a target architecture or a claim that planned work has
shipped. Prefer application code and migrations, then behavioral tests, then this document.

- [AI Coach architecture](../ai-coach.md) describes the conversational boundary and its invariants.
- [Coaching intent](ai-coach-intent.md) describes the desired product outcome.
- [Coaching intelligence upgrade](../plans/coaching-intelligence-upgrade.md) contains the two planned
  implementation packages and copy-ready execution prompts.

This map replaces the August assessment. Resolved findings are retained in Git history, not as an
active cleanup backlog. Completed historical phase plans, gate matrices, completion reports, and
the superseded refactor specification/plan were removed on 11 September 2026 to reduce agent
context; they remain recoverable in Git history. They do not override current code or the new
upgrade direction.

## Application And Coach Boundary

PacePilot is a Python 3.12 FastAPI/Jinja application using SQLAlchemy and local SQLite. APScheduler
and a thread pool run Garmin synchronization in the same process. Deployment uses exactly one
Uvicorn worker. The German UI uses committed Tailwind assets and existing browser components.

| Area | Current responsibility |
| --- | --- |
| `app/main.py`, `app/config.py`, `app/database.py`, `app/auth.py` | Startup, cached settings, persistence, and signed-session Google/GitHub authentication |
| `app/routes/coach.py` | Conversation pages/CRUD, message submission, SSE delivery, artifact HTTP actions |
| `app/services/coach/conversation.py` | `prepare_execution()`, bounded history, trusted `CoachRuntimeContext`, stale-response repair |
| `app/services/coach/agent.py` | Provider-neutral `CoachAgent` protocol and `CoachEvent` |
| `app/services/coach/provider.py` | `OpenRouterCoachProvider`, prompts, LangChain tools and stream adaptation |
| `app/services/coach/tools.py` | User-scoped analytics, planning, feedback, proposal/revision, and adaptation operations |
| `app/services/coach/presentation.py` | Durable artifact and message presentation |
| `app/models/coach.py`, `app/repositories/coach.py` | `CoachConversation`, `CoachMessage`, artifact provenance, execution state |
| `app/services/analytics/` | Athlete data, running history/intensity, health/recovery, feedback, progress |
| `app/services/planning/` | Templates, proposals, plans/cycles, adaptation, immutable revisions, workout lifecycle |
| `app/services/garmin/` | Authentication, import/backfill, account locks, workout compilation and external operations |

The Coach can read and manage explicit goals, profile, availability, and performance anchors; record
pre/post-session feedback; create/revise deterministic workouts and plan drafts; and assess daily
adaptation. It cannot author executable steps or use prose to accept, schedule, replace, publish,
or push content. Those remain explicit authenticated artifact/workout/plan actions.

The former assistant-run and per-tool telemetry entities, direct proposal form, and
`/coach/planning-shadow` route are gone. Current `app/config.py` has no old per-capability Coach
rollout/history/template flags. Do not recreate them as prerequisites for coaching improvements.

## Garmin And Athlete Data

### Authentication And Synchronization

The unofficial `garminconnect[workout]` dependency is locked to **0.3.8** in `uv.lock`.
`start_garmin_login()` / `finish_garmin_login()` in `garmin/client.py` support credential login and
MFA. Pending challenges are account/user-scoped in-process state with a ten-minute lifetime.
Token files live under `GARMIN_TOKEN_DIR/account-<account_id>/`; passwords are not stored in SQLite.
`connect_garmin_account()` restores the account session.

`sync_garmin()` in `garmin/sync.py` orchestrates:

```text
Garmin login -> activity history -> health history -> performance metrics
            -> HR-zone profiles -> personal records -> devices
```

`jobs/scheduler.py` queues account sync and repairs interrupted/stale operations; it does not
generate coaching recommendations. Default periodic sync is every 60 minutes. Preserve per-account
locks, pacing, cooldowns, resumable state, `SyncRun`, `SyncEvent`, and `GarminSyncState`.

Relevant endpoints: `POST /settings/garmin/connect`, `/settings/garmin/mfa`,
`/settings/garmin/sync`, and `GET /settings/sync-status`.

### Activity History

`sync_activity_history()` in `garmin/activity_backfill.py` calls `count_activities()` and paginated
`get_activities()`, fingerprints summaries, and maintains resumable history state. Optional
enrichment fetches activity details, splits, typed splits, and zones.

- `Activity` stores dates, sport, distance, duration/speed, HR, elevation, training effects/load,
  RPE/feel, and other available summary metrics.
- `ActivitySplit` and `ActivityZone` store enriched laps and zone durations.
- Compressed raw files live under `DATA_DIR/raw/activities/user-<user_id>/<year>/`.
- Detail enrichment defaults to zero activities during initial import and five per later sync.
  Summary availability does not imply detailed data or workout linkage is complete.
- `_map_detail_summary()` links `Activity.workout_id` through Garmin associated-workout IDs and
  remote bindings. Missing linkage is not proof that a planned session was missed.

### Verified Metric Paths

These are implemented ingestion/derivation paths, not guarantees that a particular account has
populated values. Missing, unsupported, partial, stale, and synchronized-empty data are distinct.

| Data | Source and current use |
| --- | --- |
| Volume, frequency, consistency, longest runs | `training_trends.py` and `running_baseline.py`; running windows of 7/28/56/180 days, medians/MAD, interruptions and reentry |
| Pace information | Stored activity/split speeds; no imported athlete-specific pace-zone profile or complete prescription-zone model |
| HR zones | `heart_rate_zones.py` imports validated running/default profiles onto `GarminAccount`; activity time-in-zone data is separate |
| VO2max, training status/load, acute/chronic load/ratio | `health_backfill.py` -> `DailyFitness`; wearable measurements, not injury-safety guarantees |
| Threshold speed/HR, race predictions | `performance_sync.py` -> `DailyFitness`; predictions are not race results |
| Performance results | `PerformanceAnchor` supports manual/race/time-trial inputs |
| Garmin PRs | `personal_records.py` imports accepted 1 km, mile, 5 km, 10 km and half-marathon records; automatic mapping does not include marathon |
| Sleep, HRV, resting HR, stress, Body Battery | `health_backfill.py` -> `DailyHealth`; health trends include personal baselines and source coverage |
| Recovery/readiness | Garmin readiness/recovery time plus deterministic local recovery analytics |
| Other available metrics | Respiration, SpO2, endurance/hill scores, fitness age, power and running dynamics |
| Subjective feedback | Garmin/manual effective effort/feel and stored pre/post-session reports; session-RPE load where measurable |

Imported PRs use `kind="manual", source="garmin"`; this does not establish independent maximal
race-test evidence. `AthleteGoal` stores event type, date and name, not a target finishing time.

`get_current_recovery_state()` / `preferred_readiness()` in `analytics/health_trends.py` prefer
current-day Garmin readiness. Otherwise local readiness combines available sleep, HRV, resting HR,
stress, Body Battery and recent hard-training context, with confidence and sleep-related limits.

## Conversation And Recommendation Flow

```text
POST /coach/{conversation_id}/messages
  -> conversation.prepare_execution()
  -> persist user + streaming assistant message
  -> OpenRouterCoachProvider + tool-selected AthleteDataService/planning context
  -> tools.create_running_workout_proposal()
  -> RunningProposalService.create() / _build_candidate()
  -> running analysis + requested registry template + deterministic expansion
  -> training-fit metadata -> WorkoutService.create_proposal()
  -> unaccepted, unscheduled Workout + immutable WorkoutRevision
  -> server-rendered artifact in chat -> explicit lifecycle actions -> Garmin
```

The provider assembles system, adaptive-context, proposal, planning, feedback, progress and
adaptation prompts. `get_adaptive_context()` includes durable planning/progress, scheduled work,
recovery, completed work and feedback according to focus. Data is selected through tools, not
automatically injected as an unrestricted full athlete history.

`LLM_API_KEY` and `LLM_MODEL` enable the provider. Current model default is
`z-ai/glm-5.3-flash`; routing is pinned to Z.AI without fallback. `_build_agent()` sets temperature
0.2, low reasoning effort, four model calls and six tool calls per run. These application settings
are independent of Astra's implementation-session reasoning effort.

Completed prose history is bounded to 20 messages / 12,000 characters before the current question.
Trusted user/date/session/request context stays outside model-visible arguments. `CoachMessage`
stores execution/provenance and `artifacts_json`; a partial unique index excludes concurrent
streaming answers in one conversation. Committed artifacts survive subsequent provider failures;
failed/interrupted partial prose is not reused as completed history.

## Workout Generation, Plans And Export

- Six active formats in `knowledge/workouts/`: `easy_run`, `recovery_run`, `long_run`, `strides`,
  `threshold_cruise`, `vo2_intervals`.
- `TemplateParameters` currently exposes duration and repetitions. `expand_workout_template()`
  validates numeric/template work-volume bounds and available time; it does not enforce the history
  and eligibility facts carried in `TemplateEligibilityContext`.
- Explicit requested drafts deliberately remain available with advisory history/recovery warnings.
  `tests/test_workout_proposals.py` protects that behavior.
- Single-workout duration mostly follows the time budget/template maximum; strides and intervals
  mostly use defaults, reducing repetitions to fit time. Threshold defaults to 4 x 6 minutes;
  VO2 defaults to 5 x 3 minutes.
- `running_intensity.py` selects a recent reliable performance anchor or fresh threshold speed when
  baseline confidence permits, otherwise RPE/talk test. VO2max/predictions are secondary. Single
  proposals do not pass stored anchors into `get_running_shadow_analysis()`; analyzed pace anchors
  do not currently become format-specific pace prescriptions. Easy runs can use personal Garmin
  zone-2 HR bounds with account-principal provenance.
- `_critical_speed()` uses a two-point shortest/longest performance calculation without sufficient
  duration/comparability/independence checks to serve as a robust prescription authority.
- `weekly_planner.py` derives habitual frequency/easy duration, bounds long runs against recent
  history and optionally places strides. `multiweek_planner.py` adds goal-dependent phases,
  volume/taper factors and threshold/VO2 insertion.
- `weekly_plan_service.py` persists plans/children but re-expands interval/strides formats with
  default parameters. Any future individualized preview must survive this persistence boundary.
- `progress.py` compares accepted cycle plans with linked activities and feedback, reporting
  coverage/linkage uncertainty. Accepting a plan/cycle does not accept or schedule its children.
- `DailyAdaptationService.assess_today()` assesses an accepted scheduled run for today. Candidate
  selection supports keep/reduce/easy/rest; history currently affects fingerprints, not directly
  the candidate-selection inputs.

`WorkoutDefinitionV2` already supports warmup, interval, recovery, cooldown, repeats, time/distance
endpoints, pace/HR/RPE targets and instructions. Nested repeats, open-ended steps and power targets
are not supported; none is necessary for the planned upgrade. `WorkoutRevision` already stores
purpose, guidance, load, validation, generation context and template/model provenance in JSON/fields.

`compile_workout_with_report()` in `garmin/workout_export.py` compiles these definitions through
`garminconnect.workout`, including pace-to-speed conversion. RPE becomes no device target and local
instructions remain in PacePilot, with reported compilation warnings. A parallel FIT model is not
needed.

`WorkoutService` owns creation, revisions, acceptance, local scheduling, adaptation, events,
idempotency and Garmin operations. `/workouts/{id}/confirm`, `/schedule`, `/publish`, and `/push`
remain separate POST actions. Editing accepted content does not replace its accepted execution
revision. Unknown external outcomes must not be blindly retried.

## Current UX And Highest-Impact Gaps

`coach.html` is a full-height chat page. `coach/_message.html`, `coach/presentation.py`, and
`static/js/coach.js` share server-rendered artifacts and SSE behavior. Users must initiate a
conversation; there is no proactive daily training decision. Proposal cards emphasize date,
duration, target summary and lifecycle actions rather than structured steps and personal reasons.

The dashboard independently uses recovery, weekly load and the next accepted scheduled workout.
It does not depend on a conversational recommendation and should remain intact.

The upgrade should address these concrete gaps:

1. Rich athlete analytics mostly annotate rather than determine single-workout dose.
2. The proposal prompt defaults to `easy_run` without an explicit type; requested draft creation
   does not provide automatic stimulus selection.
3. `quality_density_conflicts()` checks accepted generated quality work, not all demanding completed
   activities. Recent hard/long training does not consistently change selection/adaptation.
4. Coverage-only health warnings cause `CAUTION`; adaptation treats caution as a reduction/replacement
   signal. Missing observations must be distinguished from actual poor recovery.
5. Performance anchors/targets and personal explanations are incompletely connected. Plan generation
   uses a separate default-expansion path, and the UI hides the daily coaching outcome behind chat.

These are behavior-change opportunities, not a mandate for broad architectural cleanup.

## Verification And Invariants

Preserve authenticated user scoping, CSRF, explicit external authorization, immutable accepted
revisions, existing history, Garmin sync and database/filesystem cleanup. Settings are cached;
database/auth initialization happens at import time, so tests must arrange environment overrides
before imports and restore changed settings. No automated test needs live Garmin/OpenRouter access.

Relevant tests: `test_running_baseline.py`, `test_workout_templates.py`, `test_workout_proposals.py`,
`test_training_fit.py`, `test_daily_adaptation.py`, `test_weekly_planner.py`,
`test_weekly_plan_service.py`, `test_multiweek_planner.py`, `test_progress.py`,
`test_training_agent.py`, `test_coach_characterization.py`, `test_coach_architecture.py`,
`test_workout_revisions.py`, `test_workout_service.py`, and `coach_sse.test.mjs` under `tests/`.

Use focused tests during implementation and the final verification commands in `AGENTS.md`.
Rebuild CSS and update static cache keys after template/Tailwind changes. This documentation
inspection did not execute the test suite or live integrations.
