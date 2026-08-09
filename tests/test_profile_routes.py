import json
import re
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models import (
    Activity,
    ActivityZone,
    DailyFitness,
    DailyHealth,
    GarminSyncState,
    User,
)


def _chart_payload(html: str) -> dict:
    match = re.search(
        r'<script id="profile-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    return json.loads(match.group(1))


def test_empty_profile_renders_without_zeroing_missing_metrics(client):
    response = client.get("/profile?period=month&end=2026-06-30")

    assert response.status_code == 200
    assert "Athletenprofil" in response.text
    assert "Noch keine belastbare Einschätzung" in response.text
    assert "Trainingshistorie noch nicht belastbar" in response.text
    assert "Garmin Bereitschaft</span>\n        <strong>0" not in response.text
    assert re.search(r'class="nav-item active[^"]*" href="/profile"', response.text)
    assert _chart_payload(response.text) == {"charts": []}


@pytest.mark.parametrize("period", ["day", "week", "month", "3m", "year"])
def test_profile_periods_render(client, period):
    response = client.get(f"/profile?period={period}&end=2026-06-30")

    assert response.status_code == 200
    assert f'href="/profile?period={period}&end=2026-06-30"' in response.text


def test_profile_rejects_unknown_period_and_future_end(client):
    assert client.get("/profile?period=quarter").status_code == 422
    response = client.get("/profile?period=month&end=2099-01-01")
    assert response.status_code == 400
    assert response.json()["detail"] == "Das Enddatum darf nicht in der Zukunft liegen"
    assert client.get("/profile?period=year&end=0001-01-01").status_code == 400


def test_profile_renders_real_metrics_gaps_charts_and_user_scoped_drilldown(
    client, session_factory
):
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        other = User(display_name="Verborgener Athlet")
        session.add(other)
        session.flush()
        session.add_all(
            [
                DailyHealth(
                    user_id=user.id,
                    day=date(2026, 6, 23),
                    resting_hr=52,
                    hrv_average=60,
                    sleep_seconds=25_200,
                ),
                DailyHealth(
                    user_id=user.id,
                    day=date(2026, 6, 30),
                    resting_hr=50,
                    hrv_average=66,
                    hrv_status="BALANCED",
                    hrv_baseline_balanced_low=56,
                    hrv_baseline_balanced_high=68,
                    sleep_seconds=27_000,
                    sleep_need_seconds=28_800,
                    sleep_score=84,
                    stress_average=28,
                    body_battery_high=82,
                    body_battery_low=24,
                ),
                DailyFitness(
                    user_id=user.id,
                    day=date(2026, 6, 30),
                    vo2max=52.1,
                    acute_load=330,
                    load_ratio=1.05,
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=date(2025, 7, 1),
                    newest_synced_date=date(2026, 6, 29),
                ),
                GarminSyncState(
                    user_id=user.id,
                    resource="hrv",
                    status="ok",
                    backfill_complete=True,
                    oldest_synced_date=date(2026, 6, 23),
                    newest_synced_date=date(2026, 6, 30),
                ),
            ]
        )
        run = Activity(
            user_id=user.id,
            garmin_activity_id="profile-run",
            name="Progressiver Dauerlauf",
            activity_type="running",
            started_at=datetime(2026, 6, 29, 18),
            duration_s=3_600,
            distance_m=10_000,
            elevation_gain_m=120,
            average_hr=148,
            aerobic_training_effect=4.1,
            anaerobic_training_effect=0.8,
            workout_rpe=7,
            moderate_intensity_minutes=18,
            vigorous_intensity_minutes=22,
            raw_file="secret/raw/profile-run.json.gz",
        )
        run.zones = [ActivityZone(zone_type="heart_rate", zone_number=3, seconds=1_500)]
        old_sparse = Activity(
            user_id=user.id,
            garmin_activity_id="old-sparse",
            name="Alte Einheit mit wenigen Daten",
            activity_type="strength_training",
            started_at=datetime(2025, 7, 1, 8),
            duration_s=None,
            distance_m=None,
        )
        very_old = Activity(
            user_id=user.id,
            garmin_activity_id="very-old",
            name="Mehrjährige Historie",
            activity_type="running",
            started_at=datetime(2023, 1, 1, 8),
            duration_s=20_000,
            distance_m=500_000,
        )
        hidden = Activity(
            user_id=other.id,
            garmin_activity_id="hidden",
            name="Geheime Einheit",
            activity_type="running",
            started_at=datetime(2026, 6, 30),
            duration_s=99_999,
            distance_m=99_999,
        )
        session.add_all([run, old_sparse, very_old, hidden])
        session.commit()
        run_id = run.id

    response = client.get("/profile?period=year&end=2026-06-30")

    assert response.status_code == 200
    assert "PacePilot Readiness" in response.text
    assert "Kein Garmin-Score" in response.text
    assert "Stand 30.06.2026 · PacePilot Readiness" in response.text
    assert "Historischer Zustand" in response.text
    assert "Ruhepuls" in response.text
    assert "HRV" in response.text
    assert "Schlaf" in response.text
    assert "VO2max" in response.text
    assert "Progressiver Dauerlauf" in response.text
    assert f'href="/activities/{run_id}"' in response.text
    assert "Geheime Einheit" not in response.text
    assert "secret/raw" not in response.text
    assert 'id="hrv-chart"' in response.text
    assert 'id="sleep-chart"' in response.text
    assert 'id="running-volume-chart"' in response.text
    assert 'id="zone-chart"' in response.text
    assert 'id="garmin-training-load-chart"' in response.text
    assert 'id="exercise-load-chart"' not in response.text

    payload = _chart_payload(response.text)
    hrv = next(chart for chart in payload["charts"] if chart["id"] == "hrv-chart")
    assert len(hrv["labels"]) == 365
    assert hrv["datasets"][0]["data"].count(None) == 363
    training = next(chart for chart in payload["charts"] if chart["id"] == "running-volume-chart")
    assert training["links"][0] == "/activities?from=2025-07-01&to=2025-07-07"
    assert sum(value or 0 for value in training["datasets"][0]["data"]) == 10


def test_profile_preserves_one_sided_intensity_and_anaerobic_effect(client, session_factory):
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        session.add_all(
            [
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                ),
                Activity(
                    user_id=user.id,
                    garmin_activity_id="anaerobic",
                    name="Anaerobe Einheit",
                    activity_type="strength_training",
                    started_at=datetime(2026, 6, 30),
                    duration_s=1_800,
                    anaerobic_training_effect=3.2,
                    vigorous_intensity_minutes=12,
                ),
            ]
        )
        session.commit()

    response = client.get("/profile?period=week&end=2026-06-30")

    assert response.status_code == 200
    assert 'id="training-effect-chart"' in response.text
    payload = _chart_payload(response.text)
    intensity = next(chart for chart in payload["charts"] if chart["id"] == "intensity-chart")
    assert intensity["datasets"][0]["data"] == [None, 12]
    effect = next(chart for chart in payload["charts"] if chart["id"] == "training-effect-chart")
    assert [item["label"] for item in effect["datasets"]] == ["Anaerob"]


def test_activity_range_drilldown_filters_results(client, session_factory):
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        session.add_all(
            [
                Activity(
                    user_id=user.id,
                    garmin_activity_id="inside",
                    name="Im Zeitraum",
                    activity_type="running",
                    started_at=datetime(2026, 6, 15),
                ),
                Activity(
                    user_id=user.id,
                    garmin_activity_id="outside",
                    name="Außerhalb Zeitraum",
                    activity_type="running",
                    started_at=datetime(2026, 6, 22),
                ),
            ]
        )
        session.commit()

    response = client.get("/activities?from=2026-06-14&to=2026-06-20")

    assert response.status_code == 200
    assert "Im Zeitraum" in response.text
    assert "Außerhalb Zeitraum" not in response.text
    assert "Filter zurücksetzen" in response.text
