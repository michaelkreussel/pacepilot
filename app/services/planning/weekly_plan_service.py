import hashlib
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DEFERRED_QUALITY_TEMPLATE_IDS, deferred_quality_templates_enabled
from app.models import (
    TrainingPlan,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    User,
    Workout,
)
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.validator import WorkoutInput
from app.services.planning.weekly_planner import WeeklyPlanCandidate
from app.services.planning.workout_revision import RevisionMetadata
from app.services.planning.workout_service import WorkoutService
from app.services.planning.workout_templates import (
    TemplateEligibilityContext,
    TemplateParameters,
    expand_workout_template,
)

PLAN_SOURCE = "coach_weekly_plan"


class WeeklyPlanPersistenceError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def persist_week_candidate(
    session: Session,
    user: User,
    candidate: WeeklyPlanCandidate,
    *,
    commit: bool = True,
) -> TrainingPlanRevision:
    last_error: IntegrityError | None = None
    for _attempt in range(2):
        try:
            return _persist_week_candidate(
                session,
                user,
                candidate,
                commit=commit,
                rollback_on_error=True,
            )
        except IntegrityError as exc:
            session.rollback()
            winner = session.scalar(
                select(TrainingPlanRevision)
                .join(TrainingPlan, TrainingPlan.id == TrainingPlanRevision.plan_id)
                .where(
                    TrainingPlan.user_id == user.id,
                    TrainingPlan.week_start == candidate.week_start,
                    TrainingPlanRevision.input_fingerprint == candidate.input_fingerprint,
                )
            )
            if winner is not None:
                winning_plan = session.get(TrainingPlan, winner.plan_id)
                if winning_plan is not None and winning_plan.current_revision_id != winner.id:
                    winning_plan.current_revision_id = winner.id
                    if commit:
                        session.commit()
                return winner
            last_error = exc
    assert last_error is not None
    raise last_error


def persist_week_candidate_in_transaction(
    session: Session,
    user: User,
    candidate: WeeklyPlanCandidate,
) -> TrainingPlanRevision:
    return _persist_week_candidate(
        session,
        user,
        candidate,
        commit=False,
        rollback_on_error=False,
    )


