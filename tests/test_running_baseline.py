import json
from datetime import date, datetime, timedelta

from app.models import Activity, DailyFitness, GarminSyncState, User
from app.services.analytics import AthleteDataService
from app.services.analytics.activity_semantics import is_running_sport, sport_family
from app.services.analytics.running_intensity import PerformanceAnchorInput


def _user(session, name: str = "Runner") -> User:
    user = User(display_name=name)
    session.add(user)
    session.flush()
    return user


def _run(user_id: int, key: str, day: date, **values) -> Activity:
    return Activity(
        user_id=user_id,
        garmin_activity_id=key,
        name=key,
        activity_type=values.pop("activity_type", "running"),
        started_at=datetime.combine(day, datetime.min.time()),
        **values,
    )


def _complete_activity_history(user_id: int, as_of: date) -> GarminSyncState:
    return GarminSyncState(
        user_id=user_id,
        resource="activities",
        status="ok",
        backfill_complete=True,
        oldest_synced_date=as_of - timedelta(days=365),
        newest_synced_date=as_of - timedelta(days=2),
    )


def _adequate_baseline(session, user_id: int, as_of: date) -> None:
    for index, age in enumerate((0, 7, 14, 21, 28, 35)):
        session.add(
            _run(
                user_id,
                f"run-{index}",
                as_of - timedelta(days=age),
                duration_s=2_400 + index * 60,
                distance_m=6_000 + index * 100,
                aerobic_training_effect=2.0,
                workout_rpe=4,
            )
        )
    session.add(_complete_activity_history(user_id, as_of))


def test_activity_classification_is_shared_and_excludes_mixed_sports():
    assert sport_family("trail_running") == "running"
    assert sport_family("trail_run") == "running"
    assert is_running_sport("TREADMILL_RUNNING") is True
    assert is_running_sport("bike_run") is False
    assert sport_family("road_bike") == "cycling"


def test_running_baseline_uses_exact_windows_and_running_only_metrics(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _run(
                    user.id,
                    "seven-start",
                    as_of - timedelta(days=6),
                    activity_type="trail_running",
                    duration_s=1_800,
                    distance_m=5_000,
                    aerobic_training_effect=2,
                    workout_rpe=3,
                ),
                _run(
                    user.id,
                    "today-hard",
                    as_of,
                    duration_s=3_600,
                    distance_m=10_000,
                    aerobic_training_effect=4,
                    workout_rpe=8,
                ),
                _run(
                    user.id,
                    "outside-seven",
                    as_of - timedelta(days=7),
                    duration_s=1_200,
                    distance_m=3_000,
                    aerobic_training_effect=1,
                    workout_rpe=2,
                ),
                _run(
                    user.id,
                    "outside-180",
                    as_of - timedelta(days=180),
                    duration_s=99_000,
                    distance_m=99_000,
                ),
                _run(
                    user.id,
                    "ride",
                    as_of,
                    activity_type="cycling",
                    duration_s=7_200,
                    distance_m=50_000,
                    aerobic_training_effect=5,
                    workout_rpe=10,
                ),
                _run(
                    user.id,
                    "mixed",
                    as_of,
                    activity_type="bike_run",
                    duration_s=7_200,
                    distance_m=30_000,
                    aerobic_training_effect=5,
                ),
                _complete_activity_history(user.id, as_of),
            ]
        )
        session.commit()

        baseline = AthleteDataService(session, user.id, as_of=as_of).get_running_baseline()
        seven = baseline.window(7)

        assert [window.days for window in baseline.windows] == [7, 28, 56, 180]
        assert seven.start == date(2026, 6, 24)
        assert seven.end == as_of
        assert seven.runs == 2
        assert baseline.window(28).runs == 3
        assert baseline.window(180).runs == 3
        assert seven.active_days == 2
        assert seven.frequency_per_week == 2
        assert seven.total_duration_s == 5_400
        assert seven.total_distance_m == 15_000
        assert seven.per_run_duration_s.median == 2_700
        assert seven.per_run_duration_s.median_absolute_deviation == 900
        assert seven.weekly_runs.median == 2
        assert seven.weekly_distance_m.median == 15_000
        assert seven.longest_distance.value == 10_000
        assert seven.hard_runs == 1
        assert seven.hard_days == 1
        assert seven.quality_density_percent == 50
        assert seven.total_srpe == 570
        assert seven.quality.duration.percent == 100
        assert seven.quality.distance.percent == 100
        assert seven.quality.rpe.percent == 100
        assert seven.quality.srpe.percent == 100
        assert seven.quality.confidence == "low"
        assert seven.quality.history_coverage_percent == 100


