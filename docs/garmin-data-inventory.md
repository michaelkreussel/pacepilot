# Garmin Data Inventory

## Scope

This is the Task 1 investigation only. It does not propose or implement a database schema,
historical synchronization, or an Athlete Profile page.

The audit was run on 2026-08-08 against the configured PacePilot Garmin account, using
`garminconnect 0.3.8` and a recent complete sample date of 2026-08-07. Account-specific dates
below are observations, not Garmin retention guarantees.

The checked-in audit script is `scripts/audit_garmin_history.py`. It:

- authenticates through the existing `app.services.garmin.client.connect_garmin()` integration;
- probes progressively older dates and narrows a boundary instead of querying every day;
- spaces requests, enforces a hard call budget, and stops on authentication or rate limiting;
- distinguishes successful empty responses, unsupported endpoints, authentication failures,
  rate limiting, and other API failures;
- stores only dates, response shapes, field names, activity types, and counts in
  `data/garmin-audit.json`; it does not store metric values, names, coordinates, device IDs, or
  profile IDs.

The complete live run used 178 wrapper calls. Follow-up probes corrected Body Battery placeholder
handling and explicitly checked steps, respiration, SpO2, intensity minutes, and request windows.
No authentication failure, rate limit, unsupported-endpoint response, or transient API failure
occurred during the complete run.

## Main Findings

- The account's continuous wellness history starts in late March 2026. Different metrics start on
  different days, consistent with device setup and metric-specific activation rather than Garmin
  retention truncation.
- Daily health, heart rate, stress, Body Battery, respiration, intensity minutes, sleep, sleep
  stages/score/need, overnight HR, and HRV are useful and populated.
- SpO2, Training Readiness, morning Training Readiness, recovery time, Training Status/load,
  Endurance Score, Hill Score, and Running Tolerance returned valid but unpopulated responses for
  this account.
- VO2max starts on 2026-07-01. Race predictions, fitness age, cycling FTP, and a running power
  threshold are available. Lactate-threshold speed and heart rate are not populated.
- The account contains 62 activities from 2026-03-30 through 2026-08-06. Both the oldest and newest
  sampled activities retain details, laps/splits, and HR zones.
- Recent running activity data is rich: pace/speed, HR, power, HR and power zones, elevation,
  cadence, grade-adjusted speed, running dynamics, Training Effect, VO2max, RPE/workout feel, laps,
  and typed splits are exposed.
- The sampled activity payloads do not contain Exercise Load (`activityTrainingLoad`), even though
  aerobic and anaerobic Training Effect are available.
- A per-request limit must not be interpreted as retention. Body Battery rejects ranges over 31
  inclusive days while still returning historical data in older valid windows. The steps wrapper
  similarly chunks requests into 28-day windows.

## Health Inventory

