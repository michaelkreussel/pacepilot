from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from garminconnect.exceptions import GarminConnectTooManyRequestsError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Activity,
    DailyFitness,
    DailyHealth,
    GarminAccount,
    GarminDevice,
    GarminSyncState,
    SyncEvent,
    SyncRun,
    User,
)
from app.services.garmin import sync as sync_module
from app.services.garmin.activity_details import load_activity_details
from app.services.garmin.client import GarminUnavailableError
from app.services.garmin.health_backfill import GarminPacer
from app.services.garmin.performance_profile import sync_performance_profile
from app.services.garmin.sync import rate_limit_cooldown_remaining


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

    def get_lactate_threshold(self) -> dict[str, Any]:
        return {
            "speed_and_heart_rate": {
                "calendarDate": date.today().isoformat(),
                "speed": 4.0,
                "heartRate": 171,
            },
            "power": {"functionalThresholdPower": 315},
        }

    def get_cycling_ftp(self) -> dict[str, Any]:
        return {"functionalThresholdPower": 280}

    def get_race_predictions(self) -> dict[str, Any]:
        return {
            "calendarDate": date.today().isoformat(),
            "raceTime5K": 1_200,
            "raceTime10K": 2_520,
            "raceTimeHalf": 5_700,
            "raceTimeMarathon": 12_000,
        }

    def get_personal_record(self) -> list[dict[str, Any]]:
        return [{"type": "10K", "recordValueInSeconds": 2_600, "recordDate": "2026-07-01"}]

    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        return [
            {
                "sport": "RUNNING",
                "maxHeartRate": 190,
                "lactateThresholdHeartRate": 171,
                "zone1Floor": 95,
                "zone2Floor": 114,
                "zone3Floor": 133,
                "zone4Floor": 152,
                "zone5Floor": 171,
            }
        ]

    def get_power_zones(self) -> list[dict[str, Any]]:
        return [
            {
                "sport": "CYCLING",
                "zone1Floor": 100,
                "zone2Floor": 150,
                "zone3Floor": 200,
                "zone4Floor": 250,
                "zone5Floor": 300,
            }
        ]

    def get_devices(self) -> list[dict[str, Any]]:
        return [{"deviceId": 77, "displayName": "Forerunner", "productType": "FR"}]


class RateLimitedGarmin(FakeGarmin):
    def count_activities(self) -> int:
        raise GarminConnectTooManyRequestsError("slow down")


class DynamicPerformanceGarmin(FakeGarmin):
    def __init__(self) -> None:
        self.ftp = 280

    def get_cycling_ftp(self) -> dict[str, Any]:
        return {"functionalThresholdPower": self.ftp}


def test_sync_normalizes_and_stores_data(
    session_factory: sessionmaker[Session], monkeypatch: Any, tmp_path: Path
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "health_sync_overlap_days", 2)
    monkeypatch.setattr(settings, "garmin_call_delay_seconds", 0)
    monkeypatch.setattr(settings, "garmin_activity_initial_enrichment", 1)
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
        assert result.days_completed == result.days_total == 2
        assert result.operations_completed == result.operations_total == 16
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
        fitness = session.scalar(select(DailyFitness).where(DailyFitness.day == date.today()))
        assert fitness is not None
        assert fitness.lactate_threshold_hr == 171
        assert fitness.lactate_threshold_speed_mps == 4
        assert fitness.running_ftp_watts == 315
        assert fitness.cycling_ftp_watts == 280
        assert fitness.race_prediction_10k_seconds == 2_520
        assert fitness.personal_record_10k_seconds == 2_600
        assert fitness.configured_max_hr == 190
        assert fitness.heart_rate_zones is not None and len(fitness.heart_rate_zones) == 5
        assert fitness.power_zones is not None and len(fitness.power_zones) == 5
        assert session.scalar(select(GarminDevice)) is not None
        assert session.scalar(select(SyncEvent).where(SyncEvent.status == "success")) is not None
        assert user.onboarding_completed_at is not None
        assert user.onboarding_completed_version == 1


