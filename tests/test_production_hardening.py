import json
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    User,
    Workout,
    WorkoutEvent,
    WorkoutGarminBinding,
    WorkoutRevision,
    WorkoutValidationRun,
)
from app.services.coach.provider import (
    COACH_PROMPT_TEMPLATE_VERSION,
    COACH_TOOL_CONTRACT_VERSION,
    PROGRESS_PROMPT,
    coach_tools,
)
from app.services.garmin.workout_export import scheduled_workout_ids
from app.services.observability import decision_trace, operational_metrics
from app.services.planning.validator import WorkoutValidationError
from app.services.planning.workout_service import WorkoutService, WorkoutTransitionError

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


def test_coach_tool_and_prompt_contracts_are_versioned_and_stable() -> None:
    fixture = json.loads((FIXTURES / "coach" / "tool_contract.json").read_text(encoding="utf-8"))
    actual: dict[str, list[str]] = {}
    for tool in coach_tools(workout_proposals_enabled=True):
        schema = tool.tool_call_schema
        json_schema = schema if isinstance(schema, dict) else cast(Any, schema).model_json_schema()
        actual[tool.name] = sorted(json_schema.get("properties", {}))

    assert fixture["contract_version"] == COACH_TOOL_CONTRACT_VERSION
    assert actual == fixture["tools"]
    assert COACH_PROMPT_TEMPLATE_VERSION == "coach-prompt-v9"
    assert "get_adaptive_context" in PROGRESS_PROMPT
    assert "keine Verlaufsdaten" in PROGRESS_PROMPT
    assert {"user_id", "workout_id", "definition", "idempotency_key"}.isdisjoint(
        actual["create_running_workout_proposal"]
    )
    for tool_name in ("record_pre_session_feedback", "record_post_session_feedback"):
        assert {
            "user_id",
            "conversation_id",
            "assistant_message_id",
            "source",
        }.isdisjoint(actual[tool_name])


def test_prompt_injection_corpus_cannot_expand_coach_mutation_authority() -> None:
    fixture = json.loads(
        (FIXTURES / "coach" / "prompt_injection_cases.json").read_text(encoding="utf-8")
    )
    tool_names = {tool.name for tool in coach_tools(workout_proposals_enabled=True)}

    assert fixture["source"] == "synthetic"
    assert len(fixture["cases"]) >= 4
    assert (
        tool_names & {"accept_workout", "schedule_workout", "push_workout", "delete_workout"}
        == set()
    )
    allowed_mutations = {
        "create_running_workout_proposal",
        "create_planning_goal",
        "update_planning_goal",
        "deactivate_planning_goal",
        "update_planning_profile",
        "set_planning_availability",
        "deactivate_planning_availability",
        "create_planning_anchor",
        "update_planning_anchor",
        "deactivate_planning_anchor",
        "record_pre_session_feedback",
        "record_post_session_feedback",
        "revise_running_workout_proposal",
    }
    assert tool_names - allowed_mutations == {
        "get_adaptive_context",
        "get_current_recovery_state",
        "get_subjective_context",
        "get_health_trends",
        "get_training_summary",
        "get_progress",
        "get_recent_activities",
        "get_activity_details",
        "get_health_day",
        "get_upcoming_workouts",
        "get_revisable_running_workouts",
        "get_planning_inputs",
    }


def _revision_graph(session: Session) -> tuple[WorkoutRevision, list[WorkoutValidationRun]]:
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
    run = WorkoutValidationRun(
        workout_id=workout.id,
        revision_id=revision.id,
        validation_kind="contextual",
        rule_set_version="rules-v1",
        context_fingerprint="b" * 64,
        feedback_ids_json=["private-feedback-id"],
        valid=True,
        report_json={"checks": [{"code": "safety.synthetic", "detail": "Private"}]},
    )
    session.add(run)
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
    return revision, [run]


def test_decision_trace_and_metrics_exclude_sensitive_payloads(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        revision, runs = _revision_graph(session)

        trace = decision_trace(revision, runs)
        metrics = operational_metrics(session)

    serialized = json.dumps(trace)
    assert trace["evidence_refs"] == ["EVIDENCE-SYNTHETIC-001"]
    assert trace["revision_rule_codes"] == ["validation.synthetic"]
    assert "Private" not in serialized
    assert "private_health_value" not in serialized
    assert "feedback" not in serialized
    assert metrics["lifecycle"]["propose"] == 1


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
    assert visible.json()["schema_version"] == "operational-metrics.v1"


@pytest.mark.parametrize(
    ("source_type", "disabled_setting"),
    [
        ("coach_single", "coach_workout_proposals_enabled"),
        ("coach_daily_adaptation", "coach_daily_adaptation_enabled"),
        ("coach_weekly_plan", "coach_plan_generation_enabled"),
    ],
)
def test_source_feature_kill_switch_blocks_generated_garmin_side_effects(
    source_type: str,
    disabled_setting: str,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "coach_workout_proposals_enabled", True)
    monkeypatch.setattr(settings, "coach_daily_adaptation_enabled", True)
    monkeypatch.setattr(settings, "coach_plan_generation_enabled", True)
    monkeypatch.setattr(settings, "coach_garmin_sync_enabled", True)
    monkeypatch.setattr(settings, disabled_setting, False)
    with session_factory() as session:
        user = User(display_name="Rollback Athlete")
        session.add(user)
        session.flush()
        workout = Workout(
            user_id=user.id,
            name="Generated",
            sport="running",
            definition_version=1,
            definition={"blocks": []},
            source_type=source_type,
            approval_status="accepted",
        )
        session.add(workout)
        session.flush()
        revision = WorkoutRevision(
            workout_id=workout.id,
            revision_number=1,
            name="Generated",
            sport="running",
            definition_version=1,
            definition={"blocks": []},
            source_type=source_type,
            content_hash="c" * 64,
        )
        session.add(revision)
        session.flush()
        workout.current_revision_id = revision.id
        workout.accepted_revision_id = revision.id
        session.add(WorkoutGarminBinding(workout_id=workout.id))
        session.commit()

        with pytest.raises(WorkoutTransitionError) as exc_info:
            WorkoutService(session, user).publish(workout.id)

    assert exc_info.value.code == "coach.source_feature_disabled"
