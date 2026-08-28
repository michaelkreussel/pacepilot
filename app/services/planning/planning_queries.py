from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AthleteAvailability,
    AthleteGoal,
    AthletePlanningProfile,
    PerformanceAnchor,
    TrainingCycle,
    TrainingCycleRevision,
    TrainingCycleWeek,
    TrainingPlan,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    Workout,
)


@dataclass(frozen=True)
class PlanningProfileFact:
    experience_level: str | None
    preferred_long_run_weekday: int | None
    self_declared_reentry: bool
    constraint_note: str | None


@dataclass(frozen=True)
class GoalFact:
    id: int
    event_type: str
    event_name: str | None
    target_date: date | None
    status: str


@dataclass(frozen=True)
class AvailabilityFact:
    weekday: int
    available: bool
    available_minutes: int | None


@dataclass(frozen=True)
class PerformanceAnchorFact:
    id: int
    kind: str
    distance_m: float
    duration_s: float
    achieved_on: date
    reliable: bool
    notes: str | None


@dataclass(frozen=True)
class PlanningInputs:
    as_of: date
    profile: PlanningProfileFact | None
    goals: tuple[GoalFact, ...]
    availability: tuple[AvailabilityFact, ...]
    performance_anchors: tuple[PerformanceAnchorFact, ...]


@dataclass(frozen=True)
class TrainingCycleFact:
    id: int
    goal_id: int | None
    event_type: str
    start_date: date
    target_date: date
    status: str
    current_revision_id: int | None
    accepted_revision_id: int | None


@dataclass(frozen=True)
class TrainingCycleRevisionFact:
    id: int
    cycle_id: int
    parent_revision_id: int | None
    revision_number: int
    event_type: str
    start_date: date
    target_date: date
    planner_version: str
    knowledge_base_version: str
    input_fingerprint: str
    confidence: str
    phase_plan_json: list[dict[str, object]]
    assumptions_json: dict[str, object]
    impact_json: dict[str, object]
    validation_report_json: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class TrainingCycleState:
    cycle: TrainingCycleFact
    revision: TrainingCycleRevisionFact


@dataclass(frozen=True)
class CurrentTrainingPlanFact:
    id: int
    week_start: date
    status: str
    revision_id: int
    revision_number: int
    week_end: date
    planner_version: str
    knowledge_base_version: str
    input_fingerprint: str
    generation_context_json: dict[str, object]
    validation_report_json: dict[str, object]


@dataclass(frozen=True)
class TrainingCycleWeekFact:
    id: int
    cycle_revision_id: int
    training_plan_revision_id: int
    position: int
    week_start: date
    phase: str


@dataclass(frozen=True)
class TrainingPlanWorkoutFact:
    id: int
    plan_revision_id: int
    workout_id: int
    position: int
    role: str
    scheduled_for: date


@dataclass(frozen=True)
class PlannedWorkoutFact:
    id: int
    name: str


@dataclass(frozen=True)
class CycleWorkoutFact:
    membership: TrainingPlanWorkoutFact
    workout: PlannedWorkoutFact


@dataclass(frozen=True)
class TrainingCycleWeekDetail:
    membership: TrainingCycleWeekFact
    workouts: tuple[CycleWorkoutFact, ...]


def _goal_fact(goal: AthleteGoal) -> GoalFact:
    return GoalFact(
        id=goal.id,
        event_type=goal.event_type,
        event_name=goal.event_name,
        target_date=goal.target_date,
        status=goal.status,
    )


def _cycle_fact(cycle: TrainingCycle) -> TrainingCycleFact:
    return TrainingCycleFact(
        id=cycle.id,
        goal_id=cycle.goal_id,
        event_type=cycle.event_type,
        start_date=cycle.start_date,
        target_date=cycle.target_date,
        status=cycle.status,
        current_revision_id=cycle.current_revision_id,
        accepted_revision_id=cycle.accepted_revision_id,
    )


def _cycle_revision_fact(revision: TrainingCycleRevision) -> TrainingCycleRevisionFact:
    return TrainingCycleRevisionFact(
        id=revision.id,
        cycle_id=revision.cycle_id,
        parent_revision_id=revision.parent_revision_id,
        revision_number=revision.revision_number,
        event_type=revision.event_type,
        start_date=revision.start_date,
        target_date=revision.target_date,
        planner_version=revision.planner_version,
        knowledge_base_version=revision.knowledge_base_version,
        input_fingerprint=revision.input_fingerprint,
        confidence=revision.confidence,
        phase_plan_json=revision.phase_plan_json,
        assumptions_json=revision.assumptions_json,
        impact_json=revision.impact_json,
        validation_report_json=revision.validation_report_json,
        created_at=revision.created_at,
    )


