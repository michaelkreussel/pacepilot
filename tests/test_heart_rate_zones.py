from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.models import GarminAccount, User
from app.services.garmin.health_backfill import GarminPacer
from app.services.garmin.heart_rate_zones import (
    HeartRateZoneSchemaError,
    normalize_heart_rate_zone_profiles,
    sync_heart_rate_zone_profiles,
)

ZONE_PAYLOAD = [
    {
        "sport": "DEFAULT",
        "trainingMethod": "HR_MAX",
        "zone1Floor": 105,
        "zone2Floor": 125,
        "zone3Floor": 144,
        "zone4Floor": 164,
        "zone5Floor": 185,
        "maxHeartRateUsed": 205,
        "restingHeartRateUsed": 67,
        "lactateThresholdHeartRateUsed": None,
    }
]


class FakeZoneGarmin:
    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        return ZONE_PAYLOAD


class InvalidZoneGarmin:
    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        return [{"sport": "DEFAULT"}]


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


class ForbiddenZoneGarmin:
    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        raise HttpError("forbidden", 403)


def test_normalizes_garmin_heart_rate_zone_profiles() -> None:
    assert normalize_heart_rate_zone_profiles(ZONE_PAYLOAD) == [
        {
            "sport": "DEFAULT",
            "training_method": "HR_MAX",
            "zone_floors": [105, 125, 144, 164, 185],
            "max_hr": 205,
            "resting_hr": 67,
            "lactate_threshold_hr": None,
        }
    ]


def test_rejects_incomplete_heart_rate_zone_profiles() -> None:
    payload = [{**ZONE_PAYLOAD[0]}]
    del payload[0]["zone5Floor"]

    with pytest.raises(HeartRateZoneSchemaError, match="boundaries missing"):
        normalize_heart_rate_zone_profiles(payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"zone2Floor": 105}, "not increasing"),
        (
            {"trainingMethod": "HR_RESERVE", "restingHeartRateUsed": None},
            "reserve inputs",
        ),
        ({"zone1Floor": 105.5}, "boundaries missing"),
    ],
)
def test_rejects_internally_invalid_heart_rate_zones(updates: dict[str, Any], message: str) -> None:
    payload = [{**ZONE_PAYLOAD[0], **updates}]

    with pytest.raises(HeartRateZoneSchemaError, match=message):
        normalize_heart_rate_zone_profiles(payload)


def test_sync_stores_profiles_without_overwriting_on_schema_error(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Zones")
        session.add(user)
        session.flush()
        account = GarminAccount(user_id=user.id)
        session.add(account)
        session.commit()

        result = sync_heart_rate_zone_profiles(session, FakeZoneGarmin(), account, GarminPacer(0))

        assert result.status == "ok"
        assert result.profiles == 1
        assert account.heart_rate_zone_profiles is not None
        synced_at = account.heart_rate_zones_synced_at
        assert isinstance(synced_at, datetime)

        invalid = sync_heart_rate_zone_profiles(
            session, InvalidZoneGarmin(), account, GarminPacer(0)
        )

        session.refresh(account)
        assert invalid.status == "schema_error"
        assert account.heart_rate_zone_profiles is not None
        assert account.heart_rate_zones_synced_at == synced_at


def test_sync_escalates_http_authentication_failures(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        user = User(display_name="Forbidden")
        session.add(user)
        session.flush()
        account = GarminAccount(user_id=user.id)
        session.add(account)
        session.commit()

        with pytest.raises(RuntimeError, match="forbidden"):
            sync_heart_rate_zone_profiles(session, ForbiddenZoneGarmin(), account, GarminPacer(0))
