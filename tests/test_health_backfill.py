from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from garminconnect.exceptions import GarminConnectNotFoundError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import DailyDataStatus, DailyFitness, DailyHealth, GarminSyncState, SleepStage, User
from app.services.garmin import client as client_module
from app.services.garmin import health_backfill as health_backfill_module
from app.services.garmin.health_backfill import (
    GarminPacer,
    HealthProgressEvent,
    HealthSyncResult,
    ResourceSyncStats,
    retry_after_seconds,
    sync_health_history,
)


class FakeHealthGarmin:
    def __init__(self, *, fail_hrv_once: date | None = None, unsupported_readiness: bool = False):
        self.health_start = date(2026, 1, 5)
        self.sleep_start = date(2026, 1, 6)
        self.vo2_start = date(2026, 1, 8)
        self.steps = 8_000
        self.fail_hrv_once = fail_hrv_once
        self.unsupported_readiness = unsupported_readiness
        self.empty_summary_days: set[date] = set()
        self.empty_body_battery_days: set[date] = set()
        self.calls: list[tuple[str, str]] = []
        self.body_battery_ranges: list[tuple[date, date]] = []

    def _day(self, resource: str, value: str) -> date:
        self.calls.append((resource, value))
        return date.fromisoformat(value)

    def get_user_summary(self, value: str) -> dict[str, Any]:
        day = self._day("daily_summary", value)
        if day < self.health_start or day in self.empty_summary_days:
            return {"calendarDate": value, "totalSteps": 0}
        return {
            "calendarDate": value,
            "totalSteps": self.steps,
            "totalDistanceMeters": 6_000.0,
            "totalKilocalories": 2_100,
            "activeKilocalories": 500,
            "restingHeartRate": 48,
            "minHeartRate": 42,
            "maxHeartRate": 172,
            "averageStressLevel": 24,
            "maxStressLevel": 70,
            "bodyBatteryHighestValue": 85,
            "bodyBatteryLowestValue": 20,
            "avgWakingRespirationValue": 14.2,
            "moderateIntensityMinutes": 12,
            "vigorousIntensityMinutes": 5,
        }

    def get_body_battery(self, start: str, end: str | None = None) -> list[dict[str, Any]]:
        first = self._day("body_battery", start)
        last = date.fromisoformat(end) if end else first
        self.body_battery_ranges.append((first, last))
        result = []
        day = first
        while day <= last:
            result.append(
                {
                    "date": day.isoformat(),
                    "charged": (
                        60
                        if day >= self.health_start and day not in self.empty_body_battery_days
                        else None
                    ),
                    "drained": (
                        55
                        if day >= self.health_start and day not in self.empty_body_battery_days
                        else None
                    ),
                    "bodyBatteryValuesArray": (
                        [[1, 20], [2, 80]]
                        if day >= self.health_start and day not in self.empty_body_battery_days
                        else [[1, None]]
                    ),
                }
            )
            day += timedelta(days=1)
        return result

    def get_sleep_data(self, value: str) -> dict[str, Any]:
        day = self._day("sleep", value)
        if day < self.sleep_start:
            return {"dailySleepDTO": {"sleepTimeSeconds": 0}}
        start = datetime(day.year, day.month, day.day) - timedelta(hours=1)
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27_000,
                "deepSleepSeconds": 3_600,
                "lightSleepSeconds": 18_000,
                "remSleepSeconds": 5_400,
                "awakeSleepSeconds": 600,
                "sleepStartTimestampGMT": int(start.timestamp() * 1000),
                "sleepEndTimestampGMT": int((start + timedelta(hours=7.5)).timestamp() * 1000),
                "sleepScores": {
                    "overall": {"value": 82},
                    "totalDuration": {"value": 80},
                    "deepPercentage": {"value": 75},
                },
                "sleepNeed": {"actual": 480, "baseline": 470},
                "nextSleepNeed": {"actual": 490},
                "avgHeartRate": 47,
                "avgSleepStress": 18,
                "averageRespirationValue": 13.8,
            },
            "bodyBatteryChange": 58,
            "sleepLevels": [
                {
                    "activityLevel": 1,
                    "startGMT": int(start.timestamp() * 1000),
                    "endGMT": int((start + timedelta(hours=1)).timestamp() * 1000),
                },
                {
                    "activityLevel": 0,
                    "startGMT": int((start + timedelta(hours=1)).timestamp() * 1000),
                    "endGMT": int((start + timedelta(hours=2)).timestamp() * 1000),
                },
                {
                    "activityLevel": 2,
                    "startGMT": int((start + timedelta(hours=2)).timestamp() * 1000),
                    "endGMT": int((start + timedelta(hours=3)).timestamp() * 1000),
                },
                {
                    "activityLevel": 3,
                    "startGMT": int((start + timedelta(hours=3)).timestamp() * 1000),
                    "endGMT": int((start + timedelta(hours=3, minutes=10)).timestamp() * 1000),
                },
            ],
        }

    def get_hrv_data(self, value: str) -> dict[str, Any] | None:
        day = self._day("hrv", value)
        if day == self.fail_hrv_once:
            self.fail_hrv_once = None
            raise RuntimeError("temporary HRV failure")
        if day < self.health_start:
            return None
        return {
            "hrvSummary": {
                "lastNightAvg": 52.0,
                "weeklyAvg": 50.0,
                "lastNight5MinHigh": 70.0,
                "status": "BALANCED",
                "baseline": {"lowUpper": 40, "balancedLow": 45, "balancedUpper": 60},
            }
        }

    def get_spo2_data(self, value: str) -> dict[str, Any]:
        self._day("spo2", value)
        return {"averageSpO2": None, "continuousReadingDTOList": []}

    def get_max_metrics(self, value: str) -> list[dict[str, Any]]:
        day = self._day("vo2max", value)
        return [{"vo2MaxPreciseValue": 54.2}] if day >= self.vo2_start else []

    def get_training_readiness(self, value: str) -> list[dict[str, Any]]:
        self._day("training_readiness", value)
        if self.unsupported_readiness:
            raise GarminConnectNotFoundError("API Error 404")
        return []

    def get_training_status(self, value: str) -> dict[str, Any]:
        self._day("training_status", value)
        return {
            "mostRecentTrainingLoadBalance": None,
            "mostRecentTrainingStatus": None,
        }