def get_planning_profile(session: Session, user_id: int) -> PlanningProfileFact | None:
    profile = session.get(AthletePlanningProfile, user_id)
    if profile is None:
        return None
    return PlanningProfileFact(
        experience_level=profile.experience_level,
        preferred_long_run_weekday=profile.preferred_long_run_weekday,
        self_declared_reentry=profile.self_declared_reentry,
        constraint_note=profile.constraint_note,
    )


def list_goals(
    session: Session, user_id: int, *, status: str | None = None
) -> tuple[GoalFact, ...]:
    query = select(AthleteGoal).where(AthleteGoal.user_id == user_id)
    if status is not None:
        query = query.where(AthleteGoal.status == status)
    goals = session.scalars(query.order_by(AthleteGoal.target_date, AthleteGoal.id))
    return tuple(_goal_fact(goal) for goal in goals)


def get_active_goal(
    session: Session,
    user_id: int,
    *,
    goal_id: int | None = None,
    event_type: str | None = None,
) -> GoalFact | None:
    query = select(AthleteGoal).where(
        AthleteGoal.user_id == user_id,
        AthleteGoal.status == "active",
    )
    if goal_id is not None:
        query = query.where(AthleteGoal.id == goal_id)
    if event_type is not None:
        query = query.where(AthleteGoal.event_type == event_type)
    goal = session.scalar(query.order_by(AthleteGoal.target_date, AthleteGoal.id))
    return _goal_fact(goal) if goal is not None else None


def list_availability(
    session: Session, user_id: int, *, available_only: bool = False
) -> tuple[AvailabilityFact, ...]:
    query = select(AthleteAvailability).where(AthleteAvailability.user_id == user_id)
    if available_only:
        query = query.where(AthleteAvailability.available.is_(True))
    rows = session.scalars(query.order_by(AthleteAvailability.weekday))
    return tuple(
        AvailabilityFact(
            weekday=row.weekday,
            available=row.available,
            available_minutes=row.available_minutes,
        )
        for row in rows
    )


def list_performance_anchors(session: Session, user_id: int) -> tuple[PerformanceAnchorFact, ...]:
    anchors = session.scalars(
        select(PerformanceAnchor)
        .where(PerformanceAnchor.user_id == user_id)
        .order_by(PerformanceAnchor.achieved_on, PerformanceAnchor.id)
    )
    return tuple(
        PerformanceAnchorFact(
            id=anchor.id,
            kind=anchor.kind,
            distance_m=anchor.distance_m,
            duration_s=anchor.duration_s,
            achieved_on=anchor.achieved_on,
            reliable=anchor.reliable,
            notes=anchor.notes,
        )
        for anchor in anchors
    )


def get_planning_inputs(session: Session, user_id: int, *, as_of: date) -> PlanningInputs:
    return PlanningInputs(
        as_of=as_of,
        profile=get_planning_profile(session, user_id),
        goals=list_goals(session, user_id, status="active"),
        availability=list_availability(session, user_id, available_only=True),
        performance_anchors=list_performance_anchors(session, user_id),
    )


def list_training_cycles(session: Session, user_id: int) -> tuple[TrainingCycleFact, ...]:
    cycles = session.scalars(
        select(TrainingCycle)
        .where(TrainingCycle.user_id == user_id)
        .order_by(TrainingCycle.target_date.desc(), TrainingCycle.id.desc())
    )
    return tuple(_cycle_fact(cycle) for cycle in cycles)


def _training_cycle_state(
    session: Session,
    user_id: int,
    cycle_id: int,
    *,
    accepted: bool,
) -> TrainingCycleState | None:
    cycle = session.scalar(
        select(TrainingCycle).where(
            TrainingCycle.id == cycle_id,
            TrainingCycle.user_id == user_id,
        )
    )
    if cycle is None:
        return None
    revision_id = cycle.accepted_revision_id if accepted else cycle.current_revision_id
    if revision_id is None:
        return None
    revision = session.scalar(
        select(TrainingCycleRevision).where(
            TrainingCycleRevision.id == revision_id,
            TrainingCycleRevision.cycle_id == cycle.id,
            TrainingCycleRevision.owner_user_id == user_id,
        )
    )
    if revision is None:
        return None
    return TrainingCycleState(
        cycle=_cycle_fact(cycle),
        revision=_cycle_revision_fact(revision),
    )