| Metric | Garmin method | Earliest data | Granularity | Request limits | Available for account | Notes |
|---|---|---:|---|---|---|---|
| Daily health summary | `get_user_summary(date)` | 2026-03-26 | Daily | One date per call | Yes | Broad summary for steps, calories, HR, activity time, stress, Body Battery, respiration, SpO2, and intensity minutes. Missing days can still return structural fields. |
| Steps | `get_steps_data(date)` | 2026-04-01 | 15-minute epochs | One date per call | Yes | Epoch fields include `startGMT`, `endGMT`, `steps`, `pushes`, and activity level. Boundary requires positive steps, so an earlier all-zero wear day is possible. |
| Daily steps range | `get_daily_steps(start, end)` | Not separately audited | Daily | Garmin endpoint limit is 28 days; library automatically chunks longer ranges | Yes through summary/epochs | Useful for efficient daily totals, but automatic chunking can generate many calls. |
| Calories | `get_user_summary(date)` | 2026-03-26 summary boundary | Daily | One date per call | Yes | `totalKilocalories`, `activeKilocalories`, `bmrKilocalories`, `wellnessKilocalories`, consumed/burned/remaining fields. Exact calorie-field boundary was not isolated from the summary boundary. |
| Resting HR | `get_rhr_day(date)`, `get_user_summary(date)` | 2026-03-26 HR boundary | Daily | One date per call | Yes | Dedicated response uses `WELLNESS_RESTING_HEART_RATE`; summary also has seven-day average. |
| Heart rate | `get_heart_rates(date)` | 2026-03-26 | Daily summary plus samples | One date per call | Yes | `minHeartRate`, `maxHeartRate`, `restingHeartRate`, seven-day resting average, and timestamp/value samples. |
| Stress | `get_all_day_stress(date)` / `get_stress_data(date)` | 2026-03-27 | Daily summary plus samples | One date per call | Yes | Average/max stress plus descriptor-based stress and Body Battery arrays. The two methods currently call the same endpoint. |
| Body Battery | `get_body_battery(start, end)` | 2026-03-27 | Daily totals plus samples/events | Observed maximum is 31 inclusive days; 32+ returns HTTP 400 | Yes | Includes charged/drained, timestamp/value samples, activity impact, and dynamic feedback. Old dates return placeholder rows whose samples are all null; these are empty, not historical data. |
| Body Battery events | `get_body_battery_events(date)` | Not separately bounded | Events plus stress/Body Battery samples | One date per call | Yes | Associates sleep/activity events with Body Battery impact and stress samples. |
| Respiration | `get_respiration_data(date)` | 2026-03-27 | Daily waking/sleep summary plus samples | One date per call | Yes | Waking and sleeping averages, high/low, sleep windows, and descriptor-based samples. |
| SpO2 | `get_spo2_data(date)` | None observed | Daily summary plus sample containers | One date per call | No recorded values | Endpoint works, but all recent 90-day probes had null/empty values. Do not store these as zero. |
| Intensity minutes | `get_intensity_minutes_data(date)` | 2026-03-30 | Daily and week-to-date totals | One date per call | Yes | Moderate/vigorous minutes, week goal, weekly totals, and sample descriptors. |
| Weekly intensity minutes | `get_weekly_intensity_minutes(start, end)` | Not live-bounded | Weekly | Range endpoint; no limit documented in client | Endpoint exposed | Better for reporting, but daily source is preferable for local historical persistence. |
| Hydration | `get_hydration_data(date)` | Not bounded | Daily | One date per call | Response populated | Includes goal, intake, activity intake, sweat loss, daily average, and last entry time. Some values may be derived goals rather than manual logs. |
| Body composition / weight | `get_body_composition(start, end)`, `get_weigh_ins(start, end)` | Not bounded | Measurements and range averages | Range endpoint; limit not established | Response populated | Weight, BMI, body fat/water, muscle/bone mass, visceral fat, metabolic age, and trend fields are exposed. |
| Blood pressure | `get_blood_pressure(start, end)` | None observed on sample date | Measurements | Range endpoint; limit not established | No measurements observed | Endpoint works and returned an empty `measurementSummaries` list. |
| All-day events | `get_all_day_events(date)` | Not bounded | Events | One date per call | Yes | Includes auto-detected activities, including events not recorded as full activities. |

## Sleep And Recovery Inventory

| Metric | Garmin method | Earliest data | Granularity | Request limits | Available for account | Notes |
|---|---|---:|---|---|---|---|
| Sleep session/duration | `get_sleep_data(date)` | 2026-03-28 | Sleep session | One date per call | Yes | Start/end local/GMT, confirmed window, total sleep, awake/unmeasurable time, naps, and device source. |
| Sleep stages | `get_sleep_data(date)` | 2026-03-28 | Stage totals and intervals | One date per call | Yes | Deep, light, REM, awake seconds and `sleepLevels` intervals. |
| Sleep score | `get_sleep_data(date)` | 2026-03-28 sleep boundary | Nightly | One date per call | Yes | Overall score plus duration, stress, awake count, REM/light/deep percentage, and restlessness qualifiers. |
| Sleep need | `get_sleep_data(date)` | Present on 2026-08-07; separate boundary not measured | Nightly/current and next night | One date per call | Yes | `sleepNeed` and `nextSleepNeed` include actual, baseline, HRV/nap/history adjustments, feedback, and training feedback. |
| Overnight HR | `get_sleep_data(date)` | 2026-03-28 sleep boundary | Nightly average and samples | One date per call | Yes | `dailySleepDTO.avgHeartRate`, resting HR, and `sleepHeartRate` samples. |
| Sleep stress / Body Battery | `get_sleep_data(date)` | 2026-03-28 sleep boundary | Nightly summary and samples | One date per call | Yes | Average sleep stress, sleep stress samples, Body Battery change, and sleep Body Battery samples. |
| Overnight respiration | `get_sleep_data(date)`, `get_respiration_data(date)` | 2026-03-28 sleep boundary | Nightly summary and samples | One date per call | Yes | Average/high/low respiration and epoch data. |
| Overnight SpO2 | `get_sleep_data(date)`, `get_spo2_data(date)` | None observed | Nightly summary/samples | One date per call | No recorded values | SpO2 fields/containers exist but are unpopulated for this account. |
| HRV | `get_hrv_data(date)` | 2026-03-26 | Nightly summary plus readings | One date per call | Yes | Last-night average, 5-minute high, weekly average, readings, sleep window, feedback, status, and Garmin baseline bounds. |
| HRV status/baseline | `get_hrv_data(date)` | 2026-03-26 HRV boundary | Nightly/rolling | One date per call | Yes | Preserve Garmin's status and baseline; do not reverse-engineer it. |
| Training Readiness | `get_training_readiness(date)` | None observed | Intraday snapshots | One date per call | No data in recent 90-day probes | When available, fields include score/level, sleep, recovery-time, acute-load ratio, HRV, and stress-history factors. Endpoint returns an empty list here. |
| Morning Training Readiness | `get_morning_training_readiness(date)` | None observed | Morning snapshot | Calls Training Readiness endpoint | No | Helper selects `inputContext == AFTER_WAKEUP_RESET`; sample returned `None`. |
| Recovery time | `get_training_readiness(date)` | None observed | Snapshot factor, minutes | One date per call | No | The client exposes recovery time only inside Training Readiness for this investigation. No populated readiness response means no reliable recovery-time history. |

