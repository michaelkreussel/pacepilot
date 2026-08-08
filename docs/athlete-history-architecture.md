# Athlete History Architecture

## Scope

This document records the Task 2 storage and synchronization decisions. The migration and repository
primitives are implemented, but the Garmin backfills, trend calculations, PacePilot readiness
calculation, and Profile UI remain later sequential tasks.

## Storage Model

| Data | Storage | Rationale |
|---|---|---|
| Daily wellness and recovery | Expanded `daily_health` row per user/day | Most coaching queries need daily trends, not intraday Garmin arrays. Existing dashboard fields and rows remain compatible. |
| Sleep stage timeline | `sleep_stages` children of `daily_health` | Stage intervals are compact and support an accurate hypnogram without retaining all sleep movement or wellness samples. |
| Garmin fitness/readiness metrics | `daily_fitness` row per user/day | These values are sparse, device-dependent Garmin outputs and should not be confused with raw wellness measurements or PacePilot-derived values. |
| Activity summary | Expanded `activities` row keyed by user and Garmin activity ID | Preserves the existing stable external identifier and raw-file relationship while adding coaching-relevant metrics. |
| HR and power zones | `activity_zones` | A normalized zone number/type/seconds model works across sports and devices. |
| Laps and typed splits | `activity_splits` | Laps and intervals have meaningful future coaching value for pacing, progression, and intensity analysis. |
| Strength sets | `activity_exercise_sets` | The audited account contains useful set/repetition/weight/exercise data. |
| Historical cursor | `garmin_sync_states` row per user/resource | Tracks oldest/newest synchronized dates, resume cursor, attempts, success, errors, and backfill completion without adding a table per endpoint. |
| Daily completeness | `daily_data_statuses` row per user/day/resource | Distinguishes complete, valid empty, unsupported, and failed data from data that has never been requested. |

All Garmin values remain nullable. Missing SpO2, readiness, load, recovery, and advanced activity
metrics are not stored as zero.

## Activity Linking

`activities.associated_garmin_workout_id` preserves Garmin's completed-activity association.
`activities.workout_id` is an optional foreign key to the existing local workout and uses
`ON DELETE SET NULL`.

This supports the future relationship:

```text
planned PacePilot workout -> published Garmin workout -> completed Garmin activity
```

No matching or training-plan behavior is implemented in Task 2.

## Sync Semantics

Recommended `garmin_sync_states.resource` values are endpoint-oriented, for example
`daily_summary`, `heart_rate`, `stress`, `body_battery`, `respiration`, `spo2`,
`intensity_minutes`, `sleep`, `hrv`, `fitness`, `activities`, and `activity_details`.

`oldest_synced_date` and `newest_synced_date` describe the successfully covered range.
`backfill_cursor_date` is the next historical day to attempt. `cursor` is available for activity
pagination or another stable upstream cursor. `backfill_complete` only means the known upstream
boundary was reached; it does not imply every metric exists for every day.

Each endpoint/day result receives a `daily_data_statuses` status:

- `complete`: useful values were fetched and stored;
- `empty`: Garmin successfully confirmed no values for that day;
- `unsupported`: the account/device cannot supply the resource;
- `error`: the request failed and should be retried.

Repository methods do not commit. A backfill can update data, daily status, and its resume cursor in
one transaction, making restart behavior deterministic and idempotent.

Activity children are replaced as complete endpoint snapshots. The replacement primitives flush
deletions before inserting the same split/zone positions, avoiding SQLite uniqueness conflicts on
reruns. `details_complete`, `splits_complete`, `source_updated_at`, and detail timestamps allow Task 4
to skip unchanged complete activities.

## Readiness Scores

Garmin Training Readiness remains nullable in `daily_fitness`. The Forerunner 165 Music does not
provide it, so PacePilot will also calculate a separate deterministic **PacePilot Readiness** in
Task 5.

The PacePilot score must not be presented as Garmin Training Readiness and must not attempt to
reverse-engineer Garmin's formula. It should return a score, label, confidence, and component
breakdown from locally stored data:

- sleep duration relative to Garmin sleep need and personal sleep baseline;
- Garmin sleep score when present;
- nightly HRV relative to Garmin/personal baseline;
- resting HR relative to personal baseline;
- recent stress and Body Battery when present;
- recent locally calculated training strain and recovery balance.

Missing components should be omitted and remaining weights normalized, never replaced with zero.
Confidence should reflect available component coverage and baseline history length. This makes the
score useful on the current watch while clearly communicating when it rests on limited data.

The calculation is implemented by `AthleteDataService.get_current_recovery_state()` in the
deterministic analytics service rather than a database column. This prevents stale derived values
when baselines or the documented formula change. See `docs/athlete-trends.md` for its versioned input
weights and missing-data behavior.

## Sleep Presentation

The stored totals and stage intervals support a useful Profile presentation without frontend Garmin
aggregation:

- a categorical hypnogram with time on the horizontal axis and Awake, REM, Light, and Deep as
  discrete colored bands;
- a duration/percentage strip for each stage, with actual clock time and percentage of measured
  sleep;
- a bullet or progress comparison for actual sleep versus Garmin sleep need, where exceeding need is
  not visually treated as an error;
- the overall Garmin sleep score with compact component bars for duration, stress, awake count,
  restlessness, and stage percentages;
- aligned overnight HR, HRV, stress, or respiration traces only when useful data is available;
- weekly stacked columns showing total sleep and stage composition, plus a separate sleep-need line;
- explicit gaps for missing nights rather than zero-height bars.

The hypnogram should use accessible colors and direct labels, not rely on color alone. Awake periods
should remain visually distinct, and stage blocks should not be smoothed into a continuous curve
because sleep stages are categorical intervals.

## Deferred Work

Tasks 3 and 4 populated health/recovery and complete activity history. Task 5 provides health and
training trends plus PacePilot Readiness through a deterministic service. Task 6 now exposes those
summaries through the Athlete Profile; timezone-safe detailed sleep-stage presentation remains a
documented follow-up.
