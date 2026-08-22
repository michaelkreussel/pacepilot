from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Activity, ActivityZone, User
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
        activity.zones = [
            ActivityZone(zone_type="heart_rate", zone_number=2, low_boundary=125, seconds=600)
        ]
        activity.zones_complete = True
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
    assert response.text.index("Detailed Stats") < response.text.index(
        "Herzfrequenzzonen dieser Aktivität"
    )
    assert 'id="activity-map"' in response.text
    assert '<div class="grid gap-4 md:grid-cols-2">' in response.text
    assert "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" in response.text
    assert str(details_path) not in response.text


def test_activity_detail_expands_a_single_chart(
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
            garmin_activity_id="67890",
            name="Herzfrequenztraining",
            activity_type="other",
            started_at=datetime(2026, 8, 8, 8, 0),
            duration_s=1800,
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
            ],
            "activityDetailMetrics": [
                {"metrics": [0, 120]},
                {"metrics": [1800, 145]},
            ],
        },
    )

    response = client.get(f"/activities/{activity_id}")

    assert response.status_code == 200
    assert 'id="heart-rate-chart"' in response.text
    assert '<div class="grid gap-4">' in response.text
    assert '<div class="grid gap-4 md:grid-cols-2">' not in response.text


def test_activity_detail_shows_zone_values_and_distribution(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="zones",
            name="Lauf mit Zonen",
            activity_type="running",
            started_at=datetime(2026, 8, 8, 8, 0),
            duration_s=1_200,
        )
        activity.zones = [
            ActivityZone(zone_type="heart_rate", zone_number=1, low_boundary=105, seconds=300),
            ActivityZone(zone_type="heart_rate", zone_number=2, low_boundary=125, seconds=900),
            ActivityZone(zone_type="power", zone_number=1, low_boundary=200, seconds=600),
            ActivityZone(zone_type="power", zone_number=2, low_boundary=250, seconds=600),
        ]
        activity.zones_complete = True
        session.add(activity)
        session.commit()
        activity_id = activity.id

    response = client.get(f"/activities/{activity_id}")

    assert response.status_code == 200
    assert "Herzfrequenzzonen dieser Aktivität" in response.text
    assert "105–124 bpm" in response.text
    assert "ab 125 bpm" in response.text
    assert "5:00 min" in response.text
    assert "15:00 min" in response.text
    assert "25,0 %" in response.text
    assert "75,0 %" in response.text
    assert "Leistungszonen" not in response.text
    assert "200–249 W" not in response.text
    assert 'class="zone-progress ' in response.text
    assert 'aria-label="Herzfrequenzzone 1: 25,0 Prozent"' in response.text


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


def test_activity_list_filters_and_paginates(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        start = datetime(2026, 6, 1, 8)
        session.add_all(
            [
                Activity(
                    user_id=user.id,
                    garmin_activity_id=f"run-{index}",
                    name=f"Lauf {index:02d}",
                    activity_type="running",
                    started_at=start + timedelta(days=index),
                )
                for index in range(26)
            ]
            + [
                Activity(
                    user_id=user.id,
                    garmin_activity_id=f"ride-{index}",
                    name=f"Radtour {index}",
                    activity_type="cycling",
                    started_at=start + timedelta(days=index),
                )
                for index in range(3)
            ]
            + [
                Activity(
                    user_id=user.id,
                    garmin_activity_id="run-outside",
                    name="Lauf außerhalb",
                    activity_type="running",
                    started_at=datetime(2026, 5, 1, 8),
                )
            ]
        )
        session.commit()

    response = client.get("/activities?from=2026-06-01&to=2026-06-30&sport=running&page=2")

    assert response.status_code == 200
    assert "26 Aktivitäten in der aktuellen Auswahl" in response.text
    assert "Lauf 00" in response.text
    assert "Lauf 25" not in response.text
    assert "Radtour" not in response.text
    assert '<option value="running" selected>Laufen</option>' in response.text
    assert "1–25 von 26" not in response.text
    assert "26–26 von 26" in response.text
    assert "sport=running" in response.text
    assert "page=1" in response.text
    category_only = client.get("/activities?from=&to=&sport=cycling")
    assert category_only.status_code == 200
    assert "Radtour 0" in category_only.text
    assert "Lauf 00" not in category_only.text
    assert client.get("/activities?page=0").status_code == 422
    assert client.get("/activities?page=3").status_code == 404
