from datetime import date, datetime, timedelta

from app.models import Activity, DailyFitness, GarminSyncState, User
from app.services.analytics.automatic_profile import get_automatic_athlete_profile


def _weekly_run(user_id: int, key: str, day: date, distance_m: float) -> Activity:
    return Activity(
        user_id=user_id,
        garmin_activity_id=key,
        name=key,
        activity_type="running",
        started_at=datetime.combine(day, datetime.min.time()),
        distance_m=distance_m,
        duration_s=distance_m / 2.8,
        elapsed_duration_s=distance_m / 2.8,
        moving_duration_s=distance_m / 2.8,
        aerobic_training_effect=2.5,
        details_complete=True,
        splits_complete=True,
    )


def test_performance_state_calculates_complete_week_trends(session_factory):
    as_of = date(2026, 8, 12)
    last_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    first_monday = last_sunday - timedelta(weeks=26) + timedelta(days=1)
    distances = [20_000.0] * 22 + [20_000.0, 30_000.0, 35_000.0, 45_000.0]
    with session_factory() as session:
        user = User(display_name="Verlauf")
        session.add(user)
        session.flush()
        session.add_all(
            [
                _weekly_run(
                    user.id,
                    f"week-{index}",
                    first_monday + timedelta(weeks=index),
                    distance,
                )
                for index, distance in enumerate(distances)
            ]
        )
        session.add(
            GarminSyncState(
                user_id=user.id,
                resource="activities",
                status="ok",
                backfill_complete=True,
                oldest_synced_date=first_monday,
                newest_synced_date=last_sunday,
            )
        )
        session.commit()

        profile = get_automatic_athlete_profile(
            session,
            user.id,
            as_of=as_of,
            include_detail_evidence=False,
        )

    state = profile.performance_state
    four_weeks = next(item for item in state.trends if item.horizon_weeks == 4)
    assert state.schema_version == "automatic-performance-state.v1"
    assert state.sport == "running"
    assert four_weeks.covered is True
    assert four_weeks.earlier_weekly_distance_m == 25_000
    assert four_weeks.recent_weekly_distance_m == 40_000
    assert four_weeks.distance_change_percent == 60
    assert four_weeks.distance_direction == "higher"
    assert state.data_quality.level == "high"
    assert state.data_quality.covered_complete_weeks == 26
    assert not hasattr(state, "score")
    assert not hasattr(state, "level")


def test_performance_state_excludes_current_partial_week(session_factory):
    as_of = date(2026, 8, 12)
    last_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    first_monday = last_sunday - timedelta(weeks=12) + timedelta(days=1)
    with session_factory() as session:
        user = User(display_name="Teilwoche")
        session.add(user)
        session.flush()
        activities = [
            _weekly_run(
                user.id,
                f"week-{index}",
                first_monday + timedelta(weeks=index),
                30_000,
            )
            for index in range(12)
        ]
        activities.append(_weekly_run(user.id, "partial", as_of, 100_000))
        session.add_all(
            [
                *activities,
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=first_monday,
                    newest_synced_date=as_of,
                ),
            ]
        )
        session.commit()

        state = get_automatic_athlete_profile(
            session,
            user.id,
            as_of=as_of,
            include_detail_evidence=False,
        ).performance_state

    four_weeks = next(item for item in state.trends if item.horizon_weeks == 4)
    assert four_weeks.recent_weekly_distance_m == 30_000
    assert state.habitual_load.recent_weekly_distance_m == 30_000
    assert state.training_tolerance.recent_weekly_distance_m == 30_000


def test_performance_state_keeps_partial_history_and_missing_metrics_unknown(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Datenlücken")
        session.add(user)
        session.flush()
        incomplete = _weekly_run(user.id, "missing-distance", as_of - timedelta(days=10), 5_000)
        incomplete.distance_m = None
        incomplete.aerobic_training_effect = None
        session.add(incomplete)
        session.commit()

        state = get_automatic_athlete_profile(
            session,
            user.id,
            as_of=as_of,
            include_detail_evidence=False,
        ).performance_state

    assert state.habitual_load.covered is False
    assert all(not trend.covered for trend in state.trends)
    assert state.training_tolerance.distance_position == "unknown"
    assert state.data_quality.level == "low"
    assert state.data_quality.distance_coverage_percent == 0
    assert {item.key for item in state.data_gaps} >= {
        "training_history_partial",
        "weekly_capacity_unknown",
        "threshold_evidence_missing",
    }


def test_performance_state_marks_consistency_as_concrete_strength(session_factory):
    as_of = date(2026, 8, 12)
    last_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    first_monday = last_sunday - timedelta(weeks=12) + timedelta(days=1)
    with session_factory() as session:
        user = User(display_name="Konstant")
        session.add(user)
        session.flush()
        runs = []
        for week in range(12):
            for weekday in (0, 2, 5):
                runs.append(
                    _weekly_run(
                        user.id,
                        f"{week}-{weekday}",
                        first_monday + timedelta(weeks=week, days=weekday),
                        8_000,
                    )
                )
        session.add_all(
            [
                *runs,
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=first_monday,
                    newest_synced_date=last_sunday,
                ),
            ]
        )
        session.commit()

        state = get_automatic_athlete_profile(
            session,
            user.id,
            as_of=as_of,
            include_detail_evidence=False,
        ).performance_state

    assert any(item.key == "training_consistency" for item in state.strengths)
    assert all(item.kind != "development_gap" for item in state.data_gaps)


def test_performance_state_compares_dated_threshold_pace(session_factory):
    as_of = date(2026, 8, 12)
    last_sunday = as_of - timedelta(days=as_of.weekday() + 1)
    first_monday = last_sunday - timedelta(weeks=26) + timedelta(days=1)
    with session_factory() as session:
        user = User(display_name="Schwellenverlauf")
        session.add(user)
        session.flush()
        session.add_all(
            [
                *[
                    _weekly_run(
                        user.id,
                        f"threshold-week-{index}",
                        first_monday + timedelta(weeks=index),
                        20_000,
                    )
                    for index in range(26)
                ],
                DailyFitness(
                    user_id=user.id,
                    day=as_of - timedelta(weeks=12),
                    lactate_threshold_speed_mps=3.7,
                ),
                DailyFitness(
                    user_id=user.id,
                    day=as_of,
                    lactate_threshold_speed_mps=4.0,
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=first_monday,
                    newest_synced_date=last_sunday,
                ),
            ]
        )
        session.commit()

        state = get_automatic_athlete_profile(
            session,
            user.id,
            as_of=as_of,
            include_detail_evidence=False,
        ).performance_state

    trend = next(item for item in state.trends if item.horizon_weeks == 12)
    assert trend.earlier_threshold_pace_s_per_km == 1_000 / 3.7
    assert trend.recent_threshold_pace_s_per_km == 250
    assert trend.threshold_pace_direction == "lower"
