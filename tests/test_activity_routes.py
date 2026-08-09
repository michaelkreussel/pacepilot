from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Activity, User
from app.services.garmin.activity_details import activity_details_path, write_activity_details


def test_activity_detail_renders_charts_and_map(
    client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="12345",
            name="Testlauf",
            activity_type="running",
            started_at=datetime(2026, 8, 7, 8, 0),
            distance_m=10000,
            duration_s=3300,
            details_complete=True,
        )
        session.add(activity)
        session.commit()
        activity_id = activity.id
        details_path = activity_details_path(
            activity.started_at, activity.garmin_activity_id, user.id
        )
        activity.details_file = str(details_path)
        session.commit()

    write_activity_details(
        details_path,
        {
            "metricDescriptors": [
                {"key": "sumElapsedDuration", "metricsIndex": 0},
                {"key": "directHeartRate", "metricsIndex": 1},
                {"key": "directSpeed", "metricsIndex": 2},
                {"key": "directDoubleCadence", "metricsIndex": 3},
                {"key": "sumMovingDuration", "metricsIndex": 4},
                {"key": "sumDuration", "metricsIndex": 5},
                {"key": "sumDistance", "metricsIndex": 6},
                {"key": "directPower", "metricsIndex": 7},
            ],
            "activityDetailMetrics": [
                {"metrics": [0, 140, 3.0, 172, 0, 0, 0, 260]},
                {"metrics": [3600, 152, 3.2, 176, 3300, 3320, 10000, 280]},
            ],
            "geoPolylineDTO": {
                "polyline": [
                    {"lat": 50.0, "lon": 9.0},
                    {"lat": 50.1, "lon": 9.1},
                ]
            },
        },
    )

    response = client.get(f"/activities/{activity_id}")

    assert response.status_code == 200
    assert "Herzfrequenz" in response.text
    assert "Schrittfrequenz" in response.text
    assert "Bewegungszeit" in response.text
    assert "Gesamtzeit" in response.text
    assert "Ø Leistung" in response.text
    assert '<details class="group ' in response.text
    assert "Garmin-Metriken" in response.text
    assert 'id="activity-map"' in response.text
    assert "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" in response.text
    assert str(details_path) not in response.text


def test_activity_detail_ignores_stale_file_when_enrichment_is_incomplete(
    client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="54321",
            name="Geänderter Lauf",
            activity_type="running",
            started_at=datetime(2026, 8, 7, 8, 0),
            duration_s=3300,
            details_complete=True,
            details_file=None,
        )
        session.add(activity)
        session.commit()
        activity_id = activity.id
        details_path = activity_details_path(
            activity.started_at, activity.garmin_activity_id, user.id
        )

    write_activity_details(
        details_path,
        {
            "metricDescriptors": [{"key": "sumElapsedDuration", "metricsIndex": 0}],
            "activityDetailMetrics": [{"metrics": [0]}, {"metrics": [60]}],
            "geoPolylineDTO": {"polyline": [{"lat": 50.0, "lon": 9.0}]},
        },
    )

    response = client.get(f"/activities/{activity_id}")

    assert response.status_code == 200
    assert 'id="activity-map"' not in response.text


def test_strength_activity_hides_distance(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="98765",
            name="Krafttraining",
            activity_type="strength_training",
            started_at=datetime(2026, 8, 7, 9, 0),
            distance_m=100,
            duration_s=2700,
            calories=320,
        )
        session.add(activity)
        session.commit()
        activity_id = activity.id

    response = client.get(f"/activities/{activity_id}")

    assert response.status_code == 200
    assert "Dauer" in response.text
    assert "Kalorien" in response.text
    assert "Distanz" not in response.text
    assert "Detailed Stats" not in response.text
