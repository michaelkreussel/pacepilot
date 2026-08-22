# AI Coach Phase 5 Gate Matrix

Phase 5 introduces a deterministic running baseline and intensity model in shadow mode. It does
not generate workouts, call Garmin or an LLM, expose a coach tool, or persist baseline snapshots.

| Gate | Test or check | Fixture | Expected result |
|---|---|---|---|
| Shared activity semantics | `tests/test_running_baseline.py::test_activity_classification_is_shared_and_excludes_mixed_sports` and existing trend tests | Running variants, cycling, and `bike_run` | Trends and baseline use the same classifier; mixed sports are not counted as running |
| Exact windows | `test_running_baseline_uses_exact_windows_and_running_only_metrics` | Boundary runs plus other sports | Inclusive 7, 28, 56, and 180 day windows contain only running-family activities |
| Robust statistics and sRPE | `test_running_baseline_uses_exact_windows_and_running_only_metrics` | Runs with duration, distance, and RPE | Median/MAD, frequency, volume, long run, hard days, quality density, and RPE x duration are reproducible |
| Interruption and re-entry | `test_running_baseline_uses_median_mad_and_reproduces_reentry_and_spike` and `test_current_running_interruption_is_reported_without_claiming_reentry` | Completed and current running gaps | Gaps are reported directly; only a completed gap of at least 14 days enters the 14 day re-entry observation period |
| Distance spike | `test_distance_spike_uses_reference_runs_before_180_day_window` | Latest run plus prior 30 day reference outside the baseline window | A run above 110% of the prior 30 day maximum is reproducible without changing the exact baseline window |
| Partial week and typical long run | `test_partial_180_day_week_and_typical_weekly_long_run_are_included` | Four full weeks plus the partial 180 day edge | All 180 days contribute; weekly longest-run median/MAD is robust to one outlier |
| Data quality | `test_invalid_run_measurements_reduce_coverage_without_breaking_fingerprint` | Negative and non-finite duration/distance plus invalid RPE | Invalid values do not enter statistics or hard classification and reduce explicit metric coverage |
| Complete history without a run today | Baseline tests use complete sync state whose newest activity predates `as_of` | Fully backfilled sparse activity dates | Backfill completeness remains 100%; latest-run age and sync age remain separate fields |
| Sparse data and missing HRV | `test_sparse_data_and_wearable_predictions_do_not_create_pace_anchor` | One run, no HRV, fresh Garmin prediction and VO2max | No pace anchor or Critical Speed is produced; wearable values remain secondary context |
| Threshold freshness | `test_fresh_threshold_is_a_pace_anchor_for_an_adequate_baseline` and `test_stale_threshold_falls_back_to_rpe_and_talk_test` | Adequate baseline with 10 or 57 day old threshold | A threshold up to 56 days old can anchor pace; a stale threshold falls back to RPE/Talk Test |
| Source priority | `test_manual_race_anchors_beat_threshold_and_enable_supported_critical_speed` | Reliable race anchors plus fresh Garmin threshold | Reliable performance anchors win; two consistent distances can support Critical Speed |
| Anchor rejection | `test_unreliable_and_stale_performance_anchors_fall_back_to_threshold` | Unreliable and 181 day old anchors | Rejected anchors produce warnings and the next eligible source is selected |
| Secondary wearable context | `test_sparse_data_and_wearable_predictions_do_not_create_pace_anchor` | Garmin race prediction, VO2max, and endurance score | Wearable estimates never become a primary pace source |
| Reproducible context | `test_shadow_fingerprint_is_stable_and_changes_with_material_input` | Repeated reads followed by a material activity change | Equal inputs produce equal fingerprints; changed inputs change them |
| JSON-safe revision context | `test_fresh_threshold_is_a_pace_anchor_for_an_adequate_baseline` | Shadow generation context | Dates are normalized and the compact context is valid JSON for `generation_context_json` |

## Versioned Policies

- Baseline version: `1.0`; sport classification version: `1.0`; hard-activity rule version: `1.0`.
- Exact calendar windows are inclusive of `as_of` and use 7, 28, 56, and 180 days.
- A hard run has aerobic Training Effect at least 3.5, anaerobic Training Effect at least 2.5,
  or valid RPE at least 7. Training Effect must be finite and between 0 and 5; RPE must be 1-10.
- sRPE is valid workout RPE multiplied by canonical activity duration in minutes. Missing or invalid
  RPE/duration remains unavailable rather than zero.
- An interruption contains at least seven full non-running days. Re-entry is observed for 14 days
  after returning from at least 14 full non-running days.
- A distance spike is strictly greater than 110% of the longest measured run in the preceding 30
  calendar days.
- Reliable manual, race, or time-trial anchors remain eligible for 180 days. A Garmin lactate
  threshold remains eligible for 56 days. Garmin predictions, VO2max, and wearable scores remain
  secondary context only.
- Precise pace or Critical Speed requires at least medium confidence in the 56 day running baseline.
  Critical Speed additionally requires two consistent, reliable performance anchors.
- `PerformanceAnchorInput` is an ephemeral typed boundary. Persistent athlete anchors remain part
  of the later athlete-profile persistence phase; Phase 5 creates no database table.
