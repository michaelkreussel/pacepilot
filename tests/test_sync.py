from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Activity, DailyHealth, GarminAccount, GarminDevice, User
from app.services.garmin import sync as sync_module


class FakeGarmin:
    def get_activities_by_date(self, _start: str, _end: str) -> list[dict[str, Any]]:
        return [
            {
                "activityId": 12345,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2026-08-05 07:30:00",
                "distance": 10100.0,
                "duration": 3600.0,
                "averageHR": 145,
                "maxHR": 172,
                "calories": 700,
                "elevationGain": 80.0,
            }
        ]

    def get_user_summary(self, _day: str) -> dict[str, Any]:
        return {
            "totalSteps": 9000,
            "restingHeartRate": 48,
            "averageStressLevel": 24,
            "bodyBatteryHighestValue": 86,
        }

    def get_sleep_data(self, _day: str) -> dict[str, Any]:
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "sleepScores": {"overall": {"value": 82}},
            }
        }

    def get_hrv_data(self, _day: str) -> dict[str, Any]:
        return {"hrvSummary": {"lastNightAvg": 54.0}}

    def get_devices(self) -> list[dict[str, Any]]:
        return [{"deviceId": 77, "displayName": "Forerunner", "productType": "FR"}]


def test_sync_normalizes_and_stores_data(
    session_factory: sessionmaker[Session], monkeypatch: Any, tmp_path: Path
) -> None:
    settings = get_settings()
    settings.data_dir = tmp_path
    settings.sync_days = 2
    monkeypatch.setattr(sync_module, "connect_garmin", lambda: FakeGarmin())

    with session_factory() as session:
        user = User(display_name="Test")
        session.add(user)
        session.flush()
        account = GarminAccount(
            user_id=user.id,
            connected_at=datetime.now(UTC).replace(tzinfo=None),
            sync_status="connected",
        )
        session.add(account)
        session.commit()

        result = sync_module.sync_garmin(session, account)

        assert result.status == "ok"
        assert result.activities_synced == 1
        assert result.health_days_synced == 2
        activity = session.scalar(select(Activity))
        assert activity is not None
        assert activity.distance_m == 10100.0
        assert activity.raw_file is not None
        assert Path(activity.raw_file).exists()
        health = session.scalar(select(DailyHealth))
        assert health is not None
        assert health.sleep_score == 82
        assert health.hrv_average == 54.0
        assert session.scalar(select(GarminDevice)) is not None