def test_performance_profile_refreshes_daily_and_not_on_every_sync(session_factory) -> None:
    client = DynamicPerformanceGarmin()
    pacer = GarminPacer(0)
    first_sync = datetime(2026, 8, 11, 8)
    with session_factory() as session:
        user = User(display_name="Dynamisch")
        session.add(user)
        session.flush()
        sync_performance_profile(session, client, user.id, pacer=pacer, now=first_sync)
        session.commit()
        ftp = session.scalar(
            select(DailyFitness).where(
                DailyFitness.user_id == user.id,
                DailyFitness.day == first_sync.date(),
            )
        )
        assert ftp is not None and ftp.cycling_ftp_watts == 280

        client.ftp = 300
        sync_performance_profile(
            session, client, user.id, pacer=pacer, now=first_sync + timedelta(hours=1)
        )
        session.commit()
        assert ftp.cycling_ftp_watts == 280

        sync_performance_profile(
            session, client, user.id, pacer=pacer, now=first_sync + timedelta(hours=25)
        )
        session.commit()
        session.expire_all()
        refreshed = session.scalar(
            select(DailyFitness).where(
                DailyFitness.user_id == user.id,
                DailyFitness.day == (first_sync + timedelta(hours=25)).date(),
            )
        )
        assert refreshed is not None and refreshed.cycling_ftp_watts == 300


def test_sync_releases_lock_when_initial_commit_fails(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    with session_factory() as session:
        user = User(display_name="Test")
        session.add(user)
        session.flush()
        account = GarminAccount(user_id=user.id, sync_status="connected")
        session.add(account)
        session.commit()

        def fail_commit() -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError):
            sync_module.sync_garmin(session, account)

    assert not sync_module.account_sync_active(account.id)


def test_sync_records_rate_limit_cooldown(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "garmin_call_delay_seconds", 0)
    monkeypatch.setattr(settings, "garmin_rate_limit_cooldown_seconds", 300)
    monkeypatch.setattr(
        sync_module, "connect_garmin_account", lambda _session, _account: RateLimitedGarmin()
    )
    sync_module.GarminPacer._global_cooldown_until = 0

    with session_factory() as session:
        user = User(display_name="Limited")
        session.add(user)
        session.flush()
        account = GarminAccount(
            user_id=user.id,
            connected_at=datetime.now(UTC).replace(tzinfo=None),
            sync_status="connected",
        )
        session.add(account)
        session.commit()

        run = sync_module.sync_garmin(session, account)
        session.expire_all()
        stored_account = session.get(GarminAccount, account.id)
        stored_run = session.get(SyncRun, run.id)
        stored_user = session.get(User, user.id)

        assert stored_account is not None and stored_account.sync_status == "rate_limited"
        assert stored_account.rate_limit_until is not None
        assert stored_run is not None and stored_run.status == "rate_limited"
        assert stored_run.stage == "cooldown"
        assert rate_limit_cooldown_remaining(session, stored_account) > 0
        assert stored_user is not None and stored_user.onboarding_completed_at is None


def test_wrapped_rate_limit_and_retry_after_are_detected() -> None:
    class Response:
        status_code = 429
        headers = {"Retry-After": "900"}

    class RateLimitError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("API Error 429")
            self.response = Response()

    source = RateLimitError()
    wrapped = GarminUnavailableError("Garmin Connect ist derzeit nicht erreichbar.")
    wrapped.__cause__ = source

    assert sync_module._is_rate_limited(wrapped)
    assert sync_module._rate_limit_cooldown_seconds(wrapped, 300) == 900


def test_sync_slots_are_isolated_per_account() -> None:
    with sync_module._sync_slot(101):
        assert sync_module.account_sync_active(101)
        with sync_module._sync_slot(202):
            assert sync_module.account_sync_active(202)
        with (
            pytest.raises(sync_module.SyncAlreadyRunningError),
            sync_module._sync_slot(101),
        ):
            pass

    assert not sync_module.account_sync_active(101)
    assert not sync_module.account_sync_active(202)