def test_running_baseline_uses_median_mad_and_reproduces_reentry_and_spike(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _run(
                    user.id,
                    "before-break",
                    as_of - timedelta(days=25),
                    duration_s=2_000,
                    distance_m=5_000,
                    aerobic_training_effect=2,
                ),
                _run(
                    user.id,
                    "return",
                    as_of - timedelta(days=4),
                    duration_s=2_100,
                    distance_m=5_200,
                    aerobic_training_effect=2,
                ),
                _run(
                    user.id,
                    "spike",
                    as_of - timedelta(days=2),
                    duration_s=8_000,
                    distance_m=50_000,
                    aerobic_training_effect=4,
                ),
                _complete_activity_history(user.id, as_of),
            ]
        )
        session.commit()

        baseline = AthleteDataService(session, user.id, as_of=as_of).get_running_baseline()
        window = baseline.window(28)

        assert window.per_run_distance_m.median == 5_200
        assert window.per_run_distance_m.median_absolute_deviation == 200
        assert baseline.interruptions[0].inactive_days == 20
        assert baseline.interruptions[0].resumed_on == as_of - timedelta(days=4)
        assert baseline.reentry.active is True
        assert baseline.reentry.preceding_inactive_days == 20
        assert baseline.latest_distance_spike is not None
        assert baseline.latest_distance_spike.prior_30d_longest_distance_m == 5_200
        assert baseline.latest_distance_spike.ratio == 9.615
        assert baseline.latest_distance_spike.exceeds_110_percent is True


def test_current_running_interruption_is_reported_without_claiming_reentry(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _run(user.id, "old-run", as_of - timedelta(days=14), distance_m=5_000),
                _complete_activity_history(user.id, as_of),
            ]
        )
        session.commit()

        baseline = AthleteDataService(session, user.id, as_of=as_of).get_running_baseline()

        assert baseline.interruptions[-1].current is True
        assert baseline.interruptions[-1].inactive_days == 14
        assert baseline.reentry.active is False


