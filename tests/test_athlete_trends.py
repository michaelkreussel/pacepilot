from datetime import date, datetime

import pytest

from app.models import (
    Activity,
    ActivityExerciseSet,
    ActivitySplit,
    ActivityZone,
    DailyFitness,
    DailyHealth,
    GarminSyncState,
    User,
)
from app.services.analytics import AthleteDataService
from app.services.analytics.training_trends import get_training_summary


def _user(session, name: str = "Trends") -> User:
    user = User(display_name=name)
    session.add(user)
    session.flush()
    return user


def _activity(
    user_id: int,
    activity_id: str,
    sport: str,
    started_at: datetime,
    **values,
) -> Activity:
    return Activity(
        user_id=user_id,
        garmin_activity_id=activity_id,
        name=activity_id,
        activity_type=sport,
        started_at=started_at,
        **values,
    )


def test_health_trends_use_calendar_windows_and_prior_baseline(session_factory):
    as_of = date(2026, 6, 30)
    with session_factory() as session:
        user = _user(session)
        other = _user(session, "Other")
        session.add_all(
            [
                DailyHealth(user_id=user.id, day=date(2026, 6, 22), resting_hr=50, hrv_average=60),
                DailyHealth(user_id=user.id, day=date(2026, 6, 23), resting_hr=52, hrv_average=62),
                DailyHealth(user_id=user.id, day=date(2026, 6, 24), resting_hr=54, hrv_average=64),
                DailyHealth(user_id=user.id, day=date(2026, 6, 25), resting_hr=56, hrv_average=66),
                DailyHealth(user_id=user.id, day=date(2026, 6, 29), resting_hr=None),
                DailyHealth(
                    user_id=user.id,
                    day=as_of,
                    resting_hr=58,
                    hrv_average=68,
                    hrv_status="BALANCED",
                    hrv_baseline_low=55,
                    hrv_baseline_balanced_low=58,
                    hrv_baseline_balanced_high=72,
                    sleep_seconds=27_000,
                    sleep_need_seconds=28_800,
                    sleep_score=82,
                    stress_average=31,
                    body_battery_high=79,
                    body_battery_low=22,
                ),
                DailyHealth(user_id=other.id, day=as_of, resting_hr=200),
                DailyFitness(
                    user_id=user.id,
                    day=date(2026, 6, 29),
                    vo2max=51.2,
                    garmin_training_readiness_score=74,
                    garmin_training_readiness_level="HIGH",
                    recovery_time_minutes=600,
                    training_status="PRODUCTIVE",
                    acute_load=310,
                ),
                DailyFitness(
                    user_id=user.id,
                    day=as_of,
                    vo2max=52.4,
                    garmin_training_readiness_level="LOW",
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="hrv",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=date(2026, 4, 1),
                    newest_synced_date=as_of,
                ),
            ]
        )
        session.commit()

        service = AthleteDataService(session, user.id, as_of=as_of)
        trends = service.get_health_trends(days=28)

        assert trends.start == date(2026, 6, 3)
        assert trends.resting_hr.current == 58
        assert trends.resting_hr.current_day == as_of
        assert trends.resting_hr.average_7d == 56
        assert trends.resting_hr.average_28d == 54
        assert trends.resting_hr.personal_baseline == 51
        assert trends.resting_hr.difference_from_baseline == 7
        assert trends.resting_hr.sample_count == 5
        assert trends.resting_hr.baseline_sample_count == 2
        assert [point.day for point in trends.resting_hr.points] == [
            date(2026, 6, 22),
            date(2026, 6, 23),
            date(2026, 6, 24),
            date(2026, 6, 25),
            date(2026, 6, 30),
        ]
        assert trends.garmin_training_readiness.current == 74
        assert trends.vo2max.current == 52.4
        assert trends.sleep_need.current == 28_800

        recovery = service.get_current_recovery_state()
        assert recovery.health_day == as_of
        assert recovery.fitness_day == as_of
        assert recovery.sleep_seconds == 27_000
        assert recovery.garmin_training_readiness_score == 74
        assert recovery.garmin_training_readiness_level == "LOW"
        assert recovery.garmin_training_readiness_day == date(2026, 6, 29)
        assert recovery.recovery_time_day == date(2026, 6, 29)
        assert recovery.vo2max == 52.4
        assert recovery.vo2max_day == as_of
        assert recovery.training_status == "PRODUCTIVE"
        assert recovery.training_status_day == date(2026, 6, 29)
        assert recovery.pacepilot_readiness_score == 77.1
        assert recovery.pacepilot_readiness_label == "good"
        assert recovery.pacepilot_readiness_confidence == 71.4
        assert {item.component for item in recovery.pacepilot_readiness_components} == {
            "sleep_duration",
            "garmin_sleep_score",
            "hrv",
            "resting_hr",
            "garmin_stress",
            "garmin_body_battery_high",
        }
        assert sum(
            item.normalized_weight for item in recovery.pacepilot_readiness_components
        ) == pytest.approx(1)

        hrv = service.get_hrv_baseline()
        assert hrv.trend.personal_baseline == 61
        assert hrv.garmin_status == "BALANCED"
        assert hrv.garmin_balanced_low == 58
        assert hrv.garmin_balanced_high == 72
        hrv_coverage = {item.resource: item for item in trends.coverage}["hrv"]
        assert hrv_coverage.status == "ok"
        assert hrv_coverage.backfill_complete is True
        assert hrv_coverage.oldest_synced_date == date(2026, 4, 1)