## Fitness And Training Metrics

| Metric | Garmin method | Earliest data | Granularity | Request limits | Available for account | Notes |
|---|---|---:|---|---|---|---|
| VO2max | `get_max_metrics(date)` | 2026-07-01 | Daily effective metric | One date per call | Yes | Recent activity list also contains `vO2MaxValue`; 2026-08-07 was empty while 2026-08-06 was populated, so normal sync needs overlap. |
| Training Status | `get_training_status(date)` | None observed | Daily aggregate | One date per call | No populated data in recent 90-day probes | Top-level containers exist, but `mostRecentTrainingStatus` is unpopulated. |
| Training Load / Acute Load / load ratio | `get_training_status(date)` | None observed | Daily aggregate | One date per call | No populated data | Expected within training status/load-balance and Training Readiness ACWR fields. No useful values were returned for this account. |
| Training Effect | Activity list/detail methods | 2026-03-30 activity boundary | Per activity, also per lap in sampled oldest activity | Activity pagination/detail limits apply | Yes | Aerobic and anaerobic effects/messages are populated in both sampled activities. |
| Exercise Load | Activity list/detail methods (`activityTrainingLoad` when supplied) | None observed | Per activity | Activity pagination/detail limits apply | No in sampled oldest/recent activities | Do not substitute Training Effect for Exercise Load. |
| Endurance Score | `get_endurance_score(start, end=None)` | None observed | Precise daily; weekly when a range is used | Range limit not established | No | Sample date returned an empty object. |
| Hill Score | `get_hill_score(start, end=None)` | None observed | Daily; range stats | Range limit not established | No | Sample date returned an empty object. |
| Running Tolerance | `get_running_tolerance(start, end, aggregation)` | None observed | Daily or weekly | Range limit not established | No | Recent 28-day range returned an empty list. |
| Running lactate threshold HR/speed | `get_lactate_threshold(...)` | None observed | Latest or daily/weekly/monthly/yearly range | Range call performs three API requests | No | Latest speed and HR are null; historical speed and HR arrays were empty. |
| Running power threshold | `get_lactate_threshold(...)` | 2026-03-27 | Sparse threshold snapshot | Range call performs three API requests | Yes | Historical range returned functional threshold power/power-to-weight data for 2026-03-27. |
| Cycling FTP | `get_cycling_ftp()` | Latest value only; earliest not exposed by method | Latest | No date parameter | Yes | Current configuration/value is available. |
| Race predictions | `get_race_predictions(...)` | Daily rows returned back to tested lower bound 2026-03-01; true boundary not established | Latest, daily, or monthly | Maximum range is 366 days | Yes | 5K, 10K, half-marathon, and marathon predictions. The endpoint can return rows before wellness recording began, so requested-range coverage is not proof of device history. |
| Fitness age | `get_fitnessage_data(date)` | Not bounded | Daily/current derivation | One date per call | Yes | Fitness age, achievable age, previous age, and BMI/RHR/vigorous-activity components. |
| HR zones | `get_heart_rate_zones()` | Current configuration only | Per sport configuration | No date parameter | Yes | Zone floors and max/resting/lactate-threshold HR basis. Historical activities expose actual seconds in zones separately. |
| Power zones | `get_power_zones()`, `get_power_zones_for_sport(sport)` | Current configuration only | Per sport configuration | No date parameter | Yes | Four sport configurations returned; recent running activity had power time in five zones. |
| Personal records | `get_personal_record()` | Records reference activity dates; boundary not audited | Sparse records | No paging in method | Yes | Ten records returned with type, value, activity ID, and timestamps. |

