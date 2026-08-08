from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Activity,
    DailyHealth,
    GarminAccount,
    GarminDevice,
    GarminSyncState,
    User,
)
from app.services.garmin import sync as sync_module
from app.services.garmin.activity_details import load_activity_details


class FakeGarmin:
    def count_activities(self) -> int:
        return 1

    def get_activities(
        self, start: int = 0, limit: int = 20, activitytype: str | None = None
    ) -> list[dict[str, Any]]:
        return self.get_activities_by_date("", "") if start == 0 else []

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

    def get_activity(self, _activity_id: str) -> dict[str, Any]:
        return {
            "summaryDTO": {"directWorkoutRpe": 4},
            "metadataDTO": {
                "hasHrTimeInZones": True,
                "hasPowerTimeInZones": False,
            },
        }

    def get_user_summary(self, _day: str) -> dict[str, Any]:
        return {
            "totalSteps": 9000,
            "restingHeartRate": 48,
            "averageStressLevel": 24,
            "bodyBatteryHighestValue": 86,
        }

    def get_activity_details(
        self, _activity_id: str, maxchart: int, maxpoly: int
    ) -> dict[str, Any]:
        assert maxchart == maxpoly == 2000
        return {
            "metricDescriptors": [
                {"key": "sumElapsedDuration", "metricsIndex": 0},
                {"key": "directHeartRate", "metricsIndex": 1},
                {"key": "directSpeed", "metricsIndex": 2},
                {"key": "directDoubleCadence", "metricsIndex": 3},
            ],
            "activityDetailMetrics": [
                {"metrics": [0, 140, 2.8, 170]},
                {"metrics": [60, 150, 3.0, 174]},
            ],
            "geoPolylineDTO": {"polyline": [{"lat": 50.0, "lon": 9.0, "valid": True}]},
        }

    def get_activity_splits(self, _activity_id: str) -> dict[str, Any]:
        return {"lapDTOs": [{"distance": 10100.0, "duration": 3600.0}]}

    def get_activity_typed_splits(self, _activity_id: str) -> dict[str, Any]:
        return {"splits": []}

    def get_activity_hr_in_timezones(self, _activity_id: str) -> list[dict[str, Any]]:
        return [{"zoneNumber": 1, "zoneLowBoundary": 100, "secsInZone": 600}]

    def get_sleep_data(self, _day: str) -> dict[str, Any]:
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "sleepScores": {"overall": {"value": 82}},
            }
        }

    def get_hrv_data(self, _day: str) -> dict[str, Any]:
        return {"hrvSummary": {"lastNightAvg": 54.0}}

    def get_body_battery(self, start: str, end: str | None = None) -> list[dict[str, Any]]:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end else first
        rows = []
        day = first
        while day <= last:
            rows.append(
                {
                    "date": day.isoformat(),
                    "charged": 60,
                    "drained": 50,
                    "bodyBatteryValuesArray": [[1, 20], [2, 85]],
                }
            )
            day += timedelta(days=1)
        return rows

    def get_spo2_data(self, _day: str) -> dict[str, Any]:
        return {}

    def get_max_metrics(self, _day: str) -> list[dict[str, Any]]:
        return [{"vo2MaxPreciseValue": 54.0}]

    def get_training_readiness(self, _day: str) -> list[dict[str, Any]]:
        return []

    def get_training_status(self, _day: str) -> dict[str, Any]:
        return {}

    def get_devices(self) -> list[dict[str, Any]]:
        return [{"deviceId": 77, "displayName": "Forerunner", "productType": "FR"}]


def test_sync_normalizes_and_stores_data(
    session_factory: sessionmaker[Session], monkeypatch: Any, tmp_path: Path
) -> None:
    settings = get_settings()
    settings.data_dir = tmp_path
    settings.sync_days = 2
    monkeypatch.setattr(settings, "health_sync_overlap_days", 2)
    monkeypatch.setattr(settings, "garmin_call_delay_seconds", 0)
    monkeypatch.setattr(
        sync_module, "connect_garmin_account", lambda _session, _account: FakeGarmin()
    )

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
        today = date.today()
        for resource in (
            "daily_summary",
            "body_battery",
            "sleep",
            "hrv",
            "spo2",
            "vo2max",
            "training_readiness",
            "training_status",
        ):
            session.add(
                GarminSyncState(
                    user_id=user.id,
                    resource=resource,
                    oldest_synced_date=today - timedelta(days=10),
                    newest_synced_date=today,
                    status="ok",
                    backfill_complete=True,
                )
            )
        session.commit()

        result = sync_module.sync_garmin(session, account)

        assert result.status == "ok"
        assert result.stage == "complete"
        assert result.message == "Synchronisierung abgeschlossen"
        assert result.current_item == result.total_items == 2
        assert result.activities_synced == 1
        assert result.health_days_synced == 2
        activity = session.scalar(select(Activity))
        assert activity is not None
        assert activity.distance_m == 10100.0
        assert activity.raw_file is not None
        assert Path(activity.raw_file).exists()
        activity_data = load_activity_details(
            activity.started_at,
            activity.garmin_activity_id,
            activity.activity_type,
            activity.user_id,
        )
        assert activity_data["series"]["heart_rate"] == [[0.0, 140.0], [60.0, 150.0]]
        assert activity_data["series"]["cadence"] == [[0.0, 170.0], [60.0, 174.0]]
        assert activity_data["route"] == [[50.0, 9.0]]
        health = session.scalar(select(DailyHealth))
        assert health is not None
        assert health.steps == 9000
        assert health.sleep_score == 82
        assert health.hrv_average == 54.0
        assert session.scalar(select(GarminDevice)) is not None