def test_training_summary_respects_boundaries_and_sport_specific_metrics(session_factory):
    as_of = date(2026, 6, 28)
    with session_factory() as session:
        user = _user(session)
        other = _user(session, "Other")
        run = _activity(
            user.id,
            "run",
            "trail_running",
            datetime(2026, 6, 22),
            duration_s=3_600,
            distance_m=10_000,
            elevation_gain_m=200,
            exercise_load=100,
            aerobic_training_effect=4.0,
            anaerobic_training_effect=1.0,
            workout_rpe=8,
            moderate_intensity_minutes=10,
            vigorous_intensity_minutes=20,
        )
        run.zones = [ActivityZone(zone_type="heart_rate", zone_number=2, seconds=1_200)]
        ride = _activity(
            user.id,
            "ride",
            "cycling",
            datetime(2026, 6, 28, 23, 59),
            duration_s=2_000,
            distance_m=20_000,
            elevation_gain_m=None,
            exercise_load=None,
            aerobic_training_effect=2.0,
            anaerobic_training_effect=None,
            moderate_intensity_minutes=None,
            vigorous_intensity_minutes=5,
        )
        ride.zones = [ActivityZone(zone_type="power", zone_number=3, seconds=500)]
        session.add_all(
            [
                run,
                ride,
                _activity(
                    user.id,
                    "before",
                    "running",
                    datetime(2026, 6, 21, 23, 59, 59),
                    distance_m=99_000,
                ),
                _activity(
                    user.id,
                    "after",
                    "running",
                    datetime(2026, 6, 29),
                    distance_m=99_000,
                ),
                _activity(
                    other.id,
                    "other",
                    "running",
                    datetime(2026, 6, 25),
                    distance_m=99_000,
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=date(2026, 3, 1),
                    newest_synced_date=as_of,
                ),
            ]
        )
        session.commit()

        summary = get_training_summary(session, user.id, days=7, as_of=as_of)

        assert summary.start == date(2026, 6, 22)
        assert summary.end == as_of
        assert summary.workouts == 2
        assert summary.active_days == 2
        assert summary.training_frequency_per_week == 2
        assert summary.total_duration_s == 5_600
        assert summary.running_distance_m == 10_000
        assert summary.cycling_distance_m == 20_000
        assert summary.total_elevation_gain_m == 200
        assert summary.exercise_load == 100
        assert summary.average_aerobic_training_effect == 3
        assert summary.average_anaerobic_training_effect == 1
        assert summary.moderate_intensity_minutes == 10
        assert summary.vigorous_intensity_minutes == 25
        assert summary.hard_workouts == 1
        assert summary.data_status == "ok"
        assert summary.history_complete is True
        assert summary.oldest_synced_date == date(2026, 3, 1)
        assert [(item.sport, item.distance_m) for item in summary.volume_per_sport] == [
            ("cycling", 20_000),
            ("trail_running", 10_000),
        ]
        assert [
            (item.sport, item.zone_type, item.zone_number, item.seconds)
            for item in summary.zone_distribution
        ] == [
            ("cycling", "power", 3, 500),
            ("trail_running", "heart_rate", 2, 1_200),
        ]


