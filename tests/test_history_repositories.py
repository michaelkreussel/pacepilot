from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    ActivityExerciseSet,
    ActivitySplit,
    ActivityZone,
    DailyDataStatus,
    SleepStage,
    User,
    Workout,
)
from app.repositories.activities import (
    find_activity_with_history,
    get_or_create_activity,
    replace_activity_exercise_sets,
    replace_activity_splits,
    replace_activity_zones,
)
from app.repositories.fitness import fitness_between, get_or_create_daily_fitness
from app.repositories.health import (
    find_health_day,
    get_or_create_health_day,
    health_between,
    replace_sleep_stages,
    set_daily_data_status,
)
from app.repositories.sync_state import (
    get_or_create_sync_state,
    mark_sync_attempt,
    mark_sync_error,
    mark_sync_success,
)


def test_health_and_fitness_primitives_are_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="History")
        session.add(user)
        session.flush()
        day = date(2026, 8, 7)

        health = get_or_create_health_day(session, user.id, day)
        health.sleep_seconds = 27_000
        health.sleep_need_seconds = 28_800
        health.hrv_average = 52.0
        replace_sleep_stages(
            session,
            health,
            [
                SleepStage(
                    position=0,
                    stage="light",
                    started_at=datetime(2026, 8, 6, 23, 0),
                    ended_at=datetime(2026, 8, 6, 23, 40),
                ),
                SleepStage(
                    position=1,
                    stage="deep",
                    started_at=datetime(2026, 8, 6, 23, 40),
                    ended_at=datetime(2026, 8, 7, 0, 20),
                ),
            ],
        )
        set_daily_data_status(session, user.id, day, "sleep", "complete")
        set_daily_data_status(session, user.id, day, "spo2", "empty")

        fitness = get_or_create_daily_fitness(session, user.id, day)
        fitness.vo2max = 54.0
        session.commit()

        assert get_or_create_health_day(session, user.id, day).id == health.id
        assert get_or_create_daily_fitness(session, user.id, day).id == fitness.id
        loaded = find_health_day(session, user.id, day)
        assert loaded is not None
        assert [stage.stage for stage in loaded.sleep_stages] == ["light", "deep"]
        assert health_between(session, user.id, day, day) == [loaded]
        assert fitness_between(session, user.id, day, day) == [fitness]
        statuses = list(session.scalars(select(DailyDataStatus).order_by(DailyDataStatus.resource)))
        assert [(row.resource, row.status) for row in statuses] == [
            ("sleep", "complete"),
            ("spo2", "empty"),
        ]


def test_activity_history_primitives_replace_detail_children(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Training")
        session.add(user)
        session.flush()
        workout = Workout(user_id=user.id, name="Intervals", sport="running", status="published")
        session.add(workout)
        session.flush()

        activity = get_or_create_activity(
            session,
            user.id,
            "12345",
            name="Run",
            activity_type="running",
            started_at=datetime(2026, 8, 6, 7, 30),
        )
        activity.workout_id = workout.id
        activity.aerobic_training_effect = 3.5
        replace_activity_zones(
            session,
            activity,
            [ActivityZone(zone_type="heart_rate", zone_number=3, seconds=900)],
        )
        replace_activity_splits(
            session,
            activity,
            [ActivitySplit(split_type="lap", position=0, distance_m=1000, duration_s=240)],
        )
        replace_activity_exercise_sets(
            session,
            activity,
            [ActivityExerciseSet(position=0, set_type="active", repetitions=10)],
        )
        session.commit()

        assert (
            get_or_create_activity(
                session,
                user.id,
                "12345",
                name="Ignored",
                activity_type="other",
                started_at=datetime(2026, 1, 1),
            ).id
            == activity.id
        )
        loaded = find_activity_with_history(session, user.id, activity.id)
        assert loaded is not None
        assert loaded.workout_id == workout.id
        assert loaded.zones[0].seconds == 900
        assert loaded.splits[0].distance_m == 1000
        assert loaded.exercise_sets[0].repetitions == 10

        replace_activity_splits(
            session,
            loaded,
            [ActivitySplit(split_type="lap", position=0, distance_m=2000, duration_s=500)],
        )
        session.commit()
        session.expire_all()
        replaced = find_activity_with_history(session, user.id, activity.id)
        assert replaced is not None
        assert [(split.distance_m, split.duration_s) for split in replaced.splits] == [(2000, 500)]


def test_sync_state_tracks_ranges_resume_and_errors(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Sync")
        session.add(user)
        session.flush()

        state = get_or_create_sync_state(session, user.id, "daily_health")
        mark_sync_attempt(state)
        mark_sync_success(
            state,
            oldest_date=date(2026, 7, 1),
            newest_date=date(2026, 8, 7),
            backfill_cursor_date=date(2026, 6, 30),
        )
        mark_sync_success(
            state,
            oldest_date=date(2026, 3, 26),
            newest_date=date(2026, 8, 8),
            backfill_cursor_date=None,
            backfill_complete=True,
        )
        session.commit()

        same_state = get_or_create_sync_state(session, user.id, "daily_health")
        assert same_state.id == state.id
        assert same_state.oldest_synced_date == date(2026, 3, 26)
        assert same_state.newest_synced_date == date(2026, 8, 8)
        assert same_state.backfill_complete is True
        assert same_state.last_success_at is not None

        mark_sync_error(same_state, "x" * 1200)
        assert same_state.status == "error"
        assert len(same_state.error or "") == 1000
