import hashlib
from dataclasses import replace
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    TrainingPlan,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    User,
    Workout,
)
from app.services.planning.planning_commands import WeekPlanRevisionInput
from app.services.planning.weekly_planner import (
    DayAvailability,
    WeeklyPlanCandidate,
    plan_shadow_week,
)
from app.services.planning.workout_definition import workout_metrics
from app.services.planning.workout_service import WorkoutService

PLAN_SOURCE = "coach_weekly_plan"


class WeeklyPlanPersistenceError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WeeklyPlanAcceptanceError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class WeekPlanRevisionError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def revise_week_plan(
    session: Session,
    user: User,
    *,
    plan_id: int,
    data: WeekPlanRevisionInput,
    source_assistant_message_id: int | None = None,
) -> TrainingPlanRevision:
    plan = session.scalar(
        select(TrainingPlan).where(TrainingPlan.id == plan_id, TrainingPlan.user_id == user.id)
    )
    if plan is None:
        raise WeekPlanRevisionError("Wochenplan nicht gefunden.", code="plan.not_found")
    availability = (
        tuple(
            DayAvailability(
                weekday=item.weekday, available_minutes=int(item.available_minutes or 0)
            )
            for item in data.availability
            if item.available
        )
        if data.availability is not None
        else None
    )
    candidate = plan_shadow_week(
        session,
        user,
        week_start=data.week_start or plan.week_start,
        as_of=data.as_of or date.today(),
        availability=availability,
    )
    return persist_week_candidate(
        session,
        user,
        candidate,
        source_assistant_message_id=source_assistant_message_id,
    )


def accept_training_plan_revision(
    session: Session,
    user: User,
    *,
    plan_id: int,
    revision_id: int,
) -> TrainingPlanRevision:
    plan = session.scalar(
        select(TrainingPlan).where(TrainingPlan.id == plan_id, TrainingPlan.user_id == user.id)
    )
    revision = session.scalar(
        select(TrainingPlanRevision).where(
            TrainingPlanRevision.id == revision_id,
            TrainingPlanRevision.plan_id == plan_id,
            TrainingPlanRevision.owner_user_id == user.id,
        )
    )
    if plan is None or revision is None:
        raise WeeklyPlanAcceptanceError("Planrevision nicht gefunden.", code="plan.not_found")
    if plan.current_revision_id != revision.id:
        raise WeeklyPlanAcceptanceError(
            "Diese Planrevision ist nicht mehr die aktuelle Vorschau.",
            code="plan.revision_stale",
        )
    plan.accepted_revision_id = revision.id
    session.commit()
    return revision


def persist_week_candidate(
    session: Session,
    user: User,
    candidate: WeeklyPlanCandidate,
    *,
    source_assistant_message_id: int | None = None,
    commit: bool = True,
) -> TrainingPlanRevision:
    last_error: IntegrityError | None = None
    for _attempt in range(2):
        try:
            return _persist_week_candidate(
                session,
                user,
                candidate,
                source_assistant_message_id=source_assistant_message_id,
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
    *,
    source_assistant_message_id: int | None = None,
) -> TrainingPlanRevision:
    return _persist_week_candidate(
        session,
        user,
        candidate,
        source_assistant_message_id=source_assistant_message_id,
        commit=False,
        rollback_on_error=False,
    )


def _persist_week_candidate(
    session: Session,
    user: User,
    candidate: WeeklyPlanCandidate,
    *,
    source_assistant_message_id: int | None,
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
        source_assistant_message_id=source_assistant_message_id,
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
    try:
        for position, item in enumerate(candidate.sessions):
            if item.data is None or item.metadata is None:
                raise WeeklyPlanPersistenceError(
                    "Dem Kandidaten fehlt die ausführbare Vorschau.", code="plan.candidate_invalid"
                )
            data = item.data
            if (
                item.template_id != item.metadata.template_id
                or item.load_estimate_json != item.metadata.load_estimate_json
                or item.scheduled_for != data.scheduled_for
                or -(-int(workout_metrics(data.definition).duration_seconds) // 60)
                != item.planned_minutes
            ):
                raise WeeklyPlanPersistenceError(
                    "Vorschau und Metadaten stimmen nicht überein.", code="plan.candidate_invalid"
                )
            workout_service.validate(data)
            request_fingerprint = hashlib.sha256(
                f"{candidate.input_fingerprint}:{position}:{item.template_id}".encode()
            ).hexdigest()
            metadata = replace(
                item.metadata,
                guidance_json={
                    **(item.metadata.guidance_json or {}),
                    "rationale": item.rationale,
                    "plan_revision_id": plan_revision.id,
                },
                generation_context_json={
                    **(item.metadata.generation_context_json or {}),
                    "plan_revision_id": plan_revision.id,
                    "plan_input_fingerprint": candidate.input_fingerprint,
                    "scheduled_for": item.scheduled_for.isoformat(),
                },
                source_type=PLAN_SOURCE,
                rule_set_version=candidate.planner_version,
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