def test_weekly_trends_include_zero_weeks_and_rolling_volume(session_factory):
    as_of = date(2026, 6, 28)
    with session_factory() as session:
        user = _user(session)
        session.add_all(
            [
                _activity(
                    user.id,
                    "prior",
                    "running",
                    datetime(2026, 6, 14),
                    duration_s=1_000,
                    distance_m=3_000,
                ),
                _activity(
                    user.id,
                    "week-one-a",
                    "running",
                    datetime(2026, 6, 15),
                    duration_s=2_000,
                    distance_m=5_000,
                ),
                _activity(
                    user.id,
                    "week-one-b",
                    "trail_running",
                    datetime(2026, 6, 21),
                    duration_s=3_000,
                    distance_m=7_000,
                ),
                _activity(
                    user.id,
                    "week-two",
                    "running",
                    datetime(2026, 6, 22),
                    duration_s=4_000,
                    distance_m=10_000,
                    exercise_load=90,
                    aerobic_training_effect=4,
                ),
            ]
        )
        session.commit()

        points = AthleteDataService(session, user.id, as_of=as_of).get_training_load_trend(2)

        assert [point.week_start for point in points] == [date(2026, 6, 15), date(2026, 6, 22)]
        assert points[0].week_end == date(2026, 6, 21)
        assert points[0].running_distance_m == 12_000
        assert points[0].longest_run_distance_m == 7_000
        assert points[0].rolling_28d_running_distance_m == 15_000
        assert points[1].running_distance_m == 10_000
        assert points[1].exercise_load == 90
        assert points[1].hard_workouts == 1
        assert points[1].rolling_28d_running_distance_m == 25_000

        timeline = AthleteDataService(session, user.id, as_of=as_of).get_training_timeline(
            7, bucket_days=1
        )
        assert len(timeline) == 7
        assert timeline[0].start == date(2026, 6, 22)
        assert timeline[0].running_distance_m == 10_000
        assert timeline[-1].end == as_of


def test_activity_drill_down_is_normalized_and_user_scoped(session_factory):
    with session_factory() as session:
        user = _user(session)
        other = _user(session, "Other")
        activity = _activity(
            user.id,
            "details",
            "strength_training",
            datetime(2026, 6, 28),
            duration_s=1_800,
            calories=220,
            workout_feel=75,
            details_complete=True,
            splits_complete=True,
        )
        activity.zones = [
            ActivityZone(zone_type="heart_rate", zone_number=3, low_boundary=140, seconds=600)
        ]
        activity.splits = [
            ActivitySplit(
                split_type="lap",
                position=0,
                intensity_type="ACTIVE",
                duration_s=1_800,
                distance_m=None,
            )
        ]
        activity.exercise_sets = [
            ActivityExerciseSet(
                position=0,
                set_type="ACTIVE",
                repetitions=8,
                weight_kg=50,
                exercise_category="BENCH_PRESS",
                exercise_name="BARBELL_BENCH_PRESS",
            )
        ]
        hidden = _activity(
            other.id,
            "hidden",
            "running",
            datetime(2026, 6, 29),
        )
        future = _activity(
            user.id,
            "future",
            "running",
            datetime(2026, 7, 1),
        )
        session.add_all([activity, hidden, future])
        session.commit()

        service = AthleteDataService(session, user.id, as_of=date(2026, 6, 30))
        details = service.get_activity_details(activity.id)

        assert details is not None
        assert details.workout.activity_id == activity.id
        assert details.calories == 220
        assert details.details_complete is True
        assert details.splits_complete is True
        assert details.zones[0].seconds == 600
        assert details.splits[0].intensity_type == "ACTIVE"
        assert details.exercise_sets[0].exercise_name == "BARBELL_BENCH_PRESS"
        assert service.get_activity_details(hidden.id) is None
        assert service.get_activity_details(future.id) is None
        assert [item.activity_id for item in service.get_recent_workouts()] == [activity.id]