def _user(session: Session) -> User:
    user = User(display_name="Backfill")
    session.add(user)
    session.flush()
    return user


def test_health_backfill_is_idempotent_and_updates_recent_overlap(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeHealthGarmin()
    today = date(2026, 1, 10)
    with session_factory() as session:
        user = _user(session)
        first = sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=date(2026, 1, 1),
            overlap_days=3,
            delay=0,
        )
        health_count = session.scalar(select(func.count()).select_from(DailyHealth))
        stage_count = session.scalar(select(func.count()).select_from(SleepStage))
        status_count = session.scalar(select(func.count()).select_from(DailyDataStatus))

        assert first.resources["daily_summary"].earliest == date(2026, 1, 5)
        assert first.resources["sleep"].earliest == date(2026, 1, 6)
        assert first.resources["vo2max"].earliest == date(2026, 1, 8)
        assert first.resources["spo2"].populated_days == 0
        assert any((end - start).days > 0 for start, end in client.body_battery_ranges)
        assert health_count == 6
        assert stage_count == 20
        sample = session.scalar(select(DailyHealth).where(DailyHealth.day == today))
        assert sample is not None
        assert sample.sleep_need_seconds == 28_800
        assert sample.deep_sleep_seconds == 3_600
        assert sample.spo2_average is None
        assert sample.hrv_status == "BALANCED"
        assert [stage.stage for stage in sample.sleep_stages] == ["light", "deep", "rem", "awake"]
        fitness = session.scalar(select(DailyFitness).where(DailyFitness.day == today))
        assert fitness is not None
        assert fitness.vo2max == 54.2

        client.steps = 9_000
        second = sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=date(2026, 1, 1),
            overlap_days=3,
            delay=0,
        )

        assert second.api_calls < first.api_calls
        assert session.scalar(select(func.count()).select_from(DailyHealth)) == health_count
        assert session.scalar(select(func.count()).select_from(SleepStage)) == stage_count
        assert session.scalar(select(func.count()).select_from(DailyDataStatus)) == status_count
        updated = session.scalar(select(DailyHealth).where(DailyHealth.day == today))
        assert updated is not None
        assert updated.steps == 9_000