## Historical Activities

### Endpoints

| Data | Garmin method | Observed behavior |
|---|---|---|
| Count | `count_activities()` | Returned 62. Best way to locate the oldest activity without downloading all pages. |
| Paged history | `get_activities(start, limit, activitytype=None)` | Offset pagination; library rejects limits over 1000. |
| Date history | `get_activities_by_date(start, end, activitytype=None, sortorder=None)` | Internally pages 20 activities at a time until empty. No explicit date-span limit in the client. |
| Activity summary | `get_activity(activity_id)` | Rich summary, metadata, split summaries, and future-link field `metadataDTO.associatedWorkoutId`. |
| Sampled detail | `get_activity_details(activity_id, maxchart, maxpoly)` | Descriptor-indexed time series. `maxchart` and `maxpoly` cap/downsample returned points; `detailsAvailable` can be false. |
| Laps | `get_activity_splits(activity_id)` | `lapDTOs`, event markers, and nested lengths. Available for oldest and recent samples. |
| Sport/interval splits | `get_activity_typed_splits(activity_id)` | Rich recent running splits; oldest strength response had an empty split list. |
| Split aggregates | `get_activity_split_summaries(activity_id)` | Rich recent running aggregates; oldest strength response had an empty summaries list. |
| HR zones | `get_activity_hr_in_timezones(activity_id)` | Five zones with lower boundary and seconds in zone for both samples. |
| Power zones | `get_activity_power_in_timezones(activity_id)` | Five zones for recent run; empty for oldest strength activity. |
| Exercise sets | `get_activity_exercise_sets(activity_id)` | Strength set type, start, duration, reps, weight, exercise category/name, confidence, and workout-step index. Populated for oldest strength activity. |
| Weather | `get_activity_weather(activity_id)` | Exposed by client but not live-audited because it is not core coaching history. |
| Gear | `get_activity_gear(activity_id)` | Exposed by client but not live-audited. Potentially useful for shoe/bike mileage, not core athlete state. |
| Original file | `download_activity(activity_id, ORIGINAL)` | FIT/TCX/GPX/KML/CSV downloads are available. Not needed for normal profile persistence; useful only if source reprocessing requires FIT fields absent from JSON. |

### Account History

| Observation | Result |
|---|---|
| Total activities | 62 |
| Earliest activity | 2026-03-30, strength training |
| Latest activity at audit | 2026-08-06, running |
| Earliest detailed activity | 2026-03-30; details endpoint populated |
| Oldest activity laps/splits | Laps populated, HR zones populated, exercise sets populated; typed splits and split summaries had valid empty lists |
| Recent activity detail | Details, laps, typed splits, split summaries, HR zones, power zones all populated |
| Advanced-field aging | Cannot be assessed across device generations: the entire account history is only about 4.5 months old and the oldest/recent samples are different sports |

### Useful Activity Fields Observed

Activity-level summary and metadata:

- stable `activityId`, activity/sport type, start times/time zone, duration, elapsed duration, moving
  duration, distance, average/max speed, calories, and steps;
- elevation gain/loss, min/max/average elevation, temperature, grade-adjusted speed, and vertical
  speed;
- average/max/min HR and `hrTimeInZone_1` through `hrTimeInZone_5`;
- average/max/normalized power, total work, and `powerTimeInZone_1` through
  `powerTimeInZone_5` when supplied;
- average/max running cadence, stride length, ground-contact time, vertical oscillation, and vertical
  ratio;
- aerobic/anaerobic Training Effect, effect messages/label, moderate/vigorous intensity minutes,
  Body Battery difference, and per-activity VO2max;
- perceived effort/workout feel from detailed summary (`directWorkoutRpe`, `directWorkoutFeel`);
- PR flags, fastest 1K/mile/5K/10K splits, lap count, manual/original flags, sensors, and whether
  chart/polyline/splits/intensity intervals are available;
- `metadataDTO.associatedWorkoutId`, which is important for the future
  planned workout -> Garmin workout -> completed activity link.

Descriptor-indexed sampled detail on the recent run:

- timestamp, moving/timer/elapsed duration, distance, speed, grade-adjusted speed, elevation,
  vertical speed, HR, run/double/fractional cadence, and power;
- stride length, ground-contact time, vertical oscillation, vertical ratio, accumulated power, air
  temperature, Body Battery, and coordinates.

Lap and typed-split detail:

- duration/elapsed/moving time, distance, speed, HR, calories, elevation, cadence, power,
  normalized power, total work, intensity type, temperature, running dynamics, and coordinates;
- split type/count and averages/maxima useful for long-run progression, intervals, and segment
  analysis;