def test_missing_duration_and_mixed_sport_distance_are_not_reported_as_zero_or_running(
    session_factory,
):
    with session_factory() as session:
        user = _user(session)
        session.add(
            _activity(
                user.id,
                "mixed",
                "bike_run",
                datetime(2026, 6, 30),
                duration_s=None,
                distance_m=12_000,
            )
        )
        session.commit()

        summary = AthleteDataService(
            session, user.id, as_of=date(2026, 6, 30)
        ).get_training_summary(7)

        assert summary.total_duration_s is None
        assert summary.volume_per_sport[0].duration_s is None
        assert summary.running_distance_m == 0
        assert summary.cycling_distance_m == 0
        assert summary.volume_per_sport[0].distance_m == 12_000


def test_readiness_uses_garmin_hrv_baseline_when_personal_history_is_missing(session_factory):
    with session_factory() as session:
        user = _user(session)
        session.add(
            DailyHealth(
                user_id=user.id,
                day=date(2026, 6, 30),
                hrv_average=60,
                hrv_baseline_balanced_low=50,
                hrv_baseline_balanced_high=70,
            )
        )
        session.add_all(
            [
                _activity(
                    user.id,
                    "easy-measured",
                    "running",
                    datetime(2026, 6, 28),
                    aerobic_training_effect=1,
                ),
                _activity(
                    user.id,
                    "unknown-strain",
                    "strength_training",
                    datetime(2026, 6, 29),
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                ),
            ]
        )
        session.commit()

        recovery = AthleteDataService(
            session, user.id, as_of=date(2026, 6, 30)
        ).get_current_recovery_state()

        assert recovery.pacepilot_readiness_score == 75
        assert recovery.pacepilot_readiness_confidence == 25
        assert recovery.pacepilot_readiness_components[0].component == "hrv"
        assert recovery.pacepilot_readiness_components[0].baseline == 60


def test_readiness_includes_recovery_after_an_inactive_week(session_factory):
    with session_factory() as session:
        user = _user(session)
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=True,
            )
        )
        session.commit()

        recovery = AthleteDataService(
            session, user.id, as_of=date(2026, 6, 30)
        ).get_current_recovery_state()

        assert recovery.pacepilot_readiness_score == 95
        assert len(recovery.pacepilot_readiness_components) == 1
        assert (
            recovery.pacepilot_readiness_components[0].component == "recent_hard_training_recovery"
        )


def test_empty_summaries_preserve_unavailable_metrics(session_factory):
    with session_factory() as session:
        user = _user(session)
        service = AthleteDataService(session, user.id, as_of=date(2026, 6, 30))

        health = service.get_health_trends()
        training = service.get_training_summary()
        recovery = service.get_current_recovery_state()

        assert health.hrv.current is None
        assert health.hrv.average_7d is None
        assert health.hrv.points == ()
        assert training.workouts == 0
        assert training.running_distance_m == 0
        assert training.exercise_load is None
        assert training.average_aerobic_training_effect is None
        assert training.zone_distribution == ()
        assert recovery.pacepilot_readiness_score is None
        assert recovery.pacepilot_readiness_confidence == 0


@pytest.mark.parametrize("days", [0, -1])
def test_invalid_windows_are_rejected(session_factory, days):
    with session_factory() as session:
        user = _user(session)
        service = AthleteDataService(session, user.id, as_of=date(2026, 6, 30))

        with pytest.raises(ValueError, match="days must be at least 1"):
            service.get_health_trends(days)
        with pytest.raises(ValueError, match="days must be at least 1"):
            service.get_training_summary(days)