def test_health_backfill_does_not_refetch_past_empty_days(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeHealthGarmin()
    today = date(2026, 1, 10)
    empty_day = today - timedelta(days=1)
    client.empty_summary_days.add(empty_day)
    client.empty_body_battery_days.add(empty_day)

    with session_factory() as session:
        user = _user(session)
        sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=date(2026, 1, 1),
            overlap_days=3,
            delay=0,
        )
        empty_statuses = set(
            session.scalars(
                select(DailyDataStatus.resource).where(
                    DailyDataStatus.user_id == user.id,
                    DailyDataStatus.day == empty_day,
                    DailyDataStatus.status == "empty",
                )
            )
        )
        assert {"daily_summary", "body_battery"} <= empty_statuses

        client.calls.clear()
        client.body_battery_ranges.clear()
        sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=date(2026, 1, 1),
            overlap_days=3,
            delay=0,
        )

        assert ("daily_summary", empty_day.isoformat()) not in client.calls
        assert not any(start <= empty_day <= end for start, end in client.body_battery_ranges)
        assert ("daily_summary", today.isoformat()) in client.calls
        assert ("body_battery", today.isoformat()) in client.calls


def test_health_backfill_resumes_after_failure(
    session_factory: sessionmaker[Session],
) -> None:
    failure_day = date(2026, 1, 7)
    client = FakeHealthGarmin(fail_hrv_once=failure_day)
    with session_factory() as session:
        user = _user(session)
        with pytest.raises(RuntimeError, match="temporary HRV failure"):
            sync_health_history(
                session,
                client,
                user.id,
                today=date(2026, 1, 10),
                minimum=date(2026, 1, 1),
                overlap_days=2,
                delay=0,
            )

        failed = session.scalar(
            select(GarminSyncState).where(
                GarminSyncState.user_id == user.id,
                GarminSyncState.resource == "hrv",
            )
        )
        assert failed is not None
        assert failed.status == "error"
        assert failed.backfill_cursor_date == failure_day

        sync_health_history(
            session,
            client,
            user.id,
            today=date(2026, 1, 10),
            minimum=date(2026, 1, 1),
            overlap_days=2,
            delay=0,
        )
        resumed = session.get(GarminSyncState, failed.id)
        assert resumed is not None
        assert resumed.status == "ok"
        assert resumed.backfill_complete is True
        assert resumed.backfill_cursor_date is None
        assert resumed.oldest_synced_date == date(2026, 1, 5)
        assert resumed.newest_synced_date == date(2026, 1, 10)


