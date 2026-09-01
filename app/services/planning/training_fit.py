import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Activity, PostSessionFeedback, PreSessionFeedback
from app.models.user import utcnow
from app.services.analytics.health_trends import MetricTrend, get_health_trends
from app.services.analytics.subjective_feedback import effective_activity_feedback
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.safety_triage import (
    EffectiveSessionFeedback,
    TriageOutcome,
    triage_feedback,
)


class TrainingFitOutcome(StrEnum):
    NORMAL = "normal"
    CAUTION = "caution"
    ELEVATED = "elevated"


@dataclass(frozen=True)
class TrainingFitPolicy:
    version: str
    evidence_window_days: int
    minimum_baseline_samples: int
    required_severe_signals: int
    low_readiness_threshold: int
    severe_resting_hr_ratio: float
    severe_hrv_ratio: float
    severe_sleep_duration_ratio: float
    severe_stress_ratio: float


@dataclass(frozen=True)
class TrainingFitEvidence:
    code: str
    source: str
    observed_on: date
    value: float | str | None
    unit: str | None
    personal_baseline: float | None
    ratio_from_baseline: float | None
    severe: bool
    feedback_id: str | None = None


@dataclass(frozen=True)
class TrainingFitCoverage:
    metric: str
    current_day: date | None
    baseline_sample_count: int
    minimum_baseline_samples: int
    sufficient_for_elevation: bool


@dataclass(frozen=True)
class TrainingFitAssessment:
    outcome: TrainingFitOutcome
    policy_version: str
    evaluated_at: datetime
    effective_workout_date: date
    warning_codes: tuple[str, ...]
    evidence: tuple[TrainingFitEvidence, ...]
    coverage: tuple[TrainingFitCoverage, ...]
    feedback_ids: tuple[str, ...]
    authoritative_input_fingerprint: str


def _number(parameters: Mapping[str, object], name: str) -> int | float:
    value = parameters[name]
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise RuntimeError(f"Training-fit policy parameter {name} must be numeric")
    return value


def load_training_fit_policy() -> TrainingFitPolicy:
    registry = get_knowledge_registry()
    parameters = registry.constraints["TRAINING-FIT-001"].parameters
    knowledge_version = registry.version.rsplit(":", 1)[-1][:12]
    return TrainingFitPolicy(
        version=f"training-fit-v1+{knowledge_version}",
        evidence_window_days=int(_number(parameters, "evidence_window_days")),
        minimum_baseline_samples=int(_number(parameters, "minimum_baseline_samples")),
        required_severe_signals=int(_number(parameters, "required_severe_signals")),
        low_readiness_threshold=int(_number(parameters, "low_readiness_threshold")),
        severe_resting_hr_ratio=float(_number(parameters, "severe_resting_hr_ratio")),
        severe_hrv_ratio=float(_number(parameters, "severe_hrv_ratio")),
        severe_sleep_duration_ratio=float(_number(parameters, "severe_sleep_duration_ratio")),
        severe_stress_ratio=float(_number(parameters, "severe_stress_ratio")),
    )


def _recent(day: date | None, evaluated_on: date, window_days: int) -> bool:
    return day is not None and 0 <= (evaluated_on - day).days <= window_days


def _metric_result(
    trend: MetricTrend,
    *,
    source: str,
    threshold: float,
    direction: Literal["above", "below"],
    policy: TrainingFitPolicy,
    evaluated_on: date,
) -> tuple[TrainingFitCoverage, TrainingFitEvidence | None]:
    recent = _recent(trend.current_day, evaluated_on, policy.evidence_window_days)
    sufficient = (
        recent
        and trend.personal_baseline is not None
        and trend.personal_baseline > 0
        and trend.baseline_sample_count >= policy.minimum_baseline_samples
    )
    coverage = TrainingFitCoverage(
        metric=trend.metric,
        current_day=trend.current_day,
        baseline_sample_count=trend.baseline_sample_count,
        minimum_baseline_samples=policy.minimum_baseline_samples,
        sufficient_for_elevation=sufficient,
    )
    if trend.current is None or trend.current_day is None or trend.personal_baseline in {None, 0}:
        return coverage, None
    ratio = trend.current / trend.personal_baseline
    severe = ratio >= threshold if direction == "above" else ratio <= threshold
    if not severe:
        return coverage, None
    return coverage, TrainingFitEvidence(
        code=f"health.{trend.metric}.severe_deviation",
        source=source,
        observed_on=trend.current_day,
        value=trend.current,
        unit=trend.unit,
        personal_baseline=trend.personal_baseline,
        ratio_from_baseline=round(ratio, 4),
        severe=True,
    )


