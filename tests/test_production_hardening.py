import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    User,
    Workout,
    WorkoutEvent,
    WorkoutRevision,
)
from app.services.garmin.workout_export import scheduled_workout_ids
from app.services.observability import decision_trace, operational_metrics
from app.services.planning.validator import WorkoutValidationError

FIXTURES = Path(__file__).parent / "fixtures"


class _CalendarClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def get_scheduled_workouts(self, _year: int, _month: int) -> object:
        return self.payload


def test_synthetic_garmin_calendar_contract_fixture() -> None:
    fixture = json.loads(
        (FIXTURES / "garmin" / "contracts" / "scheduled_workouts.json").read_text(encoding="utf-8")
    )

    assert fixture["source"] == "synthetic"
    for case in fixture["cases"]:
        result = scheduled_workout_ids(
            _CalendarClient(case["payload"]),
            "synthetic-workout-1",
            date(2026, 1, 15),
        )
        assert result == case["expected_ids"]


@pytest.mark.parametrize(
    "payload",
    [None, "changed", 42, {"unexpected": "shape"}, {"unexpected": []}],
)
def test_calendar_contract_drift_stops_reconciliation(payload: object) -> None:
    with pytest.raises(WorkoutValidationError) as exc_info:
        scheduled_workout_ids(
            _CalendarClient(payload),
            "synthetic-workout-1",
            date(2026, 1, 15),
        )

    assert exc_info.value.code == "garmin.contract_drift"


def test_synthetic_contract_fixtures_contain_no_sensitive_fields() -> None:
    forbidden = {
        "authorization",
        "cookie",
        "latitude",
        "longitude",
        "oauth",
        "password",
        "polyline",
        "token",
    }
    for path in FIXTURES.rglob("*.json"):
        content = path.read_text(encoding="utf-8").lower()
        assert not any(f'"{name}"' in content for name in forbidden), path


def _revision_graph(session: Session) -> WorkoutRevision:
    user = User(display_name="Trace Athlete")
    session.add(user)
    session.flush()
    workout = Workout(
        user_id=user.id,
        name="Private workout name",
        sport="running",
        definition_version=1,
        definition={"blocks": []},
        source_type="coach_proposal",
        approval_status="proposed",
    )
    session.add(workout)
    session.flush()
    revision = WorkoutRevision(
        workout_id=workout.id,
        revision_number=1,
        name="Private workout name",
        sport="running",
        definition_version=1,
        definition={"blocks": []},
        purpose="Private purpose",
        guidance_json={"evidence_refs": ["EVIDENCE-SYNTHETIC-001"]},
        validation_report_json={
            "issues": [{"code": "validation.synthetic", "message": "Private message"}]
        },
        generation_context_json={"private_health_value": 99},
        source_type="coach_proposal",
        generator_version="generator-v1",
        template_id="easy_run",
        template_version="1",
        rule_set_version="rules-v1",
        knowledge_base_version="knowledge-v1",
        model_provider="openrouter",
        model_id="synthetic/model",
        prompt_template_version="coach-prompt-v2",
        content_hash="a" * 64,
    )
    session.add(revision)
    session.flush()
    workout.current_revision_id = revision.id
    session.add(
        WorkoutEvent(
            workout_id=workout.id,
            revision_id=revision.id,
            owner_user_id=user.id,
            actor_type="user",
            actor_user_id=user.id,
            action="propose",
            safe_metadata_json={},
        )
    )
    session.commit()
    return revision


def test_decision_trace_and_metrics_exclude_sensitive_payloads(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        revision = _revision_graph(session)

        trace = decision_trace(revision)
        metrics = operational_metrics(session)

    serialized = json.dumps(trace)
    assert trace["schema_version"] == "decision-trace.v2"
    assert trace["evidence_refs"] == ["EVIDENCE-SYNTHETIC-001"]
    assert trace["revision_rule_codes"] == ["validation.synthetic"]
    assert "Private" not in serialized
    assert "private_health_value" not in serialized
    assert "feedback" not in serialized
    assert "validations" not in trace
    assert metrics["lifecycle"]["propose"] == 1
    assert "validation" not in metrics


def test_metrics_endpoint_is_hidden_without_valid_bearer_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "m" * 32
    monkeypatch.setattr(get_settings(), "metrics_bearer_token", token)

    hidden = client.get("/api/metrics")
    visible = client.get("/api/metrics", headers={"Authorization": f"Bearer {token}"})

    assert hidden.status_code == 404
    assert visible.status_code == 200
    assert visible.json()["schema_version"] == "operational-metrics.v2"
