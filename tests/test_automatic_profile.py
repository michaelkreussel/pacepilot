from datetime import date, datetime, timedelta

from app.models import Activity, ActivitySplit, ActivityZone, DailyFitness, GarminSyncState, User
from app.services.analytics.automatic_profile import get_automatic_athlete_profile


def _run(
    user_id: int, key: str, started_at: datetime, distance: float, duration: float
) -> Activity:
    return Activity(
        user_id=user_id,
        garmin_activity_id=key,
        name=key,
        activity_type="running",
        started_at=started_at,
        distance_m=distance,
        duration_s=duration,
    )


def test_automatic_profile_prefers_garmin_record_and_uses_exact_split(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Bestzeiten")
        session.add(user)
        session.flush()
        run = _run(user.id, "split-run", datetime(2026, 8, 1), 8_000, 2_400)
        run.splits = [
            ActivitySplit(
                split_type="lap",
                position=0,
                distance_m=1_002,
                elapsed_duration_s=225,
            ),
            ActivitySplit(
                split_type="interval",
                position=0,
                distance_m=5_100,
                elapsed_duration_s=1_100,
            ),
        ]
        session.add_all(
            [
                run,
                DailyFitness(
                    user_id=user.id,
                    day=as_of,
                    personal_record_5k_seconds=1_180,
                ),
            ]
        )
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    efforts = {item.distance_key: item for item in profile.best_efforts}
    assert efforts["1k"].duration_s == 225
    assert efforts["1k"].source == "observed_split"
    assert efforts["5k"].duration_s == 1_180
    assert efforts["5k"].source == "garmin_personal_record"
    assert "10k" not in efforts


def test_automatic_profile_calculates_complete_week_capacity_with_zero_weeks(session_factory):
    as_of = date(2026, 8, 12)  # Wednesday; the current week is excluded.
    last_complete_end = date(2026, 8, 9)
    first_week_start = last_complete_end - timedelta(weeks=12) + timedelta(days=1)
    weekly_km = [20, 30, 0, 40, 50, 30, 20, 0, 40, 50, 60, 70]
    with session_factory() as session:
        user = User(display_name="Kapazität")
        session.add(user)
        session.flush()
        activities = []
        for index, kilometers in enumerate(weekly_km):
            if not kilometers:
                continue
            day = first_week_start + timedelta(weeks=index)
            activities.append(
                _run(
                    user.id,
                    f"week-{index}",
                    datetime.combine(day, datetime.min.time()),
                    kilometers * 1_000,
                    kilometers * 360,
                )
            )
        session.add_all(
            [
                *activities,
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=first_week_start,
                    newest_synced_date=last_complete_end,
                ),
            ]
        )
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    assert profile.weekly_capacity.covered is True
    assert profile.weekly_capacity.sustainable_distance_m == 45_000
    assert profile.weekly_capacity.sessions_per_week_median == 1
    assert profile.weekly_capacity.active_days_per_week_median == 1


def test_automatic_profile_keeps_capacity_unknown_when_history_is_partial(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Teilweise")
        session.add(user)
        session.flush()
        session.add(_run(user.id, "recent", datetime(2026, 8, 8), 10_000, 3_600))
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    assert profile.weekly_capacity.covered is False
    assert profile.weekly_capacity.sustainable_distance_m is None
    assert "weekly_capacity_unknown" in profile.warnings


def test_automatic_profile_requires_hr_coverage_before_reporting_intensity(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        user = User(display_name="Intensität")
        session.add(user)
        session.flush()
        activities = []
        for index in range(10):
            run = _run(
                user.id,
                f"zones-{index}",
                datetime(2026, 7, 1) + timedelta(days=index),
                8_000,
                1_800,
            )
            run.zones = [
                ActivityZone(zone_type="heart_rate", zone_number=2, seconds=1_200),
                ActivityZone(zone_type="heart_rate", zone_number=3, seconds=300),
                ActivityZone(zone_type="heart_rate", zone_number=4, seconds=300),
            ]
            activities.append(run)
        session.add_all(
            [
                *activities,
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=as_of - timedelta(days=83),
                    newest_synced_date=as_of,
                ),
            ]
        )
        session.commit()

        profile = get_automatic_athlete_profile(session, user.id, as_of=as_of)

    assert profile.intensity.sufficient is True
    assert profile.intensity.coverage_percent == 100
    assert profile.intensity.low_percent == 66.7
    assert profile.intensity.moderate_percent == 16.7
    assert profile.intensity.high_percent == 16.7


def test_automatic_profile_is_user_scoped(session_factory):
    as_of = date(2026, 8, 12)
    with session_factory() as session:
        first = User(display_name="Erste")
        second = User(display_name="Zweite")
        session.add_all([first, second])
        session.flush()
        hidden = _run(second.id, "hidden", datetime(2026, 8, 1), 5_000, 900)
        hidden.splits = [
            ActivitySplit(
                split_type="lap",
                position=0,
                distance_m=5_000,
                elapsed_duration_s=900,
            )
        ]
        session.add(hidden)
        session.commit()

        profile = get_automatic_athlete_profile(session, first.id, as_of=as_of)

    assert profile.best_efforts == ()