def _load_feedback(
    session: Session,
    user_id: int,
    evaluated_at: datetime,
    window_days: int,
) -> tuple[
    list[PreSessionFeedback],
    list[PostSessionFeedback],
    list[EffectiveSessionFeedback],
    dict[str, date],
]:
    cutoff = datetime.combine(
        evaluated_at.date() - timedelta(days=window_days),
        time.min,
    )
    pre_feedback = list(
        session.scalars(
            select(PreSessionFeedback)
            .where(
                PreSessionFeedback.user_id == user_id,
                PreSessionFeedback.recorded_at >= cutoff,
                PreSessionFeedback.recorded_at <= evaluated_at,
            )
            .order_by(PreSessionFeedback.recorded_at, PreSessionFeedback.id)
        )
    )
    post_feedback = list(
        session.scalars(
            select(PostSessionFeedback)
            .where(
                PostSessionFeedback.user_id == user_id,
                PostSessionFeedback.recorded_at >= cutoff,
                PostSessionFeedback.recorded_at <= evaluated_at,
            )
            .order_by(PostSessionFeedback.recorded_at, PostSessionFeedback.id)
        )
    )
    linked_activity_ids = {
        feedback.activity_id for feedback in post_feedback if feedback.activity_id is not None
    }
    activity_filter = Activity.started_at.between(cutoff, evaluated_at)
    if linked_activity_ids:
        activity_filter = or_(activity_filter, Activity.id.in_(linked_activity_ids))
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.user_id == user_id, activity_filter)
            .order_by(Activity.started_at, Activity.id)
        )
    )
    effective = effective_activity_feedback(session, user_id, activities)
    effective_sessions = [
        EffectiveSessionFeedback(
            activity_id=activity.id,
            effort=effective[activity.id].effort,
            feel=effective[activity.id].feel,
            feedback_ids=tuple(
                dict.fromkeys(
                    f"post:{feedback_id}"
                    for feedback_id in (
                        effective[activity.id].effort_feedback_id,
                        effective[activity.id].feel_feedback_id,
                    )
                    if feedback_id is not None
                )
            ),
        )
        for activity in activities
        if effective[activity.id].effort is not None or effective[activity.id].feel is not None
    ]
    effective_sessions.extend(
        EffectiveSessionFeedback(
            activity_id=None,
            effort=feedback.session_rpe,
            feel=feedback.overall_feel,
            feedback_ids=(f"post:{feedback.id}",),
        )
        for feedback in post_feedback
        if feedback.activity_id is None
    )
    evidence_days = {
        **{f"pre:{item.id}": item.recorded_at.date() for item in pre_feedback},
        **{f"post:{item.id}": item.recorded_at.date() for item in post_feedback},
        **{f"activity-feedback:{item.id}": item.started_at.date() for item in activities},
    }
    return pre_feedback, post_feedback, effective_sessions, evidence_days


