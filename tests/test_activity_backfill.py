from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Activity,
    ActivityExerciseSet,
    ActivitySplit,
    ActivityZone,
    GarminSyncState,
    User,
)
from app.services.garmin.activity_backfill import sync_activity_history
from app.services.garmin.activity_details import load_activity_details


def _activity(
    activity_id: int, started_at: str, activity_type: str, *, name: str
) -> dict[str, Any]:
    running = activity_type == "running"
    return {
        "activityId": activity_id,
        "activityName": name,
        "activityType": {"typeKey": activity_type},
        "startTimeLocal": started_at,
        "distance": 10_000.0 if running else None,
        "duration": 3_600.0,
        "elapsedDuration": 3_700.0,
        "movingDuration": 3_500.0,
        "averageSpeed": 2.85 if running else None,
        "maxSpeed": 4.2 if running else None,
        "averageHR": 145,
        "maxHR": 175,
        "calories": 650,
        "elevationGain": 120.0 if running else None,
        "elevationLoss": 118.0 if running else None,
        "averageRunningCadenceInStepsPerMinute": 172 if running else None,
        "maxRunningCadenceInStepsPerMinute": 184 if running else None,
        "avgPower": 280 if running else None,
        "maxPower": 410 if running else None,
        "normPower": 292 if running else None,
        "aerobicTrainingEffect": 3.5,
        "anaerobicTrainingEffect": 1.2,
        "trainingEffectLabel": "TEMPO",
        "vO2MaxValue": 54.0 if running else None,
        "avgStrideLength": 112.0 if running else None,
        "avgGroundContactTime": 245.0 if running else None,
        "avgVerticalOscillation": 8.2 if running else None,
        "avgVerticalRatio": 7.4 if running else None,
        "moderateIntensityMinutes": 20,
        "vigorousIntensityMinutes": 15,
        "differenceBodyBattery": -18,
    }


class FakeActivityGarmin:
    def __init__(self, fail_details_once: str | None = None) -> None:
        self.activities = [
            _activity(3, "2026-08-06 07:00:00", "running", name="Recent Run"),
            _activity(2, "2026-06-01 08:00:00", "cycling", name="Ride"),
            _activity(1, "2026-03-30 18:00:00", "strength_training", name="Strength"),
        ]
        self.fail_details_once = fail_details_once
        self.calls: list[tuple[str, str]] = []

    def count_activities(self) -> int:
        self.calls.append(("count", ""))
        return len(self.activities)

    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("list", str(start)))
        return self.activities[start : start + limit]

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        self.calls.append(("summary", str(activity_id)))
        running = str(activity_id) == "3"
        return {
            "summaryDTO": {
                "directWorkoutRpe": 5 if running else None,
                "directWorkoutFeel": 75 if running else None,
            },
            "metadataDTO": {
                "hasHrTimeInZones": True,
                "hasPowerTimeInZones": running,
                "associatedWorkoutId": 999 if running else None,
                "lastUpdateDate": "2026-08-07T10:00:00",
            },
        }

    def get_activity_details(self, activity_id: str, maxchart: int, maxpoly: int) -> dict[str, Any]:
        self.calls.append(("details", str(activity_id)))
        assert maxchart == maxpoly == 2000
        if str(activity_id) == self.fail_details_once:
            self.fail_details_once = None
            raise RuntimeError("detail request failed")
        if str(activity_id) == "1":
            return {"detailsAvailable": False}
        return {
            "detailsAvailable": True,
            "metricDescriptors": [
                {"key": "sumElapsedDuration", "metricsIndex": 0},
                {"key": "directHeartRate", "metricsIndex": 1},
                {"key": "directPower", "metricsIndex": 2},
            ],
            "activityDetailMetrics": [{"metrics": [0, 140, 270]}, {"metrics": [60, 150, 290]}],
        }

    def get_activity_splits(self, activity_id: str) -> dict[str, Any]:
        self.calls.append(("laps", str(activity_id)))
        return {
            "lapDTOs": [
                {
                    "distance": 1_000.0,
                    "duration": 240.0,
                    "averageHR": 148,
                    "maxHR": 165,
                }
            ]
        }

    def get_activity_typed_splits(self, activity_id: str) -> dict[str, Any]:
        self.calls.append(("typed", str(activity_id)))
        return (
            {"splits": [{"type": "INTERVAL", "distance": 1_000.0, "duration": 230.0}]}
            if str(activity_id) == "3"
            else {"splits": []}
        )

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.calls.append(("hr_zones", str(activity_id)))
        return [
            {"zoneNumber": zone, "zoneLowBoundary": 90 + zone * 10, "secsInZone": zone * 100}
            for zone in range(1, 6)
        ]

    def get_activity_power_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.calls.append(("power_zones", str(activity_id)))
        return [
            {"zoneNumber": zone, "zoneLowBoundary": 100 + zone * 30, "secsInZone": zone * 50}
            for zone in range(1, 6)
        ]

    def get_activity_exercise_sets(self, activity_id: str) -> dict[str, Any]:
        self.calls.append(("exercise_sets", str(activity_id)))
        return {
            "exerciseSets": [
                {
                    "setType": "ACTIVE",
                    "duration": 45.0,
                    "repetitionCount": 10,
                    "weight": 50.0,
                    "wktStepIndex": 1,
                    "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}],
                }
            ]
        }


