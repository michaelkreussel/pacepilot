# Automatic Athlete Profile Stages

## Architectural Boundary

The automatic athlete profile is a deterministic `AthleteDataService` projection, not a second
database model. It reads the existing sources:

- `DailyFitness` for dated Garmin fitness and performance snapshots;
- `DailyHealth` for recovery and resting-heart-rate evidence;
- `Activity`, `ActivitySplit`, and `ActivityZone` for observed training and performance;
- `GarminSyncState` for coverage and missing-data semantics;
- raw activity detail files only when normalized rows are insufficient.

Only athlete-owned intent remains separately persisted: goals, availability, constraints, and rare
dated lab/test evidence.

## Stage 1: Garmin Snapshots

Import current lactate threshold, running threshold power, cycling FTP, race predictions, personal
records, configured max HR, and HR/power zone configurations into today's existing `DailyFitness`
row. Preserve the daily history rather than overwriting a single profile record. The profile DTO
selects the latest value on or before its `as_of` date.

## Stage 2: Observed Performance

Derive sport-specific, versioned read-time metrics from normalized activities:

- Garmin personal records and exact/near-exact laps for 1K, 5K, 10K, half marathon, and marathon;
- sustainable weekly volume from complete calendar weeks;
- typical sessions and active days per week;
- longest recent road/trail run;
- low/moderate/high intensity distribution with explicit HR coverage.

Unknown coverage must remain unknown instead of becoming zero.

Implemented in `app.services.analytics.automatic_profile` as a read-time DTO with formula version
`observed-profile.v1`:

- best efforts prefer Garmin personal records and otherwise accept only laps/splits within 0.5% of
  the exact target distance;
- weekly capacity uses twelve complete Monday-Sunday weeks and reports the median of the latest six,
  including zero weeks;
- longest road, trail, and treadmill runs are kept separate over the latest 84 days;
- HR intensity requires complete date coverage, at least ten runs, four covered hours, and 70%
  duration coverage before percentages are reported.

Arbitrary rolling GPS segments and HR drift remain deferred because they require sampled-detail or
FIT reprocessing rather than activity-wide averages.

## Stage 3: Empirical Training Ranges (implemented)

Classify qualified road, trail, and treadmill sessions separately. Compute median and interquartile
ranges for easy, tempo, interval, and long-run pace/HR only after metric-specific minimum sample
rules are met. Whole-activity pace must not classify interval work that includes warm-up and
recovery; use typed splits or steady segments.

The read-time profile now reports:

- easy runs from recent low-intensity, low-elevation sessions without interval structure;
- tempo efforts from explicitly classified tempo/threshold splits or steady 15–75-minute
  activities carrying Garmin's `TEMPO`/`LACTATE_THRESHOLD` label; labeled whole activities are
  rejected when interval structure or more than 8% lap-pace variation is present;
- one combined interval range from typed work splits or repeated fast Garmin `RWD_RUN` segments in
  `VO2MAX`/anaerobic sessions; one session with at least four repetitions and eight work minutes is
  shown with low confidence, while repeated sessions can later raise confidence;
- long runs from low-intensity sessions relative to the athlete's typical duration;
- median pace, interquartile pace, median heart rate, session count, effort count, minutes, and
  confidence only after each range's minimum evidence threshold is met.

## Stage 4: Detail And FIT Evidence (implemented)

Descriptor-indexed sampled detail now provides approximate arbitrary-distance best efforts,
heart-rate drift, steady threshold segments, elevation change, and pace stability. Selective
ORIGINAL/FIT ingestion runs inside the existing activity-enrichment budget for eligible runs; FIT
timer events provide pause-aware evidence and native record resolution. If no usable FIT is
available, the analyzer falls back to Garmin detail samples with lower confidence.

Only raw FIT file references and import status are stored in SQLite. All analytical results remain
read-time DTOs and carry a separate `detail-evidence.v1` formula version, source activities, sample
counts, dates, and confidence. Garmin personal records and exact persisted splits retain precedence
over rolling detail segments.

Because native FIT decoding dominates performance-profile latency, completed detail evidence is
kept in a bounded in-process cache. The key includes the formula version, `as_of`, threshold inputs,
activity metadata, split structure, and FIT/detail file path, size, and nanosecond modification time.
Sync updates and file changes therefore invalidate cached evidence automatically. Derived evidence
is still not persisted in SQLite.

## Stage 5: Automatic Performance State (implemented)

`automatic-performance-state.v1` composes the existing evidence into a compact running-specific
summary without creating a score, rank, or artificial athlete level. It reports concrete values for:

- current Garmin thresholds and demonstrated best efforts with source, freshness, and confidence;
- sustainable endurance base, recent habitual load, and observed training tolerance;
- changes over 4, 12, and 26 complete calendar weeks;
- conservative strengths, developmental observations, and separate data gaps;
- duration, distance, strain, split, detail, and history coverage.

Trend comparisons split each horizon into equal earlier and recent halves and exclude the current
partial week. Higher or lower volume describes only the observed direction, not whether the change
is beneficial. Training tolerance likewise compares the latest four complete weeks with the prior
eight-week interquartile range; it is evidence for later planning rules, not an injury-risk claim.

## Stage 6: Deterministic Planning Limits (implemented)

`running-planning-limits.v1` turns the observed running state into conservative, read-only planning
guardrails. It provides a maximum automatic weekly distance, history-dependent progression,
hard-session count and spacing, long-run distance and duration, empirical or threshold-based target
ranges, four-week deloads, goal-distance taper defaults, and temporary recovery adjustments.

The rules preserve unknowns instead of inventing beginner defaults. Weekly volume requires at least
twelve covered complete weeks and 80% distance coverage. Progression is limited to 5% and is enabled
only with high-quality data, stable recent volume, and training inside the habitual range. No more
than two hard sessions are allowed, with at least three calendar days between them. With at least
three usual runs per week, long runs are limited to 35% of the weekly cap. At lower frequency that
share rule is not meaningful, so the entire weekly cap may be used while the run remains limited to
105% of the observed stratum-specific longest run. A missing empirical long-run pace may inherit the
same-stratum empirical Easy Run range as a low-confidence Zone-2 fallback. Garmin `AEROBIC_BASE`
activities count as low-intensity evidence when RPE and anaerobic Training Effect do not contradict
that label; the relative long-run threshold is bounded between 45 and 60 minutes.

Current recovery can only reduce limits; it can never raise them. Free-text athlete constraints are
not medically interpreted and instead require explicit review. The limits remain advisory at this
stage and do not alter manual workout validation or the `draft -> confirmed -> published -> pushed`
boundary.

## Stage 7: Read-Only Agent Integration (implemented)

The coach tool `get_athlete_planning_context` exposes the existing `planning-context.v2` together
with the current recovery state in a versioned `athlete-planning-context.v1` envelope. The payload
contains athlete intent, availability, manual and Garmin performance anchors, empirical training
ranges, habitual load, data quality, and all deterministic Stage 6 limits. It is bound to the
authenticated runtime user and the request's fixed `as_of` date; the model cannot supply either
value as a tool argument.

The tool explicitly disables detail evidence processing. Raw activity payloads, local file paths,
FIT records, and GPS data are neither read for the tool nor serialized into its output. It uses a
short-lived read-only session and does not persist tool results or create, update, confirm, publish,
or push workouts. The coach prompt requires the model to use the supplied ranges and limits without
inventing or relaxing them. Narrower tools remain preferred for isolated health questions to keep
provider-bound data proportional to the question.