def _fingerprint(
    *,
    user_id: int,
    policy: TrainingFitPolicy,
    evaluated_on: date,
    effective_workout_date: date,
    revision_fingerprint: str,
    metric_inputs: list[dict[str, object]],
    feedback_inputs: list[dict[str, object]],
    effective_sessions: list[EffectiveSessionFeedback],
    evidence_days: dict[str, date],
) -> str:
    payload = {
        "effective_workout_date": effective_workout_date.isoformat(),
        "evidence_days": evidence_days,
        "evaluated_on": evaluated_on.isoformat(),
        "feedback": feedback_inputs,
        "health": metric_inputs,
        "policy": asdict(policy),
        "revision_fingerprint": revision_fingerprint,
        "session_feedback": [asdict(item) for item in effective_sessions],
        "user_id": user_id,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def assess_training_fit(
    session: Session,
    user_id: int,
    *,
    effective_workout_date: date,
    revision_fingerprint: str,
    evaluated_at: datetime | None = None,
    policy: TrainingFitPolicy | None = None,
) -> TrainingFitAssessment:
    evaluated_at = evaluated_at or utcnow()
    evaluated_on = evaluated_at.date()
    policy = policy or load_training_fit_policy()
    trends = get_health_trends(session, user_id, days=28, as_of=evaluated_on)
    metric_rules = (
        (
            trends.resting_hr,
            "health.resting_hr",
            policy.severe_resting_hr_ratio,
            "above",
        ),
        (trends.hrv, "health.hrv", policy.severe_hrv_ratio, "below"),
        (
            trends.sleep_duration,
            "health.sleep_duration",
            policy.severe_sleep_duration_ratio,
            "below",
        ),
        (trends.stress, "health.stress", policy.severe_stress_ratio, "above"),
    )
    coverage: list[TrainingFitCoverage] = []
    evidence: list[TrainingFitEvidence] = []
    metric_inputs: list[dict[str, object]] = []
    for trend, source, threshold, direction in metric_rules:
        item_coverage, item_evidence = _metric_result(
            trend,
            source=source,
            threshold=threshold,
            direction=direction,
            policy=policy,
            evaluated_on=evaluated_on,
        )
        coverage.append(item_coverage)
        if item_evidence is not None:
            evidence.append(item_evidence)
        metric_inputs.append(
            {
                "baseline": trend.personal_baseline,
                "baseline_sample_count": trend.baseline_sample_count,
                "current": trend.current,
                "current_day": trend.current_day,
                "metric": trend.metric,
            }
        )

    readiness = trends.garmin_training_readiness
    metric_inputs.append(
        {
            "current": readiness.current,
            "current_day": readiness.current_day,
            "metric": readiness.metric,
        }
    )
    if (
        readiness.current is not None
        and readiness.current <= policy.low_readiness_threshold
        and _recent(readiness.current_day, evaluated_on, policy.evidence_window_days)
    ):
        evidence.append(
            TrainingFitEvidence(
                code="readiness.low_score",
                source="health.garmin_training_readiness",
                observed_on=readiness.current_day or evaluated_on,
                value=readiness.current,
                unit=readiness.unit,
                personal_baseline=None,
                ratio_from_baseline=None,
                severe=False,
            )
        )

    pre_feedback, post_feedback, effective_sessions, evidence_days = _load_feedback(
        session,
        user_id,
        evaluated_at,
        policy.evidence_window_days,
    )
    feedback_report = triage_feedback(pre_feedback, post_feedback, effective_sessions)
    serious_feedback = False
    for issue in feedback_report.issues:
        serious_feedback = serious_feedback or issue.severity == TriageOutcome.SAFETY_STOP
        for feedback_id in issue.feedback_ids:
            evidence.append(
                TrainingFitEvidence(
                    code=issue.code,
                    source=(
                        "feedback.pre_session"
                        if feedback_id.startswith("pre:")
                        else "feedback.post_session"
                        if feedback_id.startswith("post:")
                        else "feedback.activity"
                    ),
                    observed_on=evidence_days.get(feedback_id, evaluated_on),
                    value=None,
                    unit=None,
                    personal_baseline=None,
                    ratio_from_baseline=None,
                    severe=issue.severity == TriageOutcome.SAFETY_STOP,
                    feedback_id=feedback_id,
                )
            )

    feedback_inputs: list[dict[str, object]] = [
        {
            "content_hash": item.content_hash,
            "id": f"pre:{item.id}",
            "recorded_at": item.recorded_at,
        }
        for item in pre_feedback
    ] + [
        {
            "content_hash": item.content_hash,
            "id": f"post:{item.id}",
            "recorded_at": item.recorded_at,
        }
        for item in post_feedback
    ]
    feedback_ids = tuple(str(item["id"]) for item in feedback_inputs)
    warning_codes = list(dict.fromkeys(item.code for item in evidence))
    current_metric_count = sum(item.current_day is not None for item in coverage)
    sufficient_metric_count = sum(item.sufficient_for_elevation for item in coverage)
    if current_metric_count == 0:
        warning_codes.append("coverage.health_missing")
    elif sufficient_metric_count < policy.required_severe_signals:
        warning_codes.append("coverage.personal_baseline_sparse")

    eligible_severe_signals = sum(
        item.severe
        and item.feedback_id is None
        and any(
            item.source == f"health.{item_coverage.metric}"
            and item_coverage.sufficient_for_elevation
            for item_coverage in coverage
        )
        for item in evidence
    )
    same_day = effective_workout_date == evaluated_on
    if same_day and (serious_feedback or eligible_severe_signals >= policy.required_severe_signals):
        outcome = TrainingFitOutcome.ELEVATED
    elif warning_codes:
        outcome = TrainingFitOutcome.CAUTION
    else:
        outcome = TrainingFitOutcome.NORMAL

    return TrainingFitAssessment(
        outcome=outcome,
        policy_version=policy.version,
        evaluated_at=evaluated_at,
        effective_workout_date=effective_workout_date,
        warning_codes=tuple(dict.fromkeys(warning_codes)),
        evidence=tuple(evidence),
        coverage=tuple(coverage),
        feedback_ids=feedback_ids,
        authoritative_input_fingerprint=_fingerprint(
            user_id=user_id,
            policy=policy,
            evaluated_on=evaluated_on,
            effective_workout_date=effective_workout_date,
            revision_fingerprint=revision_fingerprint,
            metric_inputs=metric_inputs,
            feedback_inputs=feedback_inputs,
            effective_sessions=effective_sessions,
            evidence_days=evidence_days,
        ),
    )