def test_sparse_data_and_wearable_predictions_do_not_create_pace_anchor(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        other = _user(session, "Other")
        session.add_all(
            [
                _run(
                    user.id,
                    "only-run",
                    as_of - timedelta(days=2),
                    duration_s=1_800,
                    distance_m=5_000,
                ),
                _complete_activity_history(user.id, as_of),
                DailyFitness(
                    user_id=user.id,
                    day=as_of,
                    race_prediction_5k_seconds=1_200,
                    vo2max=55,
                    endurance_score=7_000,
                ),
                DailyFitness(
                    user_id=other.id,
                    day=as_of,
                    lactate_threshold_speed_mps=5,
                ),
            ]
        )
        session.commit()

        shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis()

        assert shadow.baseline.window(56).quality.confidence == "insufficient"
        assert shadow.intensity.mode == "clarify"
        assert shadow.intensity.pace_anchor is None
        assert shadow.intensity.primary_source == "athlete_clarification"
        assert {item.key for item in shadow.intensity.secondary_context} == {
            "race_5k",
            "vo2max",
            "endurance_score",
        }
        assert all(
            item.role == "secondary_context_only" for item in shadow.intensity.secondary_context
        )
        assert shadow.intensity.critical_speed.available is False
        assert shadow.intensity.critical_speed.speed_mps is None


def test_fresh_threshold_is_a_pace_anchor_for_an_adequate_baseline(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        _adequate_baseline(session, user.id, as_of)
        session.add(
            DailyFitness(
                user_id=user.id,
                day=as_of - timedelta(days=10),
                lactate_threshold_speed_mps=4,
            )
        )
        session.commit()

        shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis()

        assert shadow.baseline.window(56).quality.confidence == "medium"
        assert shadow.intensity.mode == "pace_anchor"
        assert shadow.intensity.primary_source == "garmin_lactate_threshold"
        assert shadow.intensity.pace_anchor is not None
        assert shadow.intensity.pace_anchor.age_days == 10
        assert shadow.intensity.pace_anchor.pace_seconds_per_km == 250
        assert shadow.generation_context["schema_version"] == "running_generation_context.v1"
        assert shadow.generation_context["as_of"] == "2026-06-30"
        assert shadow.generation_context["intensity"]["mode"] == "pace_anchor"
        json.dumps(shadow.generation_context, allow_nan=False)


def test_stale_threshold_falls_back_to_rpe_and_talk_test(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        _adequate_baseline(session, user.id, as_of)
        session.add(
            DailyFitness(
                user_id=user.id,
                day=as_of - timedelta(days=57),
                lactate_threshold_speed_mps=4,
            )
        )
        session.commit()

        intensity = (
            AthleteDataService(session, user.id, as_of=as_of)
            .get_running_shadow_analysis()
            .intensity
        )

        assert intensity.mode == "rpe_talk_test"
        assert intensity.pace_anchor is None
        assert intensity.primary_source == "rpe_talk_test"
        assert "lactate_threshold_older_than_56_days" in intensity.warnings
        assert [band.key for band in intensity.rpe_talk_test_bands] == [
            "easy",
            "steady",
            "threshold",
            "hard",
        ]


def test_shadow_fingerprint_is_stable_and_changes_with_material_input(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        _adequate_baseline(session, user.id, as_of)
        session.commit()
        service = AthleteDataService(session, user.id, as_of=as_of)

        first = service.get_running_shadow_analysis()
        second = service.get_running_shadow_analysis()
        assert first.context_fingerprint == second.context_fingerprint
        assert first.baseline.input_fingerprint == second.baseline.input_fingerprint

        activity = session.query(Activity).filter_by(garmin_activity_id="run-0").one()
        activity.distance_m = 7_000
        session.commit()
        changed = service.get_running_shadow_analysis()

        assert changed.baseline.input_fingerprint != first.baseline.input_fingerprint
        assert changed.context_fingerprint != first.context_fingerprint


def test_manual_race_anchors_beat_threshold_and_enable_supported_critical_speed(
    session_factory,
):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        _adequate_baseline(session, user.id, as_of)
        session.add(
            DailyFitness(
                user_id=user.id,
                day=as_of,
                lactate_threshold_speed_mps=4.5,
            )
        )
        session.commit()
        anchors = (
            PerformanceAnchorInput(
                kind="race",
                achieved_on=as_of - timedelta(days=5),
                distance_m=5_000,
                duration_s=1_200,
            ),
            PerformanceAnchorInput(
                kind="race",
                achieved_on=as_of - timedelta(days=20),
                distance_m=10_000,
                duration_s=2_700,
            ),
        )

        intensity = (
            AthleteDataService(session, user.id, as_of=as_of)
            .get_running_shadow_analysis(performance_anchors=anchors)
            .intensity
        )

        assert intensity.primary_source == "race_performance"
        assert intensity.pace_anchor is not None
        assert intensity.pace_anchor.reference_distance_m == 5_000
        assert intensity.pace_anchor.pace_seconds_per_km == 240
        assert intensity.critical_speed.available is True
        assert intensity.critical_speed.speed_mps == 3.333
        assert intensity.critical_speed.d_prime_m == 1_000


def test_unreliable_and_stale_performance_anchors_fall_back_to_threshold(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        _adequate_baseline(session, user.id, as_of)
        session.add(DailyFitness(user_id=user.id, day=as_of, lactate_threshold_speed_mps=4))
        session.commit()
        anchors = (
            PerformanceAnchorInput(
                kind="race",
                achieved_on=as_of - timedelta(days=2),
                distance_m=5_000,
                duration_s=1_200,
                reliable=False,
            ),
            PerformanceAnchorInput(
                kind="manual",
                achieved_on=as_of - timedelta(days=181),
                distance_m=10_000,
                duration_s=2_700,
            ),
        )

        intensity = (
            AthleteDataService(session, user.id, as_of=as_of)
            .get_running_shadow_analysis(performance_anchors=anchors)
            .intensity
        )

        assert intensity.primary_source == "garmin_lactate_threshold"
        assert "unreliable_performance_anchor_ignored" in intensity.warnings
        assert "performance_anchor_outside_180_day_window" in intensity.warnings


def test_partial_180_day_week_and_typical_weekly_long_run_are_included(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _run(
                    user.id,
                    f"week-{index}",
                    as_of - timedelta(days=age),
                    duration_s=distance / 2,
                    distance_m=distance,
                    aerobic_training_effect=2,
                )
                for index, (age, distance) in enumerate(
                    ((4, 50_000), (11, 7_000), (18, 6_000), (25, 5_000), (179, 4_000))
                )
            ]
            + [_complete_activity_history(user.id, as_of)]
        )
        session.commit()

        baseline = AthleteDataService(session, user.id, as_of=as_of).get_running_baseline()
        twenty_eight = baseline.window(28)
        one_eighty = baseline.window(180)

        assert twenty_eight.weekly_longest_distance_m.median == 6_500
        assert twenty_eight.weekly_longest_distance_m.median_absolute_deviation == 1_000
        assert one_eighty.runs == 5
        assert one_eighty.weekly_runs.sample_count == 26
        assert one_eighty.weekly_longest_distance_m.sample_count == 26


def test_distance_spike_uses_reference_runs_before_180_day_window(session_factory):
    as_of = date(2026, 6, 30)
    start = as_of - timedelta(days=179)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _run(user.id, "reference", start - timedelta(days=10), distance_m=5_000),
                _run(user.id, "boundary-spike", start, distance_m=10_000),
                _complete_activity_history(user.id, as_of),
            ]
        )
        session.commit()

        spike = (
            AthleteDataService(session, user.id, as_of=as_of)
            .get_running_baseline()
            .latest_distance_spike
        )

        assert spike is not None
        assert spike.prior_30d_longest_distance_m == 5_000
        assert spike.ratio == 2


def test_invalid_run_measurements_reduce_coverage_without_breaking_fingerprint(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _run(
                    user.id,
                    "valid",
                    as_of,
                    duration_s=1_800,
                    distance_m=5_000,
                    workout_rpe=4,
                ),
                _run(
                    user.id,
                    "negative",
                    as_of - timedelta(days=1),
                    duration_s=-1,
                    distance_m=-10,
                    workout_rpe=11,
                ),
                _run(
                    user.id,
                    "non-finite",
                    as_of - timedelta(days=2),
                    duration_s=float("inf"),
                    distance_m=float("inf"),
                    workout_rpe=0,
                ),
                _complete_activity_history(user.id, as_of),
            ]
        )
        session.commit()

        baseline = AthleteDataService(session, user.id, as_of=as_of).get_running_baseline()
        window = baseline.window(7)

        assert window.total_duration_s == 1_800
        assert window.total_distance_m == 5_000
        assert window.quality.duration.available == 1
        assert window.quality.distance.available == 1
        assert window.quality.invalid_duration_values == 2
        assert window.quality.invalid_distance_values == 2
        assert window.quality.invalid_rpe_values == 2
        assert window.hard_runs == 0
        assert len(baseline.input_fingerprint) == 64
