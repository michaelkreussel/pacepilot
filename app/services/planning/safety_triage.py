import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import PostSessionFeedback, PreSessionFeedback, Workout, WorkoutRevision
from app.models.user import utcnow
from app.services.planning.workout_revision import default_context_fingerprint

SAFETY_RULE_SET_VERSION = "safety-triage-v1"
FEEDBACK_FRESHNESS_DAYS = 7
type ValidationMode = Literal["acceptance", "sync"]


class IllnessSignal(StrEnum):
    NONE = "none"
    MILD_UPPER_RESPIRATORY = "mild_upper_respiratory"
    FEVER = "fever"
    SYSTEMIC = "systemic"
    CARDIOPULMONARY_WARNING = "cardiopulmonary_warning"
    UNKNOWN = "unknown"


class TriageOutcome(StrEnum):
    ALLOW = "allow"
    CLARIFY = "clarify"
    WARN = "warn"
    SAFETY_STOP = "safety_stop"


class PainInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    present: bool = False
    location: str | None = Field(default=None, max_length=100)
    severity: int | None = Field(default=None, ge=0, le=10)
    alters_gait: bool | None = None
    worsens_with_activity: bool | None = None


class PreSessionFeedbackInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    motivation: int = Field(ge=1, le=5)
    fatigue: int = Field(ge=1, le=5)
    leg_freshness: int = Field(ge=1, le=5)
    soreness: int = Field(ge=0, le=10)
    sleep_quality: int | None = Field(default=None, ge=1, le=5)
    pain: PainInput = Field(default_factory=PainInput)
    illness_signal: IllnessSignal = IllnessSignal.NONE
    available_minutes: int | None = Field(default=None, ge=0, le=1440)
    notes: str | None = Field(default=None, max_length=2000)


class PostSessionFeedbackInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    completion_percent: int = Field(ge=0, le=100)
    session_rpe: float = Field(ge=0, le=10)
    overall_feel: int = Field(ge=1, le=5)
    pain: PainInput = Field(default_factory=PainInput)
    stopped_reason: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


@dataclass(frozen=True)
class SafetyIssue:
    code: str
    severity: TriageOutcome
    message: str
    rule_id: str
    feedback_ids: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "rule_id": self.rule_id,
            "feedback_ids": list(self.feedback_ids),
        }


@dataclass(frozen=True)
class SafetyReport:
    outcome: TriageOutcome
    issues: tuple[SafetyIssue, ...]

    @property
    def valid(self) -> bool:
        return self.outcome in {TriageOutcome.ALLOW, TriageOutcome.WARN}

    def to_json(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "outcome": self.outcome.value,
            "issues": [issue.to_json() for issue in self.issues],
        }


@dataclass(frozen=True)
class SafetyContext:
    fingerprint: str
    feedback_ids: tuple[str, ...]
    report: SafetyReport


