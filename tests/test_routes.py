import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Activity,
    DailyDataStatus,
    DailyFitness,
    DailyHealth,
    GarminAccount,
    GarminDevice,
    GarminSyncState,
    SyncEvent,
    SyncRun,
    User,
    Workout,
)
from app.routes import plans as plans_module
from app.routes import settings as settings_module
from app.routes import workouts as workouts_module
from app.services.garmin import sync as sync_module
from app.services.garmin.locks import garmin_account_slot


def _workout_data(
    name: str = "Lockerer Dauerlauf", scheduled_for: str = "2026-08-09"
) -> dict[str, str]:
    return {
        "name": name,
        "sport": "running",
        "scheduled_for": scheduled_for,
        "description": "Ruhig bleiben",
        "definition": json.dumps(
            {
                "blocks": [
                    {
                        "id": "warmup-1",
                        "kind": "step",
                        "step_type": "warmup",
                        "end": {"type": "time", "seconds": 600},
                        "target": {"type": "none"},
                    },
                    {
                        "id": "interval-1",
                        "kind": "step",
                        "step_type": "interval",
                        "end": {"type": "time", "seconds": 1800},
                        "target": {
                            "type": "pace_range",
                            "fastest_seconds_per_km": 240,
                            "slowest_seconds_per_km": 260,
                        },
                    },
                    {
                        "id": "cooldown-1",
                        "kind": "step",
                        "step_type": "cooldown",
                        "end": {"type": "time", "seconds": 300},
                        "target": {"type": "none"},
                    },
                ]
            }
        ),
    }


def _mark_garmin_connected(session: Session) -> GarminAccount:
    account = session.scalar(select(GarminAccount))
    if account is None:
        user = session.scalar(select(User))
        assert user is not None
        account = GarminAccount(user_id=user.id)
        session.add(account)
    account.connected_at = datetime.now(UTC).replace(tzinfo=None)
    session.commit()
    return account


