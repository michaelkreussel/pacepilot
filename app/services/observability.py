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


def decision_trace(revision: WorkoutRevision) -> dict[str, Any]:
    return {
        "schema_version": "decision-trace.v2",
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
        "revision_rule_codes": _safe_codes(revision.validation_report_json),
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
