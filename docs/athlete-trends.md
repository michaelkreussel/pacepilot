# Athlete Trends and Coach Data Access

## Interface

`app.services.analytics.AthleteDataService` is the deterministic read boundary for the Profile page
and future AI-coach consumers. It is constructed with a SQLAlchemy session, user ID, and optional
`as_of` date. Supplying `as_of` gives every metric and activity query a deterministic upper boundary.

```python
analytics = AthleteDataService(session, user_id, as_of=date(2026, 8, 8))

analytics.get_current_recovery_state()
analytics.get_health_trends(days=28)
analytics.get_training_summary(days=28)
analytics.get_standard_training_summaries()
analytics.get_recent_workouts(limit=10)
analytics.get_weekly_running_volume(weeks=12)
analytics.get_training_load_trend(weeks=12)
analytics.get_hrv_baseline(days=28)
analytics.get_sleep_trend(days=28)
analytics.get_vo2max_trend(days=365)
analytics.get_activity_details(activity_id)
```

All results are immutable dataclasses. They contain compact values, dated trend points, normalized
activity children, explicit units, and synchronization coverage. They do not contain ORM objects,
Garmin credentials, raw file paths, sampled GPS tracks, or years of source payloads.

## Health Trends

The health result includes separate trends for:

- resting HR and nightly HRV;
- sleep duration and Garmin sleep score;
- Garmin stress, Body Battery high, and Body Battery charged;
- Garmin Training Readiness and recovery time when supported;
- VO2max.
- Garmin training, acute, and chronic load when supported.

Each metric reports the latest value within the requested window, its date, 7- and 28-day averages,
an 84-day personal baseline, difference from baseline, sample counts, and non-null chart points.
Averages ignore missing values but preserve a real zero. The personal baseline is the 84 calendar
days immediately before the latest seven-day window, preventing the current week from immediately
moving its own comparison baseline. Fewer available baseline samples are still used and are reported
in `baseline_sample_count`.

Resource coverage reports `not_synced` when no cursor exists, or the stored Garmin sync status,
backfill-complete flag, and oldest/newest covered dates. Newest coverage is capped at `as_of`.
Operational status and completion describe the current local store. This lets consumers distinguish
unavailable history from a confirmed empty metric.

`get_current_recovery_state()` returns the latest source values on or before `as_of`, including the
original Garmin HRV status/bands, Training Readiness score/level, recovery time, VO2max, Training
Status, and Garmin load fields. Sparse fitness metrics are selected independently and carry their
source dates rather than being erased by a newer row for another resource. Garmin values are never
reconstructed from other metrics.

## PacePilot Readiness

The current recovery result also contains a separate **PacePilot Readiness** score, label, confidence,
formula version `1.0`, and component breakdown. It is a transparent local heuristic, not Garmin
Training Readiness.

| Component | Base weight | Deterministic score |
|---|---:|---|
| Sleep duration | 20% | Sleep divided by Garmin sleep need, or personal sleep baseline, capped at 100 |
| Garmin sleep score | 10% | Original score, capped to 0-100 |
| HRV | 25% | 75 at personal or Garmin balanced-range midpoint, changing one point per percentage difference |
| Resting HR | 15% | 75 at personal baseline, minus five points per bpm above baseline |
| Garmin stress | 10% | `100 - stress_average` |
| Garmin Body Battery high | 10% | Original daily high, capped to 0-100 |
| Hard-training recovery | 10% | 45/60/75/85/95 for 0/1/2/3/4+ days since a hard workout |

A hard workout has aerobic Training Effect at least 3.5, anaerobic Training Effect at least 2.5,
or RPE at least 7. The training component is included only when activity history is completely
backfilled and every recent workout has Training Effect or RPE. Health inputs older than two days are
omitted. Every other missing component is also omitted, and available base weights are normalized to
100% rather than replacing missing values with zero.

Labels are `low` below 45, `fair` from 45, `good` from 65, and `high` from 80. Confidence combines
available base-weight coverage with the history of each contributing baseline component, reaching
full personal-history confidence at 28 baseline samples. Garmin sleep need and Garmin HRV baseline
are treated as complete upstream baselines. The component list exposes each score, source value,
baseline, unit, and normalized weight so callers can explain the result.

## Training Trends

Training windows are inclusive calendar days ending at `as_of`. Standard summaries cover 7, 28, 84,
183, and 365 days. A summary provides:

- workout count, active days, weekly frequency, active weeks, and consistency percentage;
- total duration and elevation;
- separate running and cycling distance;
- count, duration, distance, and elevation for each original Garmin sport type;
- moderate and vigorous intensity minutes;
- Garmin Exercise Load sum only when at least one source value exists;
- separate averages for aerobic and anaerobic Training Effect;
- hard-workout count and weekly frequency;
- HR and power zone seconds grouped by original sport, zone type, and zone number.

Missing metric values are ignored, not converted to zero. A zero running/cycling distance is returned
only when the window has no activity in that recognized sport family; if relevant activities exist
but all their distances are missing, distance remains null. Missing duration similarly remains null
unless the window is truly empty. Distances, cadence, power, and zone values are never combined
across incompatible sports. Total duration is intentionally sport-independent. Summary coverage
reports whether activity history has been synchronized and fully backfilled.

Weekly trends use Monday-through-Sunday buckets, including an explicitly partial current week. They
provide weekly running/cycling volume, duration, Exercise Load, Training Effect, hard-workout count,
longest run, and rolling 28-day duration/running distance. Empty weeks remain present for readable
charts and consistency calculations.

`get_training_timeline(days, bucket_days=...)` provides exact rolling-range buckets for UI views.
The first bucket begins at the selected range start and the final bucket is clipped and marked
partial, so Profile totals and chart bars use identical boundaries.

## Activity Drill-Down

`get_recent_workouts()` is the normal compact activity context. `get_activity_details()` is an
explicit user-scoped drill-down containing the activity summary plus normalized zones, laps/typed
splits, strength sets, and detail/split completion flags. Both methods honor `as_of`. The drill-down
deliberately does not load sampled Garmin chart/GPS JSON. A future coach should request this detail
only for selected workouts.
