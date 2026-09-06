from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import app.routes.planning_inputs as planning_inputs_module
from app.models import GarminAccount
from app.models.user import utcnow
from app.services.garmin.personal_records import extract_running_records
from app.services.planning.planning_queries import (
    get_planning_profile,
    list_availability,
    list_goals,
    list_performance_anchors,
)


def _location_path(response: Any) -> str:
    return urlparse(str(response.headers["location"])).path


def _location_query(response: Any) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(response.headers["location"])).query)


def test_planning_inputs_page_renders_all_sections(
    client: TestClient,
) -> None:
    response = client.get("/planning-inputs")

    assert response.status_code == 200
    assert "Trainingsgrundlagen" in response.text
    assert "Leistungsanker" in response.text
    assert "Trainingsprofil" in response.text
    assert "Verfügbarkeiten" in response.text


def test_create_anchor(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    response = client.post(
        "/planning-inputs/anchors",
        data={
            "kind": "manual",
            "distance_km": "5",
            "minutes": "24",
            "seconds": "44",
            "achieved_on": "2026-09-06",
            "notes": "Training",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _location_path(response) == "/planning-inputs"
    with session_factory() as session:
        anchors = list_performance_anchors(session, 1)
        assert len(anchors) == 1
        assert anchors[0].kind == "manual"
        assert anchors[0].distance_m == 5000
        assert anchors[0].duration_s == 24 * 60 + 44
        assert anchors[0].achieved_on == date(2026, 9, 6)


def test_update_anchor_kind_from_race_to_manual(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.post(
        "/planning-inputs/anchors",
        data={
            "kind": "race",
            "distance_km": "5",
            "minutes": "24",
            "seconds": "44",
            "achieved_on": "2026-09-06",
        },
        follow_redirects=False,
    )
    with session_factory() as session:
        anchor_id = list_performance_anchors(session, 1)[0].id

    response = client.post(
        f"/planning-inputs/anchors/{anchor_id}",
        data={
            "kind": "manual",
            "distance_km": "5",
            "minutes": "24",
            "seconds": "44",
            "achieved_on": "2026-09-06",
            "reliable": "true",
            "notes": "Training, kein Wettkampf",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        anchor = list_performance_anchors(session, 1)[0]
        assert anchor.kind == "manual"
        assert anchor.notes == "Training, kein Wettkampf"


def test_create_anchor_with_future_date_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/planning-inputs/anchors",
        data={
            "kind": "manual",
            "distance_km": "5",
            "minutes": "24",
            "seconds": "44",
            "achieved_on": "2999-01-01",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error" in _location_query(response)
    with session_factory() as session:
        assert list_performance_anchors(session, 1) == ()


def test_deactivate_anchor(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    client.post(
        "/planning-inputs/anchors",
        data={
            "kind": "manual",
            "distance_km": "10",
            "minutes": "53",
            "seconds": "49",
            "achieved_on": "2026-08-06",
        },
        follow_redirects=False,
    )
    with session_factory() as session:
        anchor_id = list_performance_anchors(session, 1)[0].id

    response = client.post(
        f"/planning-inputs/anchors/{anchor_id}/deactivate", follow_redirects=False
    )

    assert response.status_code == 303
    with session_factory() as session:
        assert list_performance_anchors(session, 1)[0].reliable is False


def test_update_foreign_anchor_is_rejected(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    from app.models import PerformanceAnchor, User

    with session_factory() as session:
        other = User(display_name="Zweitathlet")
        session.add(other)
        session.flush()
        session.add(
            PerformanceAnchor(
                user_id=other.id,
                kind="race",
                distance_m=5000,
                duration_s=1500,
                achieved_on=date(2026, 9, 6),
            )
        )
        session.commit()
        foreign_id = session.scalar(
            select(PerformanceAnchor.id).where(PerformanceAnchor.user_id == other.id)
        )

    response = client.post(
        f"/planning-inputs/anchors/{foreign_id}",
        data={
            "kind": "manual",
            "distance_km": "5",
            "minutes": "25",
            "seconds": "0",
            "achieved_on": "2026-09-06",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error" in _location_query(response)


def test_create_update_and_archive_goal(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = client.post(
        "/planning-inputs/goals",
        data={
            "event_type": "half_marathon",
            "event_name": "Halbmarathon Regensburg",
            "target_date": "2027-04-15",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with session_factory() as session:
        goal = list_goals(session, 1)[0]
        assert goal.event_name == "Halbmarathon Regensburg"
        goal_id = goal.id

    updated = client.post(
        f"/planning-inputs/goals/{goal_id}",
        data={
            "event_type": "half_marathon",
            "event_name": "HM Regensburg",
            "target_date": "2027-04-15",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with session_factory() as session:
        assert list_goals(session, 1)[0].event_name == "HM Regensburg"

    archived = client.post(f"/planning-inputs/goals/{goal_id}/deactivate", follow_redirects=False)
    assert archived.status_code == 303
    with session_factory() as session:
        assert list_goals(session, 1)[0].status == "archived"


def test_update_profile(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    response = client.post(
        "/planning-inputs/profile",
        data={
            "experience_level": "intermediate",
            "preferred_long_run_weekday": "6",
            "self_declared_reentry": "true",
            "constraint_note": "60-90 Min pro Tag",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        profile = get_planning_profile(session, 1)
        assert profile is not None
        assert profile.experience_level == "intermediate"
        assert profile.preferred_long_run_weekday == 6
        assert profile.self_declared_reentry is True
        assert profile.constraint_note == "60-90 Min pro Tag"


def test_update_availability(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    response = client.post(
        "/planning-inputs/availability",
        data={"available_0": "on", "minutes_0": "60", "minutes_1": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        availability = {fact.weekday: fact for fact in list_availability(session, 1)}
        assert availability[0].available is True
        assert availability[0].available_minutes == 60
        assert 1 not in availability


def _record_payload(
    type_id: int,
    value: float,
    achieved: str,
    *,
    status: str = "ACCEPTED",
    name: str = "Amberg Laufen",
) -> dict[str, Any]:
    return {
        "id": type_id,
        "typeId": type_id,
        "status": status,
        "activityId": 22594890797,
        "activityName": name,
        "activityType": "running",
        "activityStartDateTimeLocalFormatted": achieved,
        "value": value,
    }


def _connect_garmin_account(
    session_factory: sessionmaker[Session], monkeypatch: Any, payload: list[dict[str, Any]]
) -> None:
    with session_factory() as session:
        account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == 1))
        if account is None:
            account = GarminAccount(user_id=1)
            session.add(account)
        account.email = "laeufer@example.com"
        account.connected_at = utcnow()
        session.commit()

    class FakeGarmin:
        def get_personal_record(self) -> list[dict[str, Any]]:
            return payload

    monkeypatch.setattr(
        planning_inputs_module, "connect_garmin_account", lambda _s, _a: FakeGarmin()
    )


def test_extract_running_records_ignores_non_anchor_records() -> None:
    candidates = extract_running_records(
        [
            _record_payload(3, 1483.8, "2026-05-08T13:34:31.0"),
            _record_payload(4, 3229.0, "2026-08-06T08:24:55.0"),
            _record_payload(3, 1500.0, "2026-05-08T13:34:31.0", status="PENDING"),
            _record_payload(7, 21197.0, "2026-08-23T08:30:11.0"),
            _record_payload(12, 29731.0, "2026-08-23T08:30:11.0"),
            _record_payload(5, 0.0, "2026-08-23T08:30:11.0"),
            _record_payload(99, 100.0, "2026-08-23T08:30:11.0"),
        ]
    )

    assert [(item.record_type_id, item.distance_m) for item in candidates] == [
        (3, 5000.0),
        (4, 10000.0),
    ]
    assert candidates[0].duration_s == 1484
    assert candidates[0].achieved_on == date(2026, 5, 8)
    assert candidates[0].activity_name == "Amberg Laufen"


def test_load_garmin_records_renders_candidates(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    _connect_garmin_account(
        session_factory,
        monkeypatch,
        [
            _record_payload(3, 1483.8, "2026-05-08T13:34:31.0"),
            _record_payload(4, 3229.0, "2026-08-06T08:24:55.0"),
        ],
    )

    response = client.post("/planning-inputs/garmin-records")

    assert response.status_code == 200
    assert "5 km" in response.text
    assert "10 km" in response.text
    assert "24:44" in response.text


def test_confirm_garmin_records_creates_anchors(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    _connect_garmin_account(
        session_factory,
        monkeypatch,
        [
            _record_payload(3, 1483.8, "2026-05-08T13:34:31.0"),
            _record_payload(4, 3229.0, "2026-08-06T08:24:55.0"),
        ],
    )

    response = client.post(
        "/planning-inputs/garmin-records/confirm",
        data={"record_3": "on", "kind_3": "manual", "kind_4": "race"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = _location_query(response)
    assert "notice" in query
    assert "1 Leistungsanker übernommen" in query["notice"][0]
    with session_factory() as session:
        anchors = list_performance_anchors(session, 1)
        assert len(anchors) == 1
        assert anchors[0].kind == "manual"
        assert anchors[0].distance_m == 5000
        assert anchors[0].duration_s == 1484
        assert anchors[0].achieved_on == date(2026, 5, 8)
        assert anchors[0].notes == "Garmin-Bestzeit · Amberg Laufen"


def test_confirm_garmin_records_skips_existing_anchors(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    client.post(
        "/planning-inputs/anchors",
        data={
            "kind": "manual",
            "distance_km": "5",
            "minutes": "24",
            "seconds": "44",
            "achieved_on": "2026-09-06",
        },
        follow_redirects=False,
    )
    _connect_garmin_account(
        session_factory,
        monkeypatch,
        [_record_payload(3, 1483.8, "2026-05-08T13:34:31.0")],
    )

    response = client.post(
        "/planning-inputs/garmin-records/confirm",
        data={"record_3": "on", "kind_3": "manual"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = _location_query(response)
    assert "bereits vorhanden" in query["notice"][0]
    with session_factory() as session:
        assert len(list_performance_anchors(session, 1)) == 1


def test_load_garmin_records_without_connection_is_rejected(
    client: TestClient,
) -> None:
    response = client.post("/planning-inputs/garmin-records", follow_redirects=False)

    assert response.status_code == 303
    query = _location_query(response)
    assert "error" in query
    assert "nicht verbunden" in query["error"][0]


def test_load_garmin_records_without_candidates_is_rejected(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    _connect_garmin_account(session_factory, monkeypatch, [])

    response = client.post("/planning-inputs/garmin-records", follow_redirects=False)

    assert response.status_code == 303
    assert "error" in _location_query(response)


def _anchor_id(client: TestClient, session_factory: sessionmaker[Session]) -> int:
    client.post(
        "/planning-inputs/anchors",
        data={
            "kind": "manual",
            "distance_km": "5",
            "minutes": "24",
            "seconds": "44",
            "achieved_on": "2026-09-06",
        },
        follow_redirects=False,
    )
    with session_factory() as session:
        return list_performance_anchors(session, 1)[0].id


def test_delete_anchor(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    anchor_id = _anchor_id(client, session_factory)

    response = client.post(
        f"/planning-inputs/anchors/{anchor_id}/delete",
        data={"confirm": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "notice" in _location_query(response)
    with session_factory() as session:
        assert list_performance_anchors(session, 1) == ()


def test_delete_anchor_requires_confirmation(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    anchor_id = _anchor_id(client, session_factory)

    response = client.post(f"/planning-inputs/anchors/{anchor_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "error" in _location_query(response)
    with session_factory() as session:
        assert len(list_performance_anchors(session, 1)) == 1


def test_delete_foreign_anchor_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    from app.models import PerformanceAnchor, User

    with session_factory() as session:
        other = User(display_name="Zweitathlet")
        session.add(other)
        session.flush()
        session.add(
            PerformanceAnchor(
                user_id=other.id,
                kind="race",
                distance_m=5000,
                duration_s=1500,
                achieved_on=date(2026, 9, 6),
            )
        )
        session.commit()
        foreign_id = session.scalar(
            select(PerformanceAnchor.id).where(PerformanceAnchor.user_id == other.id)
        )

    response = client.post(
        f"/planning-inputs/anchors/{foreign_id}/delete",
        data={"confirm": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error" in _location_query(response)


def _goal_id(client: TestClient, session_factory: sessionmaker[Session]) -> int:
    client.post(
        "/planning-inputs/goals",
        data={
            "event_type": "half_marathon",
            "event_name": "HM Regensburg",
            "target_date": "2027-04-15",
        },
        follow_redirects=False,
    )
    with session_factory() as session:
        return list_goals(session, 1)[0].id


def test_delete_goal(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    goal_id = _goal_id(client, session_factory)

    response = client.post(
        f"/planning-inputs/goals/{goal_id}/delete",
        data={"confirm": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "notice" in _location_query(response)
    with session_factory() as session:
        assert list_goals(session, 1) == ()


def test_delete_goal_requires_confirmation(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    goal_id = _goal_id(client, session_factory)

    response = client.post(f"/planning-inputs/goals/{goal_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "error" in _location_query(response)
    with session_factory() as session:
        assert len(list_goals(session, 1)) == 1


def test_delete_goal_referenced_by_cycle_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    from app.models import TrainingCycle

    goal_id = _goal_id(client, session_factory)
    with session_factory() as session:
        session.add(
            TrainingCycle(
                user_id=1,
                goal_id=goal_id,
                event_type="half_marathon",
                start_date=date(2027, 1, 4),
                target_date=date(2027, 4, 15),
            )
        )
        session.commit()

    response = client.post(
        f"/planning-inputs/goals/{goal_id}/delete",
        data={"confirm": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = _location_query(response)
    assert "error" in query
    assert "Mehrwochenplan" in query["error"][0]
    with session_factory() as session:
        assert len(list_goals(session, 1)) == 1