def get_current_training_cycle(
    session: Session, user_id: int, cycle_id: int
) -> TrainingCycleState | None:
    return _training_cycle_state(session, user_id, cycle_id, accepted=False)


def get_accepted_training_cycle(
    session: Session, user_id: int, cycle_id: int
) -> TrainingCycleState | None:
    return _training_cycle_state(session, user_id, cycle_id, accepted=True)


def list_accepted_training_cycles(session: Session, user_id: int) -> tuple[TrainingCycleState, ...]:
    rows = session.execute(
        select(TrainingCycle, TrainingCycleRevision)
        .join(
            TrainingCycleRevision,
            (TrainingCycleRevision.id == TrainingCycle.accepted_revision_id)
            & (TrainingCycleRevision.cycle_id == TrainingCycle.id),
        )
        .where(
            TrainingCycle.user_id == user_id,
            TrainingCycleRevision.owner_user_id == user_id,
        )
        .order_by(TrainingCycle.target_date.desc(), TrainingCycle.id.desc())
    )
    return tuple(
        TrainingCycleState(
            cycle=_cycle_fact(cycle),
            revision=_cycle_revision_fact(revision),
        )
        for cycle, revision in rows
    )


def list_current_training_plans(
    session: Session,
    user_id: int,
    *,
    starts_on: date,
    ends_on: date,
) -> tuple[CurrentTrainingPlanFact, ...]:
    rows = session.execute(
        select(TrainingPlan, TrainingPlanRevision)
        .join(
            TrainingPlanRevision,
            (TrainingPlanRevision.id == TrainingPlan.current_revision_id)
            & (TrainingPlanRevision.plan_id == TrainingPlan.id),
        )
        .where(
            TrainingPlan.user_id == user_id,
            TrainingPlanRevision.owner_user_id == user_id,
            TrainingPlan.week_start >= starts_on,
            TrainingPlan.week_start <= ends_on,
        )
        .order_by(TrainingPlan.week_start, TrainingPlan.id)
    )
    return tuple(
        CurrentTrainingPlanFact(
            id=plan.id,
            week_start=plan.week_start,
            status=plan.status,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            week_end=revision.week_end,
            planner_version=revision.planner_version,
            knowledge_base_version=revision.knowledge_base_version,
            input_fingerprint=revision.input_fingerprint,
            generation_context_json=revision.generation_context_json,
            validation_report_json=revision.validation_report_json,
        )
        for plan, revision in rows
    )


def list_training_cycle_week_details(
    session: Session, user_id: int, revision_id: int
) -> tuple[TrainingCycleWeekDetail, ...]:
    weeks = session.scalars(
        select(TrainingCycleWeek)
        .where(
            TrainingCycleWeek.owner_user_id == user_id,
            TrainingCycleWeek.cycle_revision_id == revision_id,
        )
        .order_by(TrainingCycleWeek.position)
    )
    details = []
    for week in weeks:
        rows = session.execute(
            select(TrainingPlanWorkout, Workout)
            .join(Workout, Workout.id == TrainingPlanWorkout.workout_id)
            .join(
                TrainingPlanRevision,
                TrainingPlanRevision.id == TrainingPlanWorkout.plan_revision_id,
            )
            .where(
                TrainingPlanWorkout.plan_revision_id == week.training_plan_revision_id,
                TrainingPlanWorkout.owner_user_id == user_id,
                Workout.user_id == user_id,
                Workout.deleted_at.is_(None),
            )
            .order_by(TrainingPlanWorkout.position)
        )
        details.append(
            TrainingCycleWeekDetail(
                membership=TrainingCycleWeekFact(
                    id=week.id,
                    cycle_revision_id=week.cycle_revision_id,
                    training_plan_revision_id=week.training_plan_revision_id,
                    position=week.position,
                    week_start=week.week_start,
                    phase=week.phase,
                ),
                workouts=tuple(
                    CycleWorkoutFact(
                        membership=TrainingPlanWorkoutFact(
                            id=membership.id,
                            plan_revision_id=membership.plan_revision_id,
                            workout_id=membership.workout_id,
                            position=membership.position,
                            role=membership.role,
                            scheduled_for=membership.scheduled_for,
                        ),
                        workout=PlannedWorkoutFact(id=workout.id, name=workout.name),
                    )
                    for membership, workout in rows
                ),
            )
        )
    return tuple(details)