def _persist_week_candidate(
    session: Session,
    user: User,
    candidate: WeeklyPlanCandidate,
    *,
    commit: bool,
    rollback_on_error: bool,
) -> TrainingPlanRevision:
    if not candidate.validation_report.get("valid"):
        raise WeeklyPlanPersistenceError(
            "Nur ein vollständig validierter Wochenkandidat kann gespeichert werden.",
            code="plan.candidate_invalid",
        )
    plan = session.scalar(
        select(TrainingPlan).where(
            TrainingPlan.user_id == user.id,
            TrainingPlan.week_start == candidate.week_start,
        )
    )
    if plan is not None:
        existing = session.scalar(
            select(TrainingPlanRevision).where(
                TrainingPlanRevision.plan_id == plan.id,
                TrainingPlanRevision.input_fingerprint == candidate.input_fingerprint,
            )
        )
        if existing is not None:
            if plan.current_revision_id != existing.id:
                plan.current_revision_id = existing.id
                if commit:
                    session.commit()
            return existing
    else:
        plan = TrainingPlan(user_id=user.id, week_start=candidate.week_start)
        session.add(plan)
        session.flush()

    revision_number = (
        session.scalar(
            select(func.max(TrainingPlanRevision.revision_number)).where(
                TrainingPlanRevision.plan_id == plan.id
            )
        )
        or 0
    ) + 1
    plan_revision = TrainingPlanRevision(
        plan_id=plan.id,
        owner_user_id=user.id,
        revision_number=revision_number,
        week_start=candidate.week_start,
        week_end=candidate.week_end,
        planner_version=candidate.planner_version,
        knowledge_base_version=candidate.knowledge_base_version,
        input_fingerprint=candidate.input_fingerprint,
        generation_context_json=candidate.generation_context,
        validation_report_json=candidate.validation_report,
    )
    session.add(plan_revision)
    session.flush()
    plan.current_revision_id = plan_revision.id

    workout_service = WorkoutService(session, user)
    context = candidate.generation_context.get("baseline")
    consistent_weeks = (
        int(context.get("consistent_running_weeks", 0)) if isinstance(context, dict) else 0
    )
    observed_runs = (
        round(float(context.get("observed_runs_per_week", 0))) if isinstance(context, dict) else 0
    )
    history_gates = candidate.generation_context.get("history_gates")
    if isinstance(history_gates, dict):
        consistent_weeks = int(
            history_gates.get("effective_consistent_running_weeks", consistent_weeks)
        )
        observed_runs = int(history_gates.get("effective_runs_per_week", observed_runs))
    deferred_quality = candidate.generation_context.get("deferred_quality")
    deferred_quality_recorded = (
        isinstance(deferred_quality, dict) and deferred_quality.get("development_override") is True
    )
    try:
        for position, item in enumerate(candidate.sessions):
            facts: set[str] = set()
            if item.role == "long_run":
                facts.add("sufficient_recent_long_run_baseline")
            elif item.role == "strides":
                facts.add("familiar_with_relaxed_fast_running")
            elif item.template_id in DEFERRED_QUALITY_TEMPLATE_IDS:
                facts.update(
                    {
                        "reliable_intensity_model",
                        "reliable_current_performance_model",
                        "quality_density_validation",
                    }
                )
            is_deferred_quality = item.template_id in DEFERRED_QUALITY_TEMPLATE_IDS
            expanded = expand_workout_template(
                item.template_id,
                None
                if item.role == "strides" or is_deferred_quality
                else TemplateParameters(duration_minutes=item.planned_minutes),
                eligibility=TemplateEligibilityContext(
                    consistent_running_weeks=consistent_weeks,
                    runs_per_week=max(observed_runs, 1),
                    available_minutes=item.planned_minutes,
                    facts=facts,
                ),
                allow_deferred_quality=(
                    is_deferred_quality
                    and deferred_quality_recorded
                    and deferred_quality_templates_enabled()
                ),
            )
            data = WorkoutInput(
                name=expanded.name,
                sport="running",
                scheduled_for=item.scheduled_for,
                description="Deterministischer Wochenplan-Vorschlag von PacePilot.",
                definition=expanded.definition,
                definition_version=expanded.definition_version,
            )
            request_fingerprint = hashlib.sha256(
                f"{candidate.input_fingerprint}:{position}:{item.template_id}".encode()
            ).hexdigest()
            metadata = RevisionMetadata(
                purpose=expanded.purpose,
                guidance_json={
                    **expanded.guidance,
                    "rationale": item.rationale,
                    "plan_revision_id": plan_revision.id,
                },
                load_estimate_json=expanded.load_estimate.model_dump(mode="json"),
                validation_report_json={
                    "valid": True,
                    "issues": [],
                    "rule_set_version": candidate.planner_version,
                },
                generation_context_json={
                    "schema_version": "weekly_plan_workout_context.v1",
                    "plan_revision_id": plan_revision.id,
                    "plan_input_fingerprint": candidate.input_fingerprint,
                    "scheduled_for": item.scheduled_for.isoformat(),
                },
                source_type=PLAN_SOURCE,
                generator_version=expanded.generator_version,
                template_id=expanded.template_id,
                template_version=expanded.template_version,
                rule_set_version=candidate.planner_version,
                knowledge_base_version=get_knowledge_registry().version,
                edit_source="generator",
            )
            workout = workout_service.create_proposal(
                data,
                metadata,
                idempotency_key=(
                    f"weekly-plan:{plan.id}:{revision_number}:{position}:create-proposal"
                ),
                request_fingerprint=request_fingerprint,
                commit=False,
            )
            session.add(
                TrainingPlanWorkout(
                    plan_revision_id=plan_revision.id,
                    workout_id=workout.id,
                    owner_user_id=user.id,
                    position=position,
                    role=item.role,
                    scheduled_for=item.scheduled_for,
                )
            )
        if commit:
            session.commit()
    except Exception:
        if rollback_on_error:
            session.rollback()
        raise
    return plan_revision


def plan_proposals_between(
    session: Session, user_id: int, starts_on: date, ends_on: date
) -> list[tuple[TrainingPlanWorkout, Workout]]:
    return list(
        session.execute(
            select(TrainingPlanWorkout, Workout)
            .join(
                TrainingPlanRevision,
                TrainingPlanRevision.id == TrainingPlanWorkout.plan_revision_id,
            )
            .join(TrainingPlan, TrainingPlan.id == TrainingPlanRevision.plan_id)
            .join(Workout, Workout.id == TrainingPlanWorkout.workout_id)
            .where(
                TrainingPlan.user_id == user_id,
                TrainingPlan.current_revision_id == TrainingPlanRevision.id,
                TrainingPlanWorkout.owner_user_id == user_id,
                Workout.user_id == user_id,
                TrainingPlanWorkout.scheduled_for >= starts_on,
                TrainingPlanWorkout.scheduled_for <= ends_on,
                Workout.deleted_at.is_(None),
                Workout.approval_status == "proposed",
                Workout.accepted_revision_id.is_(None),
                Workout.scheduled_for.is_(None),
                Workout.local_schedule_status == "unscheduled",
            )
            .order_by(TrainingPlanWorkout.scheduled_for, TrainingPlanWorkout.position)
        ).tuples()
    )
