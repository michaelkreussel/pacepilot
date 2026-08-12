from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import (
    Activity,
    AthleteAvailability,
    AthleteGoal,
    AthleteManualAnchor,
    AthleteProfile,
    DailyFitness,
    DailyHealth,
    GarminSyncState,
    User,
)
from app.services.analytics import AthleteDataService


def test_planning_context_combines_manual_garmin_and_training_data(session_factory):
    as_of = date(2026, 8, 11)
    with session_factory() as session:
        user = User(display_name="Planung")
        other = User(display_name="Andere Person")
        session.add_all([user, other])
        session.flush()
        session.add_all(
            [
                AthleteProfile(
                    user_id=user.id,
                    primary_sport="running",
                    experience_level="intermediate",
                    experience_years=4,
                ),
                AthleteGoal(
                    user_id=user.id,
                    sport="running",
                    event_name="Herbstlauf",
                    target_date=date(2026, 10, 4),
                    distance_m=10_000,
                    target_duration_s=2_400,
                ),
                AthleteAvailability(user_id=user.id, weekday=1, max_duration_minutes=60),
                AthleteManualAnchor(
                    user_id=user.id,
                    sport="running",
                    metric="threshold_hr",
                    value=172,
                    observed_on=date(2026, 8, 1),
                    method="field_test",
                ),
                AthleteManualAnchor(
                    user_id=other.id,
                    sport="running",
                    metric="max_hr",
                    value=240,
                    observed_on=as_of,
                ),
                DailyFitness(
                    user_id=user.id,
                    day=as_of,
                    vo2max=52.4,
                    lactate_threshold_hr=169,
                    lactate_threshold_speed_mps=4,
                    race_prediction_10k_seconds=2_350,
                    configured_max_hr=190,
                    heart_rate_zones=[
                        {
                            "sport": "running",
                            "zone": 1,
                            "lower": 95,
                            "upper": 113,
                        }
                    ],
                ),
                DailyHealth(user_id=user.id, day=as_of, resting_hr=49),
                GarminSyncState(
                    user_id=user.id,
                    resource="activities",
                    status="ok",
                    backfill_complete=True,
                ),
                Activity(
                    user_id=user.id,
                    garmin_activity_id="planning-run",
                    name="Dauerlauf",
                    activity_type="running",
                    started_at=datetime(2026, 8, 9, 9),
                    distance_m=12_000,
                    duration_s=4_000,
                ),
            ]
        )
        session.commit()

        context = AthleteDataService(session, user.id, as_of=as_of).get_planning_context()

    assert context.schema_version == "planning-context.v2"
    assert context.planning_limits.schema_version == "running-planning-limits.v1"
    assert context.planning_limits.sport == "running"
    assert context.goal is not None
    assert context.goal.target_pace_s_per_km == 240
    assert context.availability[0].max_duration_minutes == 60
    assert context.training_capacity.running_distance_28d_m == 12_000
    assert context.training_capacity.history_complete is True
    assert context.zones[0].lower_boundary == 95
    threshold_hr = next(item for item in context.performance if item.key == "threshold_hr")
    assert threshold_hr.value == 172
    assert threshold_hr.source == "athlete"
    assert all(item.value != 240 for item in context.performance)
    assert {item.key for item in context.performance} >= {
        "resting_hr_baseline",
        "threshold_pace_s_per_km",
        "vo2max",
        "prediction_10k_seconds",
    }
    prediction = next(item for item in context.performance if item.key == "prediction_10k_seconds")
    assert prediction.source == "garmin"


def test_profile_edit_saves_and_renders_planning_profile(client, session_factory):
    today = date.today()
    target = today + timedelta(days=90)
    response = client.get("/performance/edit")
    assert response.status_code == 200
    assert "Leistungsprofil bearbeiten" in response.text

    response = client.post(
        "/performance",
        data={
            "primary_sport": "running",
            "experience_level": "intermediate",
            "experience_years": "5",
            "goal_enabled": "1",
            "goal_sport": "running",
            "goal_event_name": "Stadtlauf",
            "goal_target_date": target.isoformat(),
            "goal_distance_km": "10",
            "goal_target_time": "42:00",
            "available_1": "1",
            "duration_1": "75",
            "max_hr": "190",
            "threshold_hr": "174",
            "threshold_pace": "4:15",
            "performance_method": "field_test",
            "performance_observed_on": today.isoformat(),
            "reference_10k": "43:10",
            "reference_observed_on": today.isoformat(),
            "constraint_note": "Keine Bergsprints bis zur Kontrolle.",
            "constraint_until": (today + timedelta(days=14)).isoformat(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/performance?notice=")
    with session_factory() as session:
        profile = session.scalar(select(AthleteProfile))
        goal = session.scalar(select(AthleteGoal))
        anchors = list(session.scalars(select(AthleteManualAnchor)))
        availability = session.scalar(select(AthleteAvailability))
        assert profile is not None and profile.experience_years == 5
        assert goal is not None and goal.target_duration_s == 2_520
        assert availability is not None and availability.max_duration_minutes == 75
        assert {item.metric for item in anchors} == {
            "max_hr",
            "threshold_hr",
            "threshold_pace_s_per_km",
            "reference_10k_seconds",
        }

    profile_page = client.get("/profile")
    assert profile_page.status_code == 200
    assert "Planungsgrundlage separat ansehen" in profile_page.text
    assert "Stadtlauf" not in profile_page.text
    performance_page = client.get("/performance")
    assert performance_page.status_code == 200
    assert "Stadtlauf" in performance_page.text
    assert "4:15 min/km" in performance_page.text
    assert "Keine Bergsprints bis zur Kontrolle." in performance_page.text
    assert "Di</strong> · 75 min" in performance_page.text
    assert "Empirische Bereiche" in performance_page.text
    assert "Easy Run" in performance_page.text
    assert "Detail- und FIT-Evidenz" in performance_page.text
    assert "Pausenbereinigte Laufanalyse" in performance_page.text
    assert "Automatischer Leistungszustand" in performance_page.text
    assert "Was du aktuell nachweislich leisten und vertragen kannst" in performance_page.text
    assert "Details bei Bedarf" in performance_page.text
    assert "4 · 12 · 26 Wochen" in performance_page.text
    assert "Deterministische Planungsregeln" in performance_page.text
    assert "Planungskorridor" in performance_page.text
    assert "Kein harter Tag direkt davor oder danach" in performance_page.text
    assert performance_page.text.count("<details") >= 5


def test_performance_page_renders_garmin_zones(client, session_factory):
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        session.add(
            DailyFitness(
                user_id=user.id,
                day=date.today(),
                heart_rate_zones=[
                    {"sport": "running", "zone": 1, "lower": 100, "upper": 130},
                    {"sport": "running", "zone": 2, "lower": 130, "upper": 150},
                ],
            )
        )
        session.commit()

    response = client.get("/performance")

    assert response.status_code == 200
    assert "Zone 1" in response.text
    assert "100–130 bpm" in response.text


def test_profile_edit_rejects_invalid_threshold_and_preserves_input(client):
    today = date.today()
    response = client.post(
        "/performance",
        data={
            "primary_sport": "running",
            "max_hr": "170",
            "threshold_hr": "175",
            "performance_observed_on": today.isoformat(),
            "goal_event_name": "Eingabe bleibt erhalten",
        },
    )

    assert response.status_code == 422
    assert "Die Schwellen-HF muss unter der HFmax liegen" in response.text
    assert "Eingabe bleibt erhalten" in response.text