def feedback_content_hash(data: BaseModel) -> str:
    payload = json.dumps(
        data.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _issue(
    issues: dict[str, SafetyIssue],
    *,
    code: str,
    severity: TriageOutcome,
    message: str,
    rule_id: str,
    feedback_id: str,
) -> None:
    existing = issues.get(code)
    feedback_ids = (existing.feedback_ids if existing else ()) + (feedback_id,)
    issues[code] = SafetyIssue(code, severity, message, rule_id, feedback_ids)


def triage_feedback(
    pre_feedback: list[PreSessionFeedback],
    post_feedback: list[PostSessionFeedback],
) -> SafetyReport:
    issues: dict[str, SafetyIssue] = {}
    for feedback in pre_feedback:
        feedback_id = f"pre:{feedback.id}"
        if feedback.illness_signal == IllnessSignal.CARDIOPULMONARY_WARNING:
            _issue(
                issues,
                code="safety.cardiopulmonary_warning",
                severity=TriageOutcome.SAFETY_STOP,
                message=(
                    "Bitte starte dieses Lauftraining nicht. Bei Brustschmerz, ungewöhnlicher "
                    "Atemnot, Ohnmacht oder ähnlichen Warnzeichen hole zeitnah professionelle "
                    "medizinische Hilfe; bei akuten oder starken Beschwerden sofort."
                ),
                rule_id="SAFE-CARDIO-001",
                feedback_id=feedback_id,
            )
        if feedback.illness_signal in {IllnessSignal.FEVER, IllnessSignal.SYSTEMIC}:
            _issue(
                issues,
                code="safety.fever_or_systemic_illness",
                severity=TriageOutcome.SAFETY_STOP,
                message=(
                    "Bitte starte dieses Lauftraining nicht. Pausiere bei Fieber oder deutlichem "
                    "allgemeinem Krankheitsgefühl und kläre anhaltende oder starke Beschwerden "
                    "professionell ab."
                ),
                rule_id="SAFE-ILLNESS-001",
                feedback_id=feedback_id,
            )
        elif feedback.illness_signal == IllnessSignal.UNKNOWN:
            _issue(
                issues,
                code="safety.illness_unclear",
                severity=TriageOutcome.CLARIFY,
                message=(
                    "Bitte beschreibe die Krankheitszeichen genauer, bevor du das Training "
                    "freigibst."
                ),
                rule_id="SAFE-ILLNESS-002",
                feedback_id=feedback_id,
            )
        elif feedback.illness_signal == IllnessSignal.MILD_UPPER_RESPIRATORY:
            _issue(
                issues,
                code="safety.mild_illness",
                severity=TriageOutcome.WARN,
                message=(
                    "Leichte Erkältungszeichen sind gemeldet. Trainiere nur locker und brich "
                    "bei Verschlechterung ab."
                ),
                rule_id="SAFE-ILLNESS-003",
                feedback_id=feedback_id,
            )
        _triage_pain(
            issues,
            feedback_id,
            feedback.pain_present,
            feedback.pain_location,
            feedback.pain_severity,
            feedback.pain_alters_gait,
            feedback.pain_worsens_with_activity,
        )
        if (
            feedback.fatigue >= 4
            or feedback.leg_freshness <= 2
            or feedback.soreness >= 5
            or feedback.sleep_quality is not None
            and feedback.sleep_quality <= 2
        ):
            _issue(
                issues,
                code="readiness.subjective_strain",
                severity=TriageOutcome.WARN,
                message=(
                    "Deine subjektiven Angaben sprechen für erhöhte Belastung. Umfang und "
                    "Intensität sollten konservativ bleiben."
                ),
                rule_id="READY-SUBJECTIVE-001",
                feedback_id=feedback_id,
            )
        if feedback.available_minutes == 0:
            _issue(
                issues,
                code="constraint.no_time_available",
                severity=TriageOutcome.WARN,
                message="Heute ist kein Zeitbudget für dieses Training angegeben.",
                rule_id="TIME-BUDGET-001",
                feedback_id=feedback_id,
            )

    for feedback in post_feedback:
        feedback_id = f"post:{feedback.id}"
        _triage_pain(
            issues,
            feedback_id,
            feedback.pain_present,
            feedback.pain_location,
            feedback.pain_severity,
            feedback.pain_alters_gait,
            feedback.pain_worsens_with_activity,
        )
        if (
            feedback.completion_percent < 75
            or feedback.session_rpe >= 8
            or feedback.overall_feel <= 2
        ):
            _issue(
                issues,
                code="recovery.difficult_session",
                severity=TriageOutcome.WARN,
                message=(
                    "Die letzte Einheit war auffällig belastend oder unvollständig. Die nächste "
                    "Belastung sollte konservativ geprüft werden."
                ),
                rule_id="RECOVERY-SESSION-001",
                feedback_id=feedback_id,
            )

    severity_order = {
        TriageOutcome.ALLOW: 0,
        TriageOutcome.WARN: 1,
        TriageOutcome.CLARIFY: 2,
        TriageOutcome.SAFETY_STOP: 3,
    }
    outcome = max(
        (issue.severity for issue in issues.values()),
        key=severity_order.__getitem__,
        default=TriageOutcome.ALLOW,
    )
    return SafetyReport(outcome, tuple(issues.values()))


def _triage_pain(
    issues: dict[str, SafetyIssue],
    feedback_id: str,
    present: bool,
    location: str | None,
    severity: int | None,
    alters_gait: bool | None,
    worsens_with_activity: bool | None,
) -> None:
    if not present:
        return
    if alters_gait is True:
        _issue(
            issues,
            code="safety.pain_alters_gait",
            severity=TriageOutcome.SAFETY_STOP,
            message=(
                "Bitte starte kein Lauftraining mit Schmerz, der dein Gangbild verändert. "
                "Lass anhaltende oder starke Beschwerden professionell beurteilen."
            ),
            rule_id="SAFE-PAIN-001",
            feedback_id=feedback_id,
        )
        return
    if not location or severity is None or alters_gait is None:
        _issue(
            issues,
            code="safety.pain_unclear",
            severity=TriageOutcome.CLARIFY,
            message="Bitte gib Schmerzort, Stärke und eine mögliche Veränderung des Gangbilds an.",
            rule_id="SAFE-PAIN-002",
            feedback_id=feedback_id,
        )
        return
    if severity >= 4 or worsens_with_activity is True:
        _issue(
            issues,
            code="safety.pain_warning",
            severity=TriageOutcome.WARN,
            message=(
                "Schmerz ist gemeldet. Reduziere die Belastung und brich ab, wenn er zunimmt "
                "oder dein Gangbild verändert."
            ),
            rule_id="SAFE-PAIN-003",
            feedback_id=feedback_id,
        )


def build_safety_context(
    session: Session,
    user_id: int,
    workout: Workout,
    revision: WorkoutRevision,
    *,
    mode: ValidationMode,
    now: datetime | None = None,
) -> SafetyContext:
    evaluated_at = now or utcnow()
    cutoff = evaluated_at - timedelta(days=FEEDBACK_FRESHNESS_DAYS)
    effective_date = (
        workout.scheduled_for
        if workout.local_schedule_status == "scheduled"
        else revision.suggested_for
    )
    include_recent = mode == "acceptance" or effective_date == evaluated_at.date()
    pre_filter = PreSessionFeedback.workout_id == workout.id
    if include_recent:
        pre_filter = or_(pre_filter, PreSessionFeedback.user_id == user_id)
    pre_feedback = list(
        session.scalars(
            select(PreSessionFeedback)
            .where(
                PreSessionFeedback.user_id == user_id,
                PreSessionFeedback.recorded_at >= cutoff,
                pre_filter,
            )
            .order_by(PreSessionFeedback.recorded_at, PreSessionFeedback.id)
        )
    )
    post_feedback = (
        list(
            session.scalars(
                select(PostSessionFeedback)
                .where(
                    PostSessionFeedback.user_id == user_id,
                    PostSessionFeedback.recorded_at >= cutoff,
                )
                .order_by(PostSessionFeedback.recorded_at, PostSessionFeedback.id)
            )
        )
        if include_recent
        else []
    )
    report = triage_feedback(pre_feedback, post_feedback)
    feedback_entries = [
        {"id": f"pre:{item.id}", "content_hash": item.content_hash} for item in pre_feedback
    ] + [{"id": f"post:{item.id}", "content_hash": item.content_hash} for item in post_feedback]
    if not feedback_entries:
        fingerprint = default_context_fingerprint(revision.content_hash)
    else:
        payload = json.dumps(
            {
                "as_of": evaluated_at.date().isoformat(),
                "effective_date": effective_date.isoformat() if effective_date else None,
                "feedback": feedback_entries,
                "mode": mode,
                "revision_content_hash": revision.content_hash,
                "rule_set_version": SAFETY_RULE_SET_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    return SafetyContext(
        fingerprint=fingerprint,
        feedback_ids=tuple(str(item["id"]) for item in feedback_entries),
        report=report,
    )
