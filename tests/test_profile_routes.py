import json
import re
from datetime import date, datetime

import pytest
from sqlalchemy import delete, select

from app.models import (
    Activity,
    ActivityZone,
    DailyFitness,
    DailyHealth,
    GarminAccount,
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
    assert "Analyse" in response.text
    assert "Deine Entwicklung" in response.text
    assert "Noch keine belastbare Einschätzung" in response.text
    assert "Trainingshistorie noch nicht belastbar" in response.text
    assert "Garmin Bereitschaft" not in response.text
    assert re.search(r'class="nav-item active[^"]*" href="/profile">Analyse', response.text)
    assert '<script id="profile-data"' not in response.text
    assert "chart.umd.min.js" not in response.text
    assert "Leistung & Schwellen" not in response.text


def test_profile_prefers_same_day_garmin_training_readiness(client, session_factory):
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        session.add(
            DailyFitness(
                user_id=user.id,
                day=date(2026, 6, 30),
                garmin_training_readiness_score=72,
                garmin_training_readiness_level="GOOD",
            )
        )
        session.commit()

    response = client.get("/profile?period=month&end=2026-06-30")

    assert response.status_code == 200
    assert "Garmin Training Readiness" in response.text
    assert "72 / 100" in response.text
    assert "PacePilot berechnet keinen zusätzlichen Ersatz-Score" in response.text
    assert "Datenvertrauen" not in response.text
    assert "So entsteht die Einschätzung" not in response.text


def test_profile_shows_only_available_device_performance_metrics(client, session_factory):
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        session.add_all(
            [
                DailyFitness(
                    user_id=user.id,
                    day=date(2026, 5, 1),
                    cycling_ftp_watts=275,
                ),
                DailyFitness(
                    user_id=user.id,
                    day=date(2026, 6, 20),
                    lactate_threshold_speed_mps=4.0,
                    lactate_threshold_hr=170,
                    running_ftp_watts=310,
                    endurance_score=5_100,
                    fitness_age=34.5,
                ),
                DailyFitness(
                    user_id=user.id,
                    day=date(2026, 6, 28),
                    race_prediction_5k_seconds=1_245,
                    race_prediction_10k_seconds=2_610,
                ),
            ]
        )
        session.commit()

    overview = client.get("/profile?period=month&end=2026-06-30")

    assert overview.status_code == 200
    assert "Leistung & Schwellen" in overview.text
    assert "Schwellenpace" in overview.text
    assert "4:10 min/km" in overview.text
    assert "Cycling FTP" in overview.text
    assert "275 W" in overview.text
    assert "5-km-Prognose" in overview.text
    assert "20:45 min" in overview.text
    assert "Garmin Anstiegswert" not in overview.text

    pace_detail = client.get("/profile/threshold-pace?period=month&end=2026-06-30")
    pace = _chart_payload(pace_detail.text)["charts"][0]
    assert pace["id"] == "threshold-pace-chart"
    assert pace["datasets"][0]["data"] == [250.0]
    assert pace["value_format"] == "pace"
    assert pace["reverse_y"] is True
    assert "Stand 20.06.2026" in pace_detail.text

    race_detail = client.get("/profile/race-predictions?period=month&end=2026-06-30")
    race = _chart_payload(race_detail.text)["charts"][0]
    assert race["value_format"] == "duration"
    assert [dataset["label"] for dataset in race["datasets"]] == ["5 km", "10 km"]

    old_detail = client.get("/profile/cycling-ftp?period=week&end=2026-06-30")
    assert _chart_payload(old_detail.text) == {"charts": []}
    assert "275 W" in old_detail.text
    assert "Kein Verlauf im gewählten Zeitraum" in old_detail.text


def test_profile_shows_garmin_heart_rate_zone_configuration(client, session_factory):
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        session.add(
            GarminAccount(
                user_id=user.id,
                heart_rate_zone_profiles=[
                    {
                        "sport": "DEFAULT",
                        "training_method": "HR_MAX",
                        "zone_floors": [105, 125, 144, 164, 185],
                        "max_hr": 205,
                        "resting_hr": 67,
                        "lactate_threshold_hr": None,
                    },
                    {
                        "sport": "RUNNING",
                        "training_method": "HR_RESERVE",
                        "zone_floors": [136, 150, 164, 177, 191],
                        "max_hr": 205,
                        "resting_hr": 67,
                        "lactate_threshold_hr": None,
                    },
                ],
                heart_rate_zones_synced_at=datetime(2026, 6, 30, 12),
            )
        )
        session.commit()

    overview = client.get("/profile?period=month&end=2026-06-30")

    assert overview.status_code == 200
    assert "Garmin HF-Zonen" in overview.text
    assert "2 Profile" in overview.text
    assert "Standard: Nach HFmax · 205 bpm" in overview.text
    assert 'href="/profile/garmin-heart-rate-zones?period=month&amp;end=2026-06-30"' in (
        overview.text
    )

    detail = client.get("/profile/garmin-heart-rate-zones?period=month&end=2026-06-30")

    assert detail.status_code == 200
    assert "Maximale Herzfrequenz" in detail.text
    assert "Wird für diese Zonen verwendet" in detail.text
    assert "Ruhepuls" in detail.text
    assert "67 bpm" in detail.text
    assert "Herzfrequenzreserve" in detail.text
    assert detail.text.count("Wird für diese Zonen verwendet") == 3
    assert "105–124 bpm" in detail.text
    assert "185–205 bpm" in detail.text
    assert "136–149 bpm" in detail.text
    assert "191–205 bpm" in detail.text
    assert "30.06.2026, 12:00" in detail.text


@pytest.mark.parametrize("period", ["day", "week", "month", "3m", "year"])
def test_profile_periods_render(client, period):
    response = client.get(f"/profile?period={period}&end=2026-06-30")

    assert response.status_code == 200
    assert f'href="/profile?period={period}&amp;end=2026-06-30"' in response.text


def test_profile_rejects_unknown_period_and_future_end(client):
    assert client.get("/profile?period=quarter").status_code == 422
    response = client.get("/profile?period=month&end=2099-01-01")
    assert response.status_code == 400
    assert response.json()["detail"] == "Das Enddatum darf nicht in der Zukunft liegen"
    assert client.get("/profile?period=year&end=0001-01-01").status_code == 400
    assert client.get("/profile/unknown-metric").status_code == 404


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

    response = client.get("/profile?period=year&end=2026-06-30")

    assert response.status_code == 200
    assert "PacePilot Readiness" in response.text
    assert "für Geräte ohne Garmin Training Readiness" in response.text
    assert "Formel 2.0" in response.text
    assert "So entsteht die Einschätzung" in response.text
    assert "Aktuelle Signale" in response.text
    assert "Ruhepuls" in response.text
    assert "HRV" in response.text
    assert "Schlaf" in response.text
    assert "VO2max" in response.text
    assert "Belastung & Routine" in response.text
    assert 'href="/profile/hrv?period=year&amp;end=2026-06-30"' in response.text
    assert 'href="/profile/running-volume?period=year&amp;end=2026-06-30"' in response.text
    assert "Progressiver Dauerlauf" not in response.text
    assert "Geheime Einheit" not in response.text
    assert "secret/raw" not in response.text
    assert "<canvas" not in response.text
    assert '<script id="profile-data"' not in response.text

    health_detail = client.get("/profile/hrv?period=year&end=2026-06-30")
    assert health_detail.status_code == 200
    assert 'id="hrv-chart"' in health_detail.text
    assert 'id="sleep-chart"' not in health_detail.text
    payload = _chart_payload(health_detail.text)
    assert len(payload["charts"]) == 1
    hrv = next(chart for chart in payload["charts"] if chart["id"] == "hrv-chart")
    assert len(hrv["labels"]) == 365
    assert hrv["datasets"][0]["data"].count(None) == 363
    assert hrv["span_gaps"] is True
    assert hrv["links"][-1] == "/profile/hrv?period=day&end=2026-06-30"
    assert hrv["summary_title"] == "Letzte verfügbare Werte"
    assert hrv["summary"][0] == {
        "label": "HRV",
        "value": 66.0,
        "unit": "ms",
        "context": "Stand 30.06.",
    }

    training_detail = client.get("/profile/running-volume?period=year&end=2026-06-30")
    assert training_detail.status_code == 200
    assert 'id="running-volume-chart"' in training_detail.text
    assert 'id="zone-chart"' not in training_detail.text
    payload = _chart_payload(training_detail.text)
    assert len(payload["charts"]) == 1
    training = next(chart for chart in payload["charts"] if chart["id"] == "running-volume-chart")
    assert "span_gaps" not in training
    assert training["links"][0] == "/activities?from=2025-07-01&to=2025-07-07"
    assert sum(value or 0 for value in training["datasets"][0]["data"]) == 10
    assert training["summary_title"] == "Zeitraum zusammengefasst"
    assert training["summary"][0] == {
        "label": "Laufumfang gesamt",
        "value": 10.0,
        "unit": "km",
        "context": "Gewählter Zeitraum",
    }
    assert training["summary"][0]["value"] != training["datasets"][0]["data"][-1]

    frequency_detail = client.get("/profile/workout-frequency?period=year&end=2026-06-30")
    frequency = _chart_payload(frequency_detail.text)["charts"][0]
    assert frequency["summary"][0]["label"] == "Einheiten gesamt"
    assert frequency["summary"][0]["value"] == 2
    assert frequency["summary"][1]["label"] == "Durchschnitt pro Woche"

    populated_index = next(
        index for index, value in enumerate(training["datasets"][0]["data"]) if value
    )
    drilldown = client.get(training["links"][populated_index])
    assert drilldown.status_code == 200
    assert "Progressiver Dauerlauf" in drilldown.text
    assert "Geheime Einheit" not in drilldown.text
    assert "secret/raw" not in drilldown.text


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

    response = client.get("/profile/training-effect?period=week&end=2026-06-30")

    assert response.status_code == 200
    assert 'id="training-effect-chart"' in response.text
    payload = _chart_payload(response.text)
    effect = next(chart for chart in payload["charts"] if chart["id"] == "training-effect-chart")
    assert [item["label"] for item in effect["datasets"]] == ["Anaerob"]
    assert effect["summary_title"] == "Durchschnitt im Zeitraum"
    assert effect["summary"] == [
        {
            "label": "Anaerober Training Effect",
            "value": 3.2,
            "unit": "/ 5",
            "context": "Ø aller Einheiten mit Messwert",
        }
    ]

    intensity_response = client.get("/profile/intensity?period=week&end=2026-06-30")
    intensity = _chart_payload(intensity_response.text)["charts"][0]
    assert intensity["datasets"][0]["data"] == [None, 12]
    assert intensity["datasets"][0]["colors"] == ["#1d5a48", "#6757a8"]

    day_response = client.get("/profile/training-effect?period=day&end=2026-06-30")
    assert day_response.status_code == 200
    assert "30.06.2026" in day_response.text
    day_chart_ids = {chart["id"] for chart in _chart_payload(day_response.text)["charts"]}
    assert day_chart_ids == {"training-effect-chart"}


def test_zone_analysis_combines_heart_rate_zones_across_activity_types(client, session_factory):
    client.get("/")
    with session_factory() as session:
        user = session.scalar(select(User).order_by(User.id))
        assert user is not None
        run = Activity(
            user_id=user.id,
            garmin_activity_id="zone-run",
            name="Lauf",
            activity_type="running",
            started_at=datetime(2026, 6, 30, 8),
        )
        run.zones = [
            ActivityZone(zone_type="heart_rate", zone_number=2, seconds=600),
            ActivityZone(zone_type="heart_rate", zone_number=3, seconds=300),
            ActivityZone(zone_type="power", zone_number=1, seconds=1_200),
        ]
        run.zones_complete = True
        strength = Activity(
            user_id=user.id,
            garmin_activity_id="zone-strength",
            name="Krafttraining",
            activity_type="strength_training",
            started_at=datetime(2026, 6, 29, 8),
        )
        strength.zones = [
            ActivityZone(zone_type="heart_rate", zone_number=2, seconds=900),
            ActivityZone(zone_type="heart_rate", zone_number=3, seconds=600),
        ]
        strength.zones_complete = True
        session.add_all([run, strength])
        session.commit()

    response = client.get("/profile/zones?period=week&end=2026-06-30")

    assert response.status_code == 200
    chart = _chart_payload(response.text)["charts"][0]
    assert chart["kicker"] == "Alle Aktivitäten"
    assert chart["labels"] == ["Zone 2", "Zone 3"]
    assert chart["datasets"][0]["data"] == [25.0, 15.0]
    assert chart["summary"][0]["value"] == 40.0
    assert [item["label"] for item in chart["summary"]] == [
        "Erfasste Zonenzeit",
        "Zone 2",
        "Zone 3",
    ]

    with session_factory() as session:
        session.execute(delete(ActivityZone).where(ActivityZone.zone_type == "heart_rate"))
        session.commit()

    power_response = client.get("/profile/zones?period=week&end=2026-06-30")
    power_chart = _chart_payload(power_response.text)["charts"][0]
    assert power_chart["title"] == "Leistungszonen"
    assert power_chart["labels"] == ["Zone 1"]
    assert power_chart["datasets"][0]["data"] == [20.0]


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
