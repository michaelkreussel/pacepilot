import json
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import WorkoutRevision
from app.services.planning.workout_definition import definition_to_json
from app.services.planning.workout_templates import (
    TemplateEligibilityContext,
    expand_workout_template,
)

ELIGIBLE = TemplateEligibilityContext(
    consistent_running_weeks=12,
    runs_per_week=3,
    available_minutes=60,
)


def test_v2_workout_round_trip_preview_and_garmin_degradation(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    expanded = expand_workout_template("easy_run", eligibility=ELIGIBLE)
    definition = definition_to_json(expanded.definition)
    response = client.post(
        "/workouts",
        data={
            "name": "Lockerer Lauf nach Talk Test",
            "sport": "running",
            "scheduled_for": date.today().isoformat(),
            "description": "Zeitbasiert und ohne erfundene Pace",
            "definition_version": "2",
            "definition": json.dumps(definition),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "RPE 2–3/10" in page.text
    assert "vollständigen Sätzen" in page.text
    assert "Garmin kann das RPE-Ziel nicht als Gerätebereich abbilden" in page.text
    assert "Lokale Schrittanweisungen bleiben in PacePilot" in page.text

    edit = client.get(f"{response.headers['location']}/edit")
    assert edit.status_code == 200
    assert 'name="definition_version"' in edit.text
    assert "vollst\\u00e4ndigen S\\u00e4tzen" in edit.text
    with session_factory() as session:
        revision = session.scalar(select(WorkoutRevision))
        assert revision is not None
        assert revision.definition_version == 2


def test_v2_validation_error_preserves_local_guidance(client: TestClient) -> None:
    expanded = expand_workout_template("easy_run", eligibility=ELIGIBLE)
    definition = definition_to_json(expanded.definition)
    definition["blocks"][0]["target"] = {
        "type": "rpe_range",
        "lower_rpe": 5,
        "upper_rpe": 2,
    }

    response = client.post(
        "/workouts",
        data={
            "name": "Ungültiger Easy Run",
            "sport": "running",
            "scheduled_for": date.today().isoformat(),
            "description": "",
            "definition_version": "2",
            "definition": json.dumps(definition),
        },
    )

    assert response.status_code == 422
    assert "untere RPE-Grenze" in response.text
    assert "vollst\\u00e4ndigen S\\u00e4tzen" in response.text


def test_v2_schema_error_explains_instruction_limit(client: TestClient) -> None:
    expanded = expand_workout_template("easy_run", eligibility=ELIGIBLE)
    definition = definition_to_json(expanded.definition)
    definition["blocks"][0]["instructions"] = [f"Hinweis {index}" for index in range(6)]

    response = client.post(
        "/workouts",
        data={
            "name": "Zu viele Hinweise",
            "sport": "running",
            "scheduled_for": date.today().isoformat(),
            "description": "",
            "definition_version": "2",
            "definition": json.dumps(definition),
        },
    )

    assert response.status_code == 422
    assert "maximal fünf Zeilen" in response.text
    assert "Hinweis 5" in response.text