- exercise sets for strength sessions, including reps, weight, set type, duration, exercise, and
  workout-step index.

Raw GPS tracks are available but are not required for the Athlete Profile's expected trend and
coaching use cases. Route storage would add sensitive, high-volume data without a current analytical
requirement.

## API And Historical Limitations

- Garmin Connect is an unofficial, undocumented integration. Endpoint schemas, availability, and
  limits can change without notice; fields vary by device, firmware, sport, subscription, and user
  settings.
- Successful HTTP responses can be structurally non-empty but contain no metric data. Body Battery
  is the clearest example: dates from 2005 returned a dated placeholder with six all-null sample
  rows. Missing detection must inspect values, not `bool(response)`.
- Missing is not zero. Empty/null SpO2, readiness, load, and recovery values must remain absent.
- A 400/404 can mean an unsupported endpoint or invalid request, while an empty object/list is valid
  no-data. Authentication (401/403), rate limiting (429), and transient API/server errors require
  distinct handling.
- Body Battery accepts at most 31 inclusive days in one observed request. This is a request-window
  restriction, not a history boundary.
- `get_daily_steps()` documents a 28-day endpoint limit and silently creates multiple requests for
  longer ranges. Backfill code must chunk explicitly enough to control pacing and progress.
- Race-prediction ranges may not exceed 366 days.
- Activity list pages are capped at 1000 by the client. `get_activities_by_date()` uses 20-item pages
  and can issue many requests for broad active periods.
- `get_activity_details()` is sampled according to `maxchart` and route points according to
  `maxpoly`; it is not guaranteed to be the full original FIT stream.
- Lactate-threshold history calls three endpoints (speed, HR, power), so one wrapper call has a
  higher request cost.
- Per-day fitness endpoints may be sparse or update after the day. VO2max was empty on 2026-08-07
  but populated on 2026-08-06. Incremental sync should overlap recent dates.
- The audit's boundary search assumes a metric is normally continuous after adoption, confirms an
  empty milestone with nearby dates, narrows logarithmically, and scans the final week. Isolated
  recording gaps can still make an "earliest observed" date later than the theoretical first day.
- The account has no older device era. It is therefore impossible to conclude whether Garmin drops
  advanced fields from genuinely old activities; the oldest available activity itself still has
  detail, laps, HR zones, and strength sets.

## Persistence Recommendations For Later Tasks

These are inputs to Task 2, not schema changes made by this task.

Persist daily aggregates useful for trends:

- steps, distance, calories, resting/min/max HR, stress, Body Battery high/low/charged/drained,
  respiration, and moderate/vigorous intensity minutes;
- sleep start/end, total/stage/awake/nap durations, score and useful score components, sleep need,
  overnight HR, sleep stress, and sleep Body Battery change;
- HRV last-night/weekly values, 5-minute high, Garmin status, feedback, and Garmin baseline bounds;
- SpO2, Training Readiness/recovery, Training Status/load, Endurance/Hill Score, and similar metrics
  only when non-null data becomes available.

Persist sparse fitness snapshots rather than copying daily placeholders:

- VO2max, lactate/running power threshold, cycling FTP, race predictions, fitness age, and future
  Training Status/load/readiness values;
- the source method/date and enough completeness state to distinguish not-yet-synced from valid
  no-data and unsupported.

Persist activity summaries keyed by Garmin activity ID:

- sport/type, timestamps, distance, duration variants, speed/pace, calories, elevation, HR, HR-zone
  seconds, cadence, power/power-zone seconds, Training Effect, Exercise Load if it appears, VO2max,
  running dynamics, RPE/feel, intensity minutes, and associated Garmin workout ID;
- laps and typed splits because they provide clear coaching value for pacing, intervals, long-run
  progression, HR drift, and sport-specific analysis;
- strength exercise sets where present;
- no GPS track by default. Existing compressed raw activity JSON can remain a reprocessing source.

Do not blindly persist every response field. Intraday wellness arrays are high volume and are rarely
needed for deterministic coach summaries; daily/nightly aggregates should be the default, with raw
activity detail retained selectively for drill-down and reprocessing.

## Re-running The Audit

From the repository root:

```powershell
uv run python scripts/audit_garmin_history.py
```

Useful controls:

```powershell
uv run python scripts/audit_garmin_history.py --date 2026-08-07 --min-date 2005-01-01 --delay 1 --max-calls 300
```

The default output, `data/garmin-audit.json`, is ignored by Git because it is account-specific
runtime evidence. Review a new report before using its boundaries: a device change, newly enabled
sensor setting, Garmin backfill, or client/API change can alter availability.