def test_main_pages_render(client: TestClient) -> None:
    for path in (
        "/",
        "/profile",
        "/activities",
        "/plans",
        "/workouts/new",
        "/coach",
        "/settings",
        "/help",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "PacePilot" in response.text

    assert client.get("/api/health").json() == {"status": "ok"}
    assert "So entsteht deine Einheit" in client.get("/workouts/new").text
    assert "Diese Funktion befindet sich noch in Entwicklung" in client.get("/coach").text

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


def test_last_sync_is_marked_as_utc_for_browser_localization(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/")
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        account.connected_at = datetime(2026, 8, 9, 10, 0)
        account.last_sync_at = datetime(2026, 8, 9, 12, 34)
        session.commit()

    dashboard = client.get("/")
    settings = client.get("/settings")

    expected = 'data-local-datetime datetime="2026-08-09T12:34:00Z"'
    assert expected in dashboard.text
    assert expected in settings.text
    assert "/static/js/local-time.js" in dashboard.text


def test_sync_status_shows_authoritative_day_and_metric_progress(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/settings")
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        account.sync_status = "running"
        run = SyncRun(
            user_id=account.user_id,
            stage="health",
            message="Schlaf wird geladen",
            days_completed=2,
            days_total=3,
            operations_completed=9,
            operations_total=16,
            current_day=date(2026, 8, 8),
            current_operation="Schlaf",
        )
        session.add(run)
        session.flush()
        session.add(
            SyncEvent(
                sync_run_id=run.id,
                level="success",
                category="metric",
                status="success",
                resource="sleep",
                day=run.current_day,
                message="Schlaf: 1 Datensätze",
                duration_ms=430,
                record_count=1,
            )
        )
        session.commit()

    response = client.get("/settings/sync-status")

    assert response.status_code == 200
    assert "2 / 3" in response.text
    assert "9 / 16 Metriken" in response.text
    assert "Aktivitätsprotokoll" in response.text
    assert "430 ms" in response.text
    assert f"/settings/sync-runs/{run.id}/export" in response.text


def test_sync_export_contains_all_events_and_is_user_scoped(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/settings")
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        run = SyncRun(
            user_id=account.user_id,
            status="ok",
            stage="complete",
            activities_processed=12,
            activities_total=12,
            days_completed=3,
            days_total=3,
            operations_completed=24,
            operations_total=24,
        )
        session.add(run)
        session.flush()
        session.add_all(
            [
                SyncEvent(
                    sync_run_id=run.id,
                    level="success",
                    category="metric",
                    status="success",
                    resource="sleep",
                    day=date(2026, 8, 1),
                    operation=f"step-{position}",
                    message=f"Schritt {position}",
                    duration_ms=position,
                    record_count=1,
                )
                for position in range(65)
            ]
        )
        other_user = User(display_name="Andere Person")
        session.add(other_user)
        session.flush()
        other_run = SyncRun(user_id=other_user.id)
        session.add(other_run)
        session.commit()
        run_id = run.id
        other_run_id = other_run.id

    response = client.get(f"/settings/sync-runs/{run_id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="pacepilot-garmin-sync-{run_id}.json"'
    )
    payload = response.json()
    assert payload["sync_run"]["id"] == run_id
    assert payload["sync_run"]["operations_completed"] == 24
    assert len(payload["events"]) == 65
    assert [event["operation"] for event in payload["events"]] == [
        f"step-{position}" for position in range(65)
    ]
    assert client.get(f"/settings/sync-runs/{other_run_id}/export").status_code == 404


def test_garmin_connect_without_mfa_marks_account_connected(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    login_calls: list[tuple[str, str, int, int]] = []

    def start_login(email: str, password: str, *, account_id: int, user_id: int) -> None:
        login_calls.append((email, password, account_id, user_id))

    monkeypatch.setattr(settings_module, "start_garmin_login", start_login)

    response = client.post(
        "/settings/garmin/connect",
        data={"email": " runner@example.com ", "password": "secret"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        assert login_calls == [("runner@example.com", "secret", account.id, account.user_id)]
        assert account.email == "runner@example.com"
        assert account.connected_at is not None
        assert account.sync_status == "connected"


def test_garmin_connect_with_mfa_requests_and_verifies_code(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    challenge_id = "challenge-123"
    verification_calls: list[tuple[str | None, str, int, int]] = []
    monkeypatch.setattr(
        settings_module, "start_garmin_login", lambda *_args, **_kwargs: challenge_id
    )
    monkeypatch.setattr(
        settings_module,
        "pending_garmin_login",
        lambda candidate, **_kwargs: candidate == challenge_id,
    )

    def finish_login(candidate: str | None, code: str, *, account_id: int, user_id: int) -> str:
        verification_calls.append((candidate, code, account_id, user_id))
        return "runner@example.com"

    monkeypatch.setattr(settings_module, "finish_garmin_login", finish_login)

    started = client.post(
        "/settings/garmin/connect",
        data={"email": "runner@example.com", "password": "secret"},
        follow_redirects=False,
    )
    mfa_page = client.get("/settings")

    assert started.status_code == 303
    assert "Anmeldung bestätigen" in mfa_page.text
    assert 'autocomplete="one-time-code"' in mfa_page.text
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        assert account.email is None
        assert account.connected_at is None
        assert account.sync_status == "mfa_required"
        account_id = account.id
        user_id = account.user_id

    verified = client.post("/settings/garmin/mfa", data={"code": "123456"}, follow_redirects=False)

    assert verified.status_code == 303
    assert verification_calls == [(challenge_id, "123456", account_id, user_id)]
    with session_factory() as session:
        account = session.get(GarminAccount, account_id)
        assert account is not None
        assert account.email == "runner@example.com"
        assert account.connected_at is not None
        assert account.sync_status == "connected"


def test_garmin_disconnect_removes_tokens_but_keeps_imported_data(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "garmin_token_dir", tmp_path / "tokens")
    client.get("/settings")
    with session_factory() as session:
        account = _mark_garmin_connected(session)
        user_id = account.user_id
        session.add(
            Activity(
                user_id=user_id,
                garmin_activity_id="111",
                name="Importierter Lauf",
                activity_type="running",
                started_at=datetime(2026, 8, 1, 8),
            )
        )
        session.commit()
        token_directory = settings.garmin_token_dir / f"account-{account.id}"
        token_directory.mkdir(parents=True)
        (token_directory / "garmin_tokens.json").write_text("token", encoding="utf-8")

    response = client.post("/settings/garmin/disconnect", follow_redirects=False)

    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert not token_directory.exists()
    with session_factory() as session:
        account = session.scalar(select(GarminAccount))
        assert account is not None
        assert account.connected_at is None
        assert account.email is None
        assert account.sync_status == "not_connected"
        assert session.scalar(select(func.count()).select_from(Activity)) == 1


def test_delete_garmin_data_preserves_connection_user_and_local_workout(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "garmin_token_dir", tmp_path / "tokens")
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    assert created.status_code == 303

    with session_factory() as session:
        account = _mark_garmin_connected(session)
        account.email = "runner@example.com"
        user_id = account.user_id
        workout = session.scalar(select(Workout))
        assert workout is not None
        workout.garmin_workout_id = "remote-123"
        workout.status = "pushed"
        activity = Activity(
            user_id=user_id,
            garmin_activity_id="222",
            name="Garmin-Lauf",
            activity_type="running",
            started_at=datetime(2026, 8, 2, 8),
            workout_id=workout.id,
        )
        session.add_all(
            [
                activity,
                DailyHealth(user_id=user_id, day=date(2026, 8, 2), steps=8000),
                DailyFitness(user_id=user_id, day=date(2026, 8, 2), vo2max=50),
                GarminSyncState(user_id=user_id, resource="activities"),
                DailyDataStatus(
                    user_id=user_id,
                    day=date(2026, 8, 2),
                    resource="sleep",
                    status="complete",
                ),
                GarminDevice(
                    account_id=account.id,
                    garmin_device_id="device-1",
                    name="Forerunner",
                ),
            ]
        )
        run = SyncRun(user_id=user_id, status="ok")
        session.add(run)
        session.flush()
        session.add(
            SyncEvent(
                sync_run_id=run.id,
                message="Test",
            )
        )
        session.commit()
        account_id = account.id
        connected_at = account.connected_at

    activity_directory = settings.data_dir / "raw" / "activities" / f"user-{user_id}"
    activity_directory.mkdir(parents=True)
    (activity_directory / "orphan.json.gz").write_text("raw", encoding="utf-8")
    token_directory = settings.garmin_token_dir / f"account-{account_id}"
    token_directory.mkdir(parents=True)
    (token_directory / "garmin_tokens.json").write_text("token", encoding="utf-8")

    response = client.post(
        "/settings/garmin/data/delete",
        data={"confirmation": "delete"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert not activity_directory.exists()
    assert token_directory.exists()
    assert (token_directory / "garmin_tokens.json").read_text(encoding="utf-8") == "token"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(Activity)) == 0
        assert session.scalar(select(func.count()).select_from(DailyHealth)) == 0
        assert session.scalar(select(func.count()).select_from(DailyFitness)) == 0
        assert session.scalar(select(func.count()).select_from(GarminSyncState)) == 0
        assert session.scalar(select(func.count()).select_from(DailyDataStatus)) == 0
        assert session.scalar(select(func.count()).select_from(GarminDevice)) == 0
        assert session.scalar(select(func.count()).select_from(SyncRun)) == 0
        assert session.scalar(select(func.count()).select_from(SyncEvent)) == 0
        workout = session.scalar(select(Workout))
        assert workout is not None
        assert workout.step_count == 3
        assert workout.garmin_workout_id is None
        assert workout.status == "confirmed"
        account = session.scalar(select(GarminAccount))
        assert account is not None
        assert account.id == account_id
        assert account.email == "runner@example.com"
        assert account.connected_at == connected_at
        assert account.sync_status == "connected"


def test_garmin_account_actions_are_in_connection_card(client: TestClient) -> None:
    response = client.get("/settings")

    start = response.text.index('class="card connection-card garmin-connection-card"')
    end = response.text.index("</article>", start)
    connection_card = response.text[start:end]
    assert 'action="/settings/garmin/disconnect"' in connection_card
    assert 'action="/settings/garmin/data/delete"' in connection_card
    assert "Importierte Daten löschen" in connection_card


def test_delete_garmin_data_is_blocked_during_account_operation(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/settings")
    with session_factory() as session:
        account = _mark_garmin_connected(session)
        account_id = account.id

    with garmin_account_slot(account_id):
        response = client.post(
            "/settings/garmin/data/delete",
            data={"confirmation": "delete"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    with session_factory() as session:
        account = session.get(GarminAccount, account_id)
        assert account is not None
        assert account.connected_at is not None


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
            "definition": _workout_data()["definition"],
        },
    )
    assert response.status_code == 422
    assert "Bitte einen Namen angeben" in response.text


def test_non_finite_workout_duration_is_rejected(client: TestClient) -> None:
    data = _workout_data()
    definition = json.loads(data["definition"])
    definition["blocks"][0]["end"]["seconds"] = float("nan")
    data["definition"] = json.dumps(definition)

    response = client.post("/workouts", data=data)

    assert response.status_code == 422
    assert "größer als null" in response.text


def test_workout_supports_many_steps(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    data = _workout_data(name="Langer Ablauf")
    definition = json.loads(data["definition"])
    definition["blocks"] = [
        {
            "id": f"step-{index}",
            "kind": "step",
            "step_type": "interval",
            "end": {"type": "time", "seconds": 60 + index},
            "target": {"type": "none"},
        }
        for index in range(25)
    ]
    data["definition"] = json.dumps(definition)

    response = client.post("/workouts", data=data, follow_redirects=False)

    assert response.status_code == 303
    with session_factory() as session:
        workout = session.scalar(select(Workout))
        assert workout is not None
        assert workout.step_count == 25
        assert [block.end.seconds for block in workout.definition_model.blocks] == [
            60 + index for index in range(25)
        ]


def test_validation_error_preserves_submitted_builder(
    client: TestClient,
) -> None:
    data = _workout_data(name="Mein fehlerhafter Ablauf")
    definition = json.loads(data["definition"])
    definition["blocks"][1]["target"]["fastest_seconds_per_km"] = 300
    definition["blocks"][1]["target"]["slowest_seconds_per_km"] = 280
    data["definition"] = json.dumps(definition)

    response = client.post("/workouts", data=data)

    assert response.status_code == 422
    assert "Mein fehlerhafter Ablauf" in response.text
    assert '"id": "interval-1"' in response.text
    assert "schnelle Pace-Grenze" in response.text


def test_edit_draft_workout(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    created = client.post("/workouts", data=_workout_data(), follow_redirects=False)
    location = created.headers["location"]

    form = client.get(f"{location}/edit")
    assert form.status_code == 200
    assert "Einheit bearbeiten" in form.text
    assert 'value="Lockerer Dauerlauf"' in form.text
    assert "workoutBuilder" in form.text
    assert '"fastest_seconds_per_km": 240.0' in form.text
    assert 'class="builder-workspace"' in form.text
    assert 'class="repeat-content"' in form.text
    assert 'draggable="true"' in form.text
    assert "startPaletteDrag('interval'" in form.text
    assert "/static/icons/workout.svg#pencil" in form.text
    assert "setDropTarget(null, index)" in form.text
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
        interval = workout.definition_model.blocks[1]
        assert interval.target.fastest_seconds_per_km == 240
        assert interval.target.slowest_seconds_per_km == 260


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
        _mark_garmin_connected(session)

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
        _mark_garmin_connected(session)

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
    with session_factory() as session:
        _mark_garmin_connected(session)

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

    def queue_once(queued_account_id: int, mark_queued: Callable[[], bool]) -> bool:
        if queued_account_id in queued:
            return False
        assert mark_queued()
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
