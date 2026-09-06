from datetime import date
from typing import Any

import pytest
from garminconnect.exceptions import GarminConnectAuthenticationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import PerformanceAnchor, User
from app.services.garmin.personal_records import sync_personal_records


def _payload(*records: dict[str, Any]) -> list[dict[str, Any]]:
    return list(records)


def _record(
    type_id: int,
    value: float,
    achieved: str,
    *,
    name: str = "Amberg Laufen",
) -> dict[str, Any]:
    return {
        "id": type_id,
        "typeId": type_id,
        "status": "ACCEPTED",
        "activityId": 22594890797,
        "activityName": name,
        "activityType": "running",
        "activityStartDateTimeLocalFormatted": achieved,
        "value": value,
    }


class FakeGarmin:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def get_personal_record(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _user_id(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        user = User(display_name="Testathlet")
        session.add(user)
        session.commit()
        return user.id


def _anchors(session_factory: sessionmaker[Session], user_id: int) -> list[PerformanceAnchor]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(PerformanceAnchor).where(PerformanceAnchor.user_id == user_id)
            ).all()
        )


def test_sync_creates_garmin_anchors_for_new_distances(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)
    client = FakeGarmin(
        _payload(
            _record(3, 1483.8, "2026-05-08T13:34:31.0"),
            _record(4, 3229.0, "2026-08-06T08:24:55.0"),
        )
    )

    with session_factory() as session:
        result = sync_personal_records(session, client, user_id)

    assert result.status == "ok"
    assert (result.created, result.updated, result.unchanged) == (2, 0, 0)
    with session_factory() as session:
        anchors = _anchors(session_factory, user_id)
        assert [(anchor.source, anchor.kind) for anchor in anchors] == [
            ("garmin", "manual"),
            ("garmin", "manual"),
        ]
        five_k = next(anchor for anchor in anchors if anchor.distance_m == 5000)
        assert five_k.duration_s == 1484
        assert five_k.achieved_on == date(2026, 5, 8)
        assert five_k.notes == "Garmin-Bestzeit · Amberg Laufen"


def test_sync_skips_manually_captured_equivalent(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)
    with session_factory() as session:
        session.add(
            PerformanceAnchor(
                user_id=user_id,
                kind="manual",
                source="manual",
                distance_m=5000,
                duration_s=1484,
                achieved_on=date(2026, 9, 6),
            )
        )
        session.commit()
    client = FakeGarmin(_payload(_record(3, 1483.8, "2026-05-08T13:34:31.0")))

    with session_factory() as session:
        result = sync_personal_records(session, client, user_id)

    assert (result.created, result.updated, result.unchanged) == (0, 0, 1)
    with session_factory() as session:
        assert len(_anchors(session_factory, user_id)) == 1


def test_sync_updates_managed_row_on_improvement_and_preserves_overrides(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)
    with session_factory() as session:
        session.add(
            PerformanceAnchor(
                user_id=user_id,
                kind="race",
                source="garmin",
                distance_m=5000,
                duration_s=1500,
                achieved_on=date(2026, 5, 8),
                reliable=False,
            )
        )
        session.commit()
    client = FakeGarmin(_payload(_record(3, 1483.8, "2026-08-06T08:24:55.0", name="Neues Rennen")))

    with session_factory() as session:
        result = sync_personal_records(session, client, user_id)

    assert (result.created, result.updated, result.unchanged) == (0, 1, 0)
    with session_factory() as session:
        anchors = _anchors(session_factory, user_id)
        assert len(anchors) == 1
        assert anchors[0].duration_s == 1484
        assert anchors[0].achieved_on == date(2026, 8, 6)
        assert anchors[0].notes == "Garmin-Bestzeit · Neues Rennen"
        assert anchors[0].kind == "race"
        assert anchors[0].reliable is False


def test_sync_counts_identical_managed_rows_as_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)
    with session_factory() as session:
        session.add(
            PerformanceAnchor(
                user_id=user_id,
                kind="manual",
                source="garmin",
                distance_m=5000,
                duration_s=1484,
                achieved_on=date(2026, 5, 8),
            )
        )
        session.commit()
    client = FakeGarmin(_payload(_record(3, 1483.8, "2026-05-08T13:34:31.0")))

    with session_factory() as session:
        result = sync_personal_records(session, client, user_id)

    assert (result.created, result.updated, result.unchanged) == (0, 0, 1)


def test_sync_with_empty_payload_reports_empty(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)

    with session_factory() as session:
        result = sync_personal_records(session, FakeGarmin([]), user_id)

    assert result.status == "empty"
    with session_factory() as session:
        assert _anchors(session_factory, user_id) == []


def test_sync_without_record_method_reports_unsupported(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)

    with session_factory() as session:
        result = sync_personal_records(session, object(), user_id)

    assert result.status == "unsupported"


def test_sync_error_result_on_unexpected_failure(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)

    with session_factory() as session:
        result = sync_personal_records(session, FakeGarmin(RuntimeError("boom")), user_id)

    assert result.status == "error"
    assert result.error


def test_sync_reraises_authentication_errors(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _user_id(session_factory)

    with session_factory() as session, pytest.raises(GarminConnectAuthenticationError):
        sync_personal_records(
            session,
            FakeGarmin(GarminConnectAuthenticationError("expired")),
            user_id,
        )