def test_unsupported_metric_is_recorded_without_blocking_backfill(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeHealthGarmin(unsupported_readiness=True)
    with session_factory() as session:
        user = _user(session)
        result = sync_health_history(
            session,
            client,
            user.id,
            today=date(2026, 1, 10),
            minimum=date(2026, 1, 1),
            delay=0,
        )
        state = session.scalar(
            select(GarminSyncState).where(
                GarminSyncState.user_id == user.id,
                GarminSyncState.resource == "training_readiness",
            )
        )
        assert state is not None
        assert state.status == "unsupported"
        assert state.backfill_complete is True
        assert result.resources["training_status"].complete is True


def test_health_progress_reports_planning_and_prioritizes_recent_days(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeHealthGarmin()
    today = date(2026, 1, 10)
    events: list[HealthProgressEvent] = []

    with session_factory() as session:
        user = _user(session)
        sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=today - timedelta(days=1),
            delay=0,
            progress=events.append,
        )

    assert events[0].phase == "resource_planning_start"
    plan = next(event for event in events if event.phase == "plan")
    assert plan.planned is not None
    assert tuple(plan.planned) == (
        "daily_summary",
        "body_battery",
        "sleep",
        "hrv",
        "spo2",
        "vo2max",
        "training_readiness",
        "training_status",
    )
    assert plan.planned["daily_summary"] == (today - timedelta(days=1), today)

    day_events = [event for event in events if event.day is not None]
    for day in (today, today - timedelta(days=1)):
        current = [event for event in day_events if event.day == day]
        assert current[0].phase == "day_start"
        assert current[-1].phase == "day_complete"
        assert [event.resource for event in current if event.phase == "operation_complete"] == [
            "daily_summary",
            "body_battery",
            "sleep",
            "hrv",
            "vo2max",
        ]

    first_day_start = next(
        index for index, event in enumerate(events) if event.phase == "day_start"
    )
    assert all(
        event.phase
        in {
            "resource_planning_start",
            "resource_planning_complete",
            "plan",
            "resource_skipped",
        }
        for event in events[:first_day_start]
    )

    first_day_complete = next(
        index
        for index, event in enumerate(events)
        if event.phase == "day_complete" and event.day == today
    )
    second_day_start = next(
        index
        for index, event in enumerate(events)
        if event.phase == "day_start" and event.day == today - timedelta(days=1)
    )
    assert first_day_complete < second_day_start


def test_garmin_pacer_uses_strict_start_spacing_across_instances(monkeypatch: Any) -> None:
    clock = [100.0]
    starts: list[float] = []

    monkeypatch.setattr(health_backfill_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        health_backfill_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(health_backfill_module.random, "uniform", lambda _low, _high: 1.0)
    GarminPacer._global_next_call_at = 0
    GarminPacer._global_cooldown_until = 0

    first = GarminPacer(1)
    second = GarminPacer(1)
    first.call("first", lambda: starts.append(clock[0]))
    second.call("second", lambda: starts.append(clock[0]))

    assert starts == [100.0, 101.0]


def test_garmin_pacer_rechecks_cooldown_after_reserving_a_slot(monkeypatch: Any) -> None:
    clock = [100.0]
    starts: list[float] = []
    deferred = [False]

    monkeypatch.setattr(health_backfill_module.time, "monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if not deferred[0]:
            deferred[0] = True
            GarminPacer.defer_all(10)

    monkeypatch.setattr(health_backfill_module.time, "sleep", sleep)
    monkeypatch.setattr(health_backfill_module.random, "uniform", lambda _low, _high: 1.0)
    GarminPacer._global_next_call_at = 0
    GarminPacer._global_cooldown_until = 0

    pacer = GarminPacer(1)
    pacer.call("initial", lambda: starts.append(clock[0]))
    pacer.call("after cooldown", lambda: starts.append(clock[0]))

    assert starts == [100.0, 111.0]


def test_retry_after_http_date_is_honored() -> None:
    retry_at = datetime.now(UTC) + timedelta(minutes=15)

    assert retry_after_seconds(format_datetime(retry_at), 300) > 850


def test_reprobed_dense_resource_uses_historical_summary_anchor(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeHealthGarmin()
    today = date(2026, 1, 10)

    with session_factory() as session:
        user = _user(session)
        sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=date(2026, 1, 1),
            delay=0,
        )
        sleep_state = session.scalar(
            select(GarminSyncState).where(
                GarminSyncState.user_id == user.id,
                GarminSyncState.resource == "sleep",
            )
        )
        assert sleep_state is not None
        sleep_state.oldest_synced_date = None
        sleep_state.newest_synced_date = None
        sleep_state.backfill_cursor_date = None
        sleep_state.backfill_complete = True
        sleep_state.status = "empty"
        sleep_state.last_attempt_at = datetime(2025, 1, 1)
        session.commit()

        result = sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=date(2026, 1, 1),
            delay=0,
        )

        assert result.resources["sleep"].earliest == date(2026, 1, 6)


def test_unique_days_processed_uses_exact_completed_dates() -> None:
    first = date(2026, 1, 5)
    last = date(2026, 1, 7)
    stats = ResourceSyncStats(
        days_processed=2,
        earliest=first,
        latest=last,
        processed_days={first, last},
    )

    result = HealthSyncResult(resources={"daily_summary": stats})

    assert result.unique_days_processed == 2


def test_discovery_payloads_are_reused_during_execution(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeHealthGarmin()
    today = date(2026, 1, 10)

    with session_factory() as session:
        user = _user(session)
        sync_health_history(
            session,
            client,
            user.id,
            today=today,
            minimum=today,
            delay=0,
        )

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
        assert client.calls.count((resource, today.isoformat())) == 1


def test_account_token_directories_are_isolated(monkeypatch: Any, tmp_path: Path) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "garmin_token_dir", tmp_path)
    login_paths: list[Path] = []

    class FakeGarmin:
        def __init__(self, email: str | None, password: str | None) -> None:
            pass

        def login(self, path: str) -> None:
            login_paths.append(Path(path))

    monkeypatch.setattr(client_module, "Garmin", FakeGarmin)

    client_module.connect_garmin(account_id=1)
    client_module.connect_garmin(account_id=2)

    assert login_paths == [tmp_path / "account-1", tmp_path / "account-2"]
