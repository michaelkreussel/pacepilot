from datetime import date, datetime, timedelta

from app.models import Activity, ActivitySplit, ActivityZone, User
from app.services.analytics.automatic_profile import get_automatic_athlete_profile


def _run(
    user_id: int,
    key: str,
    day: date,
    *,
    duration_s: float,
    distance_m: float,
    average_hr: int | None = None,
    activity_type: str = "running",
) -> Activity:
    return Activity(
        user_id=user_id,
        garmin_activity_id=key,
        name=key,
        activity_type=activity_type,
        started_at=datetime.combine(day, datetime.min.time()),
        duration_s=duration_s,
        elapsed_duration_s=duration_s,
        moving_duration_s=duration_s,
        distance_m=distance_m,
        average_hr=average_hr,
        elevation_gain_m=20,
    )


def test_easy_range_requires_multiple_low_intensity_sessions(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Easy")
        session.add(user)
        session.flush()
        runs = []
        for index, pace in enumerate((330, 335, 340, 345, 350, 355)):
            duration = 1_800
            run = _run(
                user.id,
                f"easy-{index}",
                as_of - timedelta(days=7 * index),
                duration_s=duration,
                distance_m=duration * 1_000 / pace,
                average_hr=140 + index,
            )
            run.zones = [
                ActivityZone(zone_type="heart_rate", zone_number=2, seconds=1_710),
                ActivityZone(zone_type="heart_rate", zone_number=3, seconds=90),
            ]
            runs.append(run)
        session.add_all(runs)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    easy = next(
        item for item in profile.training_ranges if item.key == "easy" and item.stratum == "road"
    )
    assert easy.sufficient is True
    assert easy.pace_median_s_per_km == 342.5
    assert easy.pace_p25_s_per_km == 336.25
    assert easy.pace_p75_s_per_km == 348.75
    assert easy.heart_rate_median == 142.5
    assert easy.sample_sessions == 6
    assert easy.sample_minutes == 180


def test_easy_range_keeps_trail_separate_and_rejects_stopped_run(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Getrennt")
        session.add(user)
        session.flush()
        activities = []
        for index in range(6):
            trail = _run(
                user.id,
                f"trail-{index}",
                as_of - timedelta(days=index),
                duration_s=1_800,
                distance_m=4_000,
                activity_type="trail_running",
            )
            trail.workout_rpe = 3
            activities.append(trail)
        stopped = _run(
            user.id,
            "stopped-road",
            as_of,
            duration_s=1_800,
            distance_m=8_000,
        )
        stopped.elapsed_duration_s = 3_000
        stopped.moving_duration_s = 1_800
        stopped.workout_rpe = 2
        activities.append(stopped)
        session.add_all(activities)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    road = next(
        item for item in profile.training_ranges if item.key == "easy" and item.stratum == "road"
    )
    trail = next(
        item for item in profile.training_ranges if item.key == "easy" and item.stratum == "trail"
    )
    assert road.sample_sessions == 0
    assert road.sufficient is False
    assert trail.sample_sessions == 6
    assert trail.sufficient is True


def test_tempo_range_uses_typed_tempo_splits_only(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Tempo")
        session.add(user)
        session.flush()
        activities = []
        for index, pace in enumerate((270, 275, 280)):
            run = _run(
                user.id,
                f"tempo-{index}",
                as_of - timedelta(days=index * 14),
                duration_s=3_600,
                distance_m=12_000,
            )
            split_duration = 1_200
            run.splits = [
                ActivitySplit(
                    split_type="typed_tempo",
                    position=0,
                    intensity_type="TEMPO",
                    duration_s=split_duration,
                    distance_m=split_duration * 1_000 / pace,
                    average_hr=168 + index,
                ),
                ActivitySplit(
                    split_type="lap",
                    position=0,
                    intensity_type="ACTIVE",
                    duration_s=600,
                    distance_m=3_000,
                ),
            ]
            activities.append(run)
        session.add_all(activities)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    tempo = next(
        item for item in profile.training_ranges if item.key == "tempo" and item.stratum == "road"
    )
    assert tempo.sufficient is True
    assert tempo.pace_median_s_per_km == 275
    assert tempo.heart_rate_median == 169
    assert tempo.sample_sessions == 3
    assert tempo.sample_efforts == 3
    assert tempo.sample_minutes == 60


def test_tempo_range_uses_steady_garmin_tempo_activities(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Steady Tempo")
        session.add(user)
        session.flush()
        activities = []
        for index, pace in enumerate((300, 305, 310)):
            duration = 1_800
            run = _run(
                user.id,
                f"steady-tempo-{index}",
                as_of - timedelta(days=index * 14),
                duration_s=duration,
                distance_m=duration * 1_000 / pace,
                average_hr=165 + index,
            )
            run.training_effect_label = "TEMPO" if index < 2 else "LACTATE_THRESHOLD"
            run.splits = [
                ActivitySplit(
                    split_type="lap",
                    position=position,
                    intensity_type="ACTIVE",
                    duration_s=pace,
                    distance_m=1_000,
                    average_hr=165 + index,
                )
                for position in range(5)
            ]
            activities.append(run)
        session.add_all(activities)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    tempo = next(
        item for item in profile.training_ranges if item.key == "tempo" and item.stratum == "road"
    )
    assert tempo.sufficient is True
    assert tempo.pace_median_s_per_km == 305
    assert tempo.heart_rate_median == 166
    assert tempo.sample_sessions == 3
    assert tempo.sample_minutes == 90


def test_tempo_range_rejects_unsteady_or_interval_labeled_activity(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Kein Tempo")
        session.add(user)
        session.flush()
        unsteady = _run(
            user.id,
            "unsteady",
            as_of,
            duration_s=1_800,
            distance_m=6_000,
        )
        unsteady.training_effect_label = "TEMPO"
        unsteady.splits = [
            ActivitySplit(
                split_type="lap",
                position=position,
                duration_s=duration,
                distance_m=1_000,
            )
            for position, duration in enumerate((240, 300, 420, 250, 410))
        ]
        intervals = _run(
            user.id,
            "intervals",
            as_of,
            duration_s=1_800,
            distance_m=6_000,
        )
        intervals.training_effect_label = "TEMPO"
        intervals.splits = [
            ActivitySplit(
                split_type="typed_INTERVAL_ACTIVE",
                position=0,
                duration_s=1_800,
                distance_m=6_000,
            )
        ]
        session.add_all([unsteady, intervals])
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    tempo = next(
        item for item in profile.training_ranges if item.key == "tempo" and item.stratum == "road"
    )
    assert tempo.sample_sessions == 0
    assert tempo.sufficient is False


def test_interval_range_combines_repetition_lengths(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Intervalle")
        session.add(user)
        session.flush()
        activities = []
        for session_index in range(3):
            run = _run(
                user.id,
                f"interval-{session_index}",
                as_of - timedelta(days=session_index * 14),
                duration_s=3_600,
                distance_m=10_000,
            )
            run.splits = [
                ActivitySplit(
                    split_type="typed_interval",
                    position=position,
                    intensity_type="INTERVAL",
                    duration_s=120,
                    distance_m=600,
                )
                for position in range(4)
            ]
            activities.append(run)
        session.add_all(activities)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    interval = next(
        item
        for item in profile.training_ranges
        if item.key == "interval" and item.stratum == "road"
    )
    assert interval.sufficient is True
    assert interval.pace_median_s_per_km == 200
    assert interval.sample_sessions == 3
    assert interval.sample_efforts == 12
    assert interval.sample_minutes == 24
    assert interval.confidence == "medium"


def test_interval_range_detects_repeated_rwd_run_segments(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Garmin Intervalle")
        session.add(user)
        session.flush()
        run = _run(
            user.id,
            "rwd-intervals",
            as_of,
            duration_s=2_400,
            distance_m=6_000,
        )
        run.training_effect_label = "VO2MAX"
        run.splits = [
            ActivitySplit(
                split_type="typed_RWD_RUN",
                position=position,
                duration_s=150,
                distance_m=500,
                average_hr=170 + position,
            )
            for position in range(4)
        ]
        session.add(run)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    interval = next(
        item
        for item in profile.training_ranges
        if item.key == "interval" and item.stratum == "road"
    )
    assert interval.sufficient is True
    assert interval.pace_median_s_per_km == 300
    assert interval.sample_sessions == 1
    assert interval.sample_efforts == 4
    assert interval.sample_minutes == 10
    assert interval.confidence == "low"


def test_long_run_range_requires_four_low_intensity_runs(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Lang")
        session.add(user)
        session.flush()
        runs = []
        for index in range(4):
            run = _run(
                user.id,
                f"long-{index}",
                as_of - timedelta(days=index * 21),
                duration_s=4_800,
                distance_m=14_000,
                average_hr=145,
            )
            run.workout_rpe = 3
            runs.append(run)
        session.add_all(runs)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    long_run = next(
        item
        for item in profile.training_ranges
        if item.key == "long_run" and item.stratum == "road"
    )
    assert long_run.sufficient is True
    assert long_run.sample_sessions == 4
    assert long_run.sample_minutes == 320


def test_long_run_range_accepts_aerobic_base_history(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Aerobic Base")
        session.add(user)
        session.flush()
        runs = []
        for index in range(4):
            run = _run(
                user.id,
                f"base-long-{index}",
                as_of - timedelta(days=index * 21),
                duration_s=4_800,
                distance_m=10_000,
                average_hr=145,
            )
            run.training_effect_label = "AEROBIC_BASE"
            run.aerobic_training_effect = 3.4
            run.anaerobic_training_effect = 0.2
            runs.append(run)
        session.add_all(runs)
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    long_run = next(
        item
        for item in profile.training_ranges
        if item.key == "long_run" and item.stratum == "road"
    )
    assert long_run.sufficient is True
    assert long_run.pace_median_s_per_km == 480
    assert long_run.sample_sessions == 4
