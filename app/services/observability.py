from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    WorkoutEvent,
    WorkoutGarminOperation,
    WorkoutRevision,
)


def _safe_codes(report: dict[str, object] | None) -> list[str]:
    if not report:
        return []
    codes: set[str] = set()
    for key in ("issues", "rules", "checks"):
        values = report.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                code = value.get("code")
                if isinstance(code, str) and 0 < len(code) <= 100:
                    codes.add(code)
    return sorted(codes)


def _evidence_refs(revision: WorkoutRevision) -> list[str]:
    guidance = revision.guidance_json or {}
    values = guidance.get("evidence_refs")
    if not isinstance(values, list):
        return []
    return sorted({value for value in values if isinstance(value, str) and 0 < len(value) <= 100})


def _safe_text(value: object, *, max_length: int = 100) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= max_length else None


def _advisory_assessment(revision: WorkoutRevision) -> dict[str, object] | None:
    guidance = revision.guidance_json or {}
    assessment = guidance.get("training_fit")
    if not isinstance(assessment, dict):
        return None
    outcome = assessment.get("outcome")
    if outcome not in {"normal", "caution", "elevated"}:
        outcome = None
    warning_codes = assessment.get("warning_codes")
    return {
        "outcome": outcome,
        "policy_version": _safe_text(assessment.get("policy_version")),
        "evaluated_at": _safe_text(assessment.get("evaluated_at")),
        "effective_workout_date": _safe_text(assessment.get("effective_workout_date")),
        "warning_codes": sorted(
            {code for code in warning_codes if isinstance(code, str) and 0 < len(code) <= 100}
        )
        if isinstance(warning_codes, list)
        else [],
        "assessment_fingerprint": _safe_text(
            assessment.get("authoritative_input_fingerprint"), max_length=64
        ),
    }


def _event_acknowledgement(event: WorkoutEvent) -> dict[str, object] | None:
    authorization = event.safe_metadata_json.get("training_fit_authorization")
    if not isinstance(authorization, dict):
        return None
    return {
        "source": "workout_event",
        "action": event.action,
        "policy_version": _safe_text(authorization.get("policy_version")),
        "assessment_fingerprint": _safe_text(
            authorization.get("assessment_fingerprint"), max_length=64
        ),
        "effective_date": _safe_text(authorization.get("effective_date")),
        "acknowledged_at": _safe_text(authorization.get("acknowledged_at")),
        "authorized_revision_id": authorization.get("authorized_revision_id"),
    }


def _operation_acknowledgement(
    operation: WorkoutGarminOperation,
) -> dict[str, object] | None:
    if operation.training_fit_acknowledged_at is None:
        return None
    return {
        "source": "garmin_operation",
        "action": operation.operation_type,
        "policy_version": operation.training_fit_policy_version,
        "assessment_fingerprint": operation.training_fit_assessment_fingerprint,
        "effective_date": (
            operation.training_fit_effective_date.isoformat()
            if operation.training_fit_effective_date is not None
            else None
        ),
        "acknowledged_at": operation.training_fit_acknowledged_at.isoformat(),
        "authorized_revision_id": operation.training_fit_authorized_revision_id,
    }


def decision_trace(session: Session, revision: WorkoutRevision) -> dict[str, Any]:
    events = list(
        session.scalars(
            select(WorkoutEvent)
            .where(
                WorkoutEvent.workout_id == revision.workout_id,
                WorkoutEvent.revision_id == revision.id,
            )
            .order_by(WorkoutEvent.id)
        )
    )
    operations = list(
        session.scalars(
            select(WorkoutGarminOperation)
            .where(
                WorkoutGarminOperation.workout_id == revision.workout_id,
                WorkoutGarminOperation.revision_id == revision.id,
            )
            .order_by(WorkoutGarminOperation.id)
        )
    )
    acknowledgements = [
        acknowledgement
        for acknowledgement in (
            *(_event_acknowledgement(event) for event in events),
            *(_operation_acknowledgement(operation) for operation in operations),
        )
        if acknowledgement is not None
    ]
    structural_report = revision.validation_report_json or {}
    structural_valid = structural_report.get("valid")
    return {
        "schema_version": "decision-trace.v3",
        "workout_id": revision.workout_id,
        "revision_id": revision.id,
        "revision_number": revision.revision_number,
        "source_type": revision.source_type,
        "template": {
            "id": revision.template_id,
            "version": revision.template_version,
        },
        "versions": {
            "generator": revision.generator_version,
            "rule_set": revision.rule_set_version,
            "knowledge_base": revision.knowledge_base_version,
            "model_provider": revision.model_provider,
            "model": revision.model_id,
            "prompt": revision.prompt_template_version,
        },
        "evidence_refs": _evidence_refs(revision),
        "structural_validation": {
            "valid": structural_valid if isinstance(structural_valid, bool) else None,
            "rule_codes": _safe_codes(revision.validation_report_json),
        },
        "advisory_assessment": _advisory_assessment(revision),
        "acknowledgements": acknowledgements,
        "lifecycle_events": [
            {
                "action": event.action,
                "occurred_at": event.created_at.isoformat(),
            }
            for event in events
        ],
        "garmin_operations": [
            {
                "operation_type": operation.operation_type,
                "status": operation.status,
                "error_code": operation.error_code,
            }
            for operation in operations
        ],
    }


def operational_metrics(session: Session) -> dict[str, Any]:
    event_counts = Counter(session.scalars(select(WorkoutEvent.action)))
    garmin_counts = Counter(
        (operation_type, status)
        for operation_type, status in session.execute(
            select(
                WorkoutGarminOperation.operation_type,
                WorkoutGarminOperation.status,
            )
        )
    )
    lifecycle_actions = (
        "propose",
        "revise",
        "accept",
        "reject",
        "adapt_keep",
        "adapt_rest",
        "adapt_propose",
        "adapt_replace_propose",
    )
    return {
        "schema_version": "operational-metrics.v2",
        "lifecycle": {action: event_counts[action] for action in lifecycle_actions},
        "garmin_operations": {
            f"{operation_type}.{status}": count
            for (operation_type, status), count in sorted(garmin_counts.items())
        },
        "garmin_unresolved": sum(
            count
            for (operation_type, status), count in garmin_counts.items()
            if status in {"pending", "unknown"}
        ),
    }
