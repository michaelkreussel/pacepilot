from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import DailyHealth, GarminAccount, Workout
from app.routes import plans as plans_module
from app.routes import settings as settings_module
from app.routes import workouts as workouts_module
from app.services.garmin import sync as sync_module


def _workout_data(
    name: str = "Lockerer Dauerlauf", scheduled_for: str = "2026-08-09"
) -> dict[str, str | list[str]]:
    return {
        "name": name,
        "sport": "running",
        "scheduled_for": scheduled_for,
        "description": "Ruhig bleiben",
        "step_type": ["warmup", "interval", "cooldown"],
        "duration_type": ["time", "time", "time"],
        "duration_value": ["10", "30", "5"],
        "repeat_count": ["1", "1", "1"],
        "target_type": ["no_target", "pace", "no_target"],
        "target_min": ["", "4:00", ""],
        "target_max": ["", "4:20", ""],
    }


def test_main_pages_render(client: TestClient) -> None:
    for path in (
        "/",
        "/profile",
        "/activities",
        "/plans",
        "/workouts/new",
        "/coach",
        "/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "PacePilot" in response.text

    assert client.get("/api/health").json() == {"status": "ok"}

    openapi = client.get("/openapi.json").json()
    assert "303" in openapi["paths"]["/workouts/{workout_id}/publish"]["post"]["responses"]
    assert "303" in openapi["paths"]["/settings/garmin/sync"]["post"]["responses"]


def test_training_plan_month_view_shows_calendar_weeks_and_workouts(
    client: TestClient, monkeypatch: Any
) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2027, 1, 15)

    monkeypatch.setattr(plans_module, "date", FixedDate)
    client.post(
        "/workouts",
        data=_workout_data(name="Silvesterlauf", scheduled_for="2026-12-31"),
    )
    client.post(
        "/workouts",
        data=_workout_data(name="Januarlauf", scheduled_for="2027-01-31"),
    )

    response = client.get("/plans?view=month")

    assert response.status_code == 200
    assert "Januar 2027" in response.text
    assert 'aria-label="Kalenderwoche 53"' in response.text
    assert 'aria-label="Kalenderwoche 4"' in response.text
    assert "Silvesterlauf" in response.text
    assert "Januarlauf" in response.text
    assert "/plans?view=month&amp;month=-1" in response.text
    assert "/plans?view=month&amp;month=1" in response.text


def test_training_plan_can_switch_between_week_and_month(client: TestClient) -> None:
    week = client.get("/plans")
    month = client.get("/plans?view=month")

    assert 'class="active" href="/plans?view=week">Woche</a>' in week.text
    assert "Diese Woche" in week.text
    assert 'class="active" href="/plans?view=month">Monat</a>' in month.text
    assert "Dieser Monat" in month.text
    assert 'const storageKey = "pacepilot-plan-url"' in month.text
    assert "window.location.replace(savedUrl)" in month.text


def test_sync_status_partial_renders(client: TestClient) -> None:
    response = client.get("/settings/sync-status")
    assert response.status_code == 200
    assert 'id="sync-progress"' in response.text