def _user(session: Session, name: str = "Activity") -> User:
    user = User(display_name=name)
    session.add(user)
    session.flush()
    return user


def test_activity_backfill_imports_details_and_skips_unchanged(
    session_factory: sessionmaker[Session], monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    client = FakeActivityGarmin()
    with session_factory() as session:
        user = _user(session)
        first = sync_activity_history(session, client, user.id, delay=0, page_size=2)

        assert first.remote_count == 3
        assert first.inserted == 3
        assert first.oldest == date(2026, 3, 30)
        assert first.newest == date(2026, 8, 6)
        assert session.scalar(select(func.count()).select_from(Activity)) == 3
        assert session.scalar(select(func.count()).select_from(ActivitySplit)) == 4
        assert session.scalar(select(func.count()).select_from(ActivityZone)) == 20
        assert session.scalar(select(func.count()).select_from(ActivityExerciseSet)) == 1

        recent = session.scalar(select(Activity).where(Activity.garmin_activity_id == "3"))
        assert recent is not None
        assert recent.vo2max == 54.0
        assert recent.workout_rpe == 5
        assert recent.workout_feel == 75
        assert recent.details_complete is True
        assert recent.splits_complete is True
        assert recent.raw_file is not None and f"user-{user.id}" in recent.raw_file
        assert recent.details_file is not None and Path(recent.details_file).is_file()
        normalized = load_activity_details(
            recent.started_at,
            recent.garmin_activity_id,
            recent.activity_type,
            recent.user_id,
        )
        assert normalized["summary"]["average_power"] == 280.0

        detail_calls = len([call for call in client.calls if call[0] == "details"])
        counts = (
            session.scalar(select(func.count()).select_from(Activity)),
            session.scalar(select(func.count()).select_from(ActivitySplit)),
            session.scalar(select(func.count()).select_from(ActivityZone)),
            session.scalar(select(func.count()).select_from(ActivityExerciseSet)),
        )
        second = sync_activity_history(session, client, user.id, delay=0, page_size=2)
        assert second.inserted == second.updated == 0
        assert second.skipped == 2
        assert len([call for call in client.calls if call[0] == "details"]) == detail_calls
        assert counts == (
            session.scalar(select(func.count()).select_from(Activity)),
            session.scalar(select(func.count()).select_from(ActivitySplit)),
            session.scalar(select(func.count()).select_from(ActivityZone)),
            session.scalar(select(func.count()).select_from(ActivityExerciseSet)),
        )

        client.activities[0]["activityName"] = "Updated Run"
        updated = sync_activity_history(session, client, user.id, delay=0, page_size=2)
        assert updated.updated == 1
        session.expire_all()
        renamed = session.scalar(select(Activity).where(Activity.garmin_activity_id == "3"))
        assert renamed is not None and renamed.name == "Updated Run"

        client.activities.insert(0, _activity(4, "2026-08-08 06:30:00", "running", name="New Run"))
        incremental = sync_activity_history(session, client, user.id, delay=0, page_size=2)
        assert incremental.inserted == 1
        assert session.scalar(select(func.count()).select_from(Activity)) == 4


def test_activity_backfill_resumes_failed_page_and_handles_sparse_old_activity(
    session_factory: sessionmaker[Session], monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    client = FakeActivityGarmin(fail_details_once="2")
    with session_factory() as session:
        user = _user(session)
        with pytest.raises(RuntimeError, match="detail request failed"):
            sync_activity_history(session, client, user.id, delay=0, page_size=3)

        state = session.scalar(
            select(GarminSyncState).where(
                GarminSyncState.user_id == user.id,
                GarminSyncState.resource == "activities",
            )
        )
        assert state is not None
        assert state.status == "error"
        assert state.cursor is None
        assert session.scalar(select(func.count()).select_from(Activity)) == 1
        recent_detail_calls = client.calls.count(("details", "3"))

        resumed = sync_activity_history(session, client, user.id, delay=0, page_size=3)
        assert resumed.backfill_complete is True
        assert resumed.skipped == 1
        assert client.calls.count(("details", "3")) == recent_detail_calls
        assert session.scalar(select(func.count()).select_from(Activity)) == 3
        oldest = session.scalar(select(Activity).where(Activity.garmin_activity_id == "1"))
        assert oldest is not None
        assert oldest.details_complete is True
        assert oldest.details_file is None
        assert len(oldest.exercise_sets) == 1


def test_activity_ids_and_raw_files_are_scoped_per_user(
    session_factory: sessionmaker[Session], monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    with session_factory() as session:
        first_user = _user(session, "One")
        second_user = _user(session, "Two")
        first_client = FakeActivityGarmin()
        second_client = FakeActivityGarmin()

        sync_activity_history(session, first_client, first_user.id, delay=0, page_size=100)
        sync_activity_history(session, second_client, second_user.id, delay=0, page_size=100)

        assert session.scalar(select(func.count()).select_from(Activity)) == 6
        first = session.scalar(select(Activity).where(Activity.user_id == first_user.id))
        second = session.scalar(select(Activity).where(Activity.user_id == second_user.id))
        assert first is not None and second is not None
        assert first.raw_file != second.raw_file
        assert f"user-{first_user.id}" in str(first.raw_file)
        assert f"user-{second_user.id}" in str(second.raw_file)