def test_health_refresh_fetches_current_steps(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    class FakeGarmin:
        def get_user_summary(self, _day: str) -> dict[str, int]:
            return {"totalSteps": 2559}

    client.get("/")
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        account.connected_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()

    monkeypatch.setattr(
        sync_module, "connect_garmin_account", lambda _session, _account: FakeGarmin()
    )

    response = client.post("/health")

    assert response.status_code == 200
    assert "2559" in response.text
    with session_factory() as session:
        health = session.scalar(select(DailyHealth).where(DailyHealth.day == date.today()))
        assert health is not None
        assert health.steps == 2559


def test_create_and_confirm_workout(client: TestClient) -> None:
    response = client.post(
        "/workouts",
        data=_workout_data(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]

    detail = client.get(location)
    assert detail.status_code == 200
    assert "Lockerer Dauerlauf" in detail.text
    assert "Pace 4:00 min/km bis 4:20 min/km" in detail.text
    assert "Entwurf bestätigen" in detail.text

    confirmed = client.post(f"{location}/confirm", follow_redirects=True)
    assert confirmed.status_code == 200
    assert "An Garmin übertragen" in confirmed.text


def test_invalid_workout_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/workouts",
        data={
            "name": "",
            "sport": "running",
            "step_type": "interval",
            "duration_type": "time",
            "duration_value": "0",
            "repeat_count": "1",
        },
    )
    assert response.status_code == 422
    assert "Bitte einen Namen angeben" in response.text


def test_non_finite_workout_duration_is_rejected(client: TestClient) -> None:
    data = _workout_data()
    data["duration_value"] = ["nan", "30", "5"]

    response = client.post("/workouts", data=data)

    assert response.status_code == 422
    assert "größer als null" in response.text


def test_edit_draft_workout(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    location = created.headers["location"]

    form = client.get(f"{location}/edit")
    assert form.status_code == 200
    assert "Einheit bearbeiten" in form.text
    assert 'value="Lockerer Dauerlauf"' in form.text
    assert 'x-data=\'workoutEditor([{"duration": "time"' in form.text
    assert '"targetMin": "4:00"' in form.text
    assert 'class="workout-step-heading"' in form.text
    assert 'class="workout-step-fields"' in form.text
    assert "/static/css/app.css?v=20260807-4" in form.text

    updated = client.post(
        location,
        data=_workout_data(name="Tempolauf bearbeitet", scheduled_for="2026-08-10"),
        follow_redirects=False,
    )

    assert updated.status_code == 303
    assert updated.headers["location"] == location
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        assert workout.name == "Tempolauf bearbeitet"
        assert workout.scheduled_for == date(2026, 8, 10)
        assert workout.status == "draft"
        assert workout.steps[1].target_min == 240
        assert workout.steps[1].target_max == 260


def test_edit_pushed_workout_updates_garmin_and_device(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    location = created.headers["location"]
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        workout.garmin_workout_id = "12345"
        workout.status = "pushed"
        session.commit()

    class FakeGarmin:
        updated: list[tuple[str, dict[str, Any]]] = []
        unscheduled: list[str] = []
        scheduled: list[tuple[str, str]] = []
        pushed: list[str] = []

        def update_workout(self, workout_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.updated.append((workout_id, payload))
            return {}

        def get_scheduled_workouts(self, _year: int, month: int) -> dict[str, object]:
            if month == 8:
                return {"items": [{"id": 987, "date": "2026-08-09", "workoutId": 12345}]}
            return {"items": []}

        def unschedule_workout(self, scheduled_id: str) -> None:
            self.unscheduled.append(scheduled_id)

        def schedule_workout(self, workout_id: str, day: str) -> dict[str, Any]:
            self.scheduled.append((workout_id, day))
            return {}

        def push_workout_to_device(self, workout_id: str) -> dict[str, Any]:
            self.pushed.append(workout_id)
            return {}

    garmin = FakeGarmin()
    connected_account_ids: list[int] = []

    def connect_test_account(_session: Session, account: GarminAccount) -> FakeGarmin:
        connected_account_ids.append(account.id)
        return garmin

    monkeypatch.setattr(workouts_module, "connect_garmin_account", connect_test_account)

    response = client.post(
        location,
        data=_workout_data(name="Garmin Update", scheduled_for="2026-08-10"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert garmin.updated[0][0] == "12345"
    assert garmin.updated[0][1]["workoutName"] == "Garmin Update"
    assert garmin.unscheduled == ["987"]
    assert garmin.scheduled == [("12345", "2026-08-10")]
    assert garmin.pushed == ["12345"]
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        account = session.scalar(
            select(GarminAccount).where(GarminAccount.user_id == workout.user_id)
        )
        assert account is not None
        assert connected_account_ids == [account.id]
        assert workout.name == "Garmin Update"
        assert workout.scheduled_for == date(2026, 8, 10)
        assert workout.status == "pushed"


def test_delete_pushed_workout_removes_garmin_workout(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    location = created.headers["location"]
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        workout.garmin_workout_id = "12345"
        workout.status = "pushed"
        session.commit()

    class FakeGarmin:
        unscheduled: list[str] = []
        deleted: list[str] = []

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {"items": [{"id": 987, "date": "2026-08-09", "workoutId": 12345}]}

        def unschedule_workout(self, scheduled_id: str) -> None:
            self.unscheduled.append(scheduled_id)

        def delete_workout(self, workout_id: str) -> None:
            self.deleted.append(workout_id)

    garmin = FakeGarmin()
    monkeypatch.setattr(
        workouts_module,
        "connect_garmin_account",
        lambda _session, _account: garmin,
    )

    response = client.post(f"{location}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/plans"
    assert garmin.unscheduled == ["987"]
    assert garmin.deleted == ["12345"]
    with session_factory() as session:
        assert session.scalar(select(Workout)) is None


def test_publish_retry_reuses_uploaded_garmin_workout(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    location = created.headers["location"]
    client.post(f"{location}/confirm")

    class FakeGarmin:
        uploads = 0
        schedule_attempts = 0
        fail_schedule = True

        def upload_workout(self, _payload: dict[str, Any]) -> dict[str, str]:
            self.uploads += 1
            return {"workoutId": "remote-123"}

        def get_scheduled_workouts(self, _year: int, _month: int) -> list[object]:
            return []

        def schedule_workout(self, _workout_id: str, _day: str) -> None:
            self.schedule_attempts += 1
            if self.fail_schedule:
                raise RuntimeError("temporary Garmin failure")

    garmin = FakeGarmin()
    monkeypatch.setattr(
        workouts_module,
        "connect_garmin_account",
        lambda _session, _account: garmin,
    )

    failed = client.post(f"{location}/publish", follow_redirects=False)

    assert failed.status_code == 303
    assert "error=" in failed.headers["location"]
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        assert workout.garmin_workout_id == "remote-123"
        assert workout.status == "published"

    garmin.fail_schedule = False
    retried = client.post(f"{location}/publish", follow_redirects=False)

    assert retried.status_code == 303
    assert garmin.uploads == 1
    assert garmin.schedule_attempts == 2


def test_draft_workout_with_remote_id_cannot_be_pushed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    location = created.headers["location"]
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        workout.garmin_workout_id = "remote-123"
        session.commit()

    def fail_connect(*_args: object) -> object:
        raise AssertionError("draft workout contacted Garmin")

    monkeypatch.setattr(workouts_module, "connect_garmin_account", fail_connect)

    response = client.post(f"{location}/push", follow_redirects=False)

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        assert workout.status == "draft"


def test_manual_sync_is_queued_once(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    client.get("/settings")
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        account.connected_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
        account_id = account.id

    queued: set[int] = set()

    def queue_once(queued_account_id: int, mark_queued: Callable[[], None]) -> bool:
        if queued_account_id in queued:
            return False
        mark_queued()
        queued.add(queued_account_id)
        return True

    monkeypatch.setattr(settings_module, "queue_account_sync", queue_once)

    first = client.post("/settings/garmin/sync", follow_redirects=False)
    second = client.post("/settings/garmin/sync", follow_redirects=False)

    assert first.status_code == second.status_code == 303
    assert queued == {account_id}
    with session_factory() as session:
        account = session.get(GarminAccount, account_id)
        assert account is not None
        assert account.sync_status == "queued"
