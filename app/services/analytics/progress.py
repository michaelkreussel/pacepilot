from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Activity, PostSessionFeedback, PreSessionFeedback
from app.repositories.activities import activities_between
from app.services.analytics.health_trends import (
    get_current_recovery_state,
    preferred_readiness,
)
from app.services.analytics.training_trends import TrainingSummary, get_training_summary
from app.services.planning.planning_queries import (
    CycleWorkoutFact,
    GoalFact,
    TrainingCycleState,
    get_active_goal,
    list_accepted_training_cycles,
    list_goals,
    list_training_cycle_week_details,
)

LinkageConfidence = Literal["unavailable", "low", "medium", "high"]


class ProgressReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ProgressPeriod:
    start: date
    end: date
    days: int


@dataclass(frozen=True)
class ProgressGoal:
    id: int
    event_type: str
    event_name: str | None
    target_date: date | None
    days_to_target: int | None


@dataclass(frozen=True)
class ProgressPlan:
    cycle_id: int
    accepted_revision_id: int
    phase: str | None
    phase_week_start: date | None
    confidence: str


@dataclass(frozen=True)
class ProgressComparison:
    planned_sessions: int | None
    completed_planned_sessions: int | None
    observed_activity_sessions: int | None
    planned_duration_s: float | None
    completed_duration_s: float | None
    planned_distance_m: float | None
    completed_distance_m: float | None
    adherence_percent: float | None


@dataclass(frozen=True)
class ProgressMatching:
    matched_sessions: int | None
    unmatched_planned_sessions: int | None
    unmatched_activity_sessions: int | None
    linkage_confidence: LinkageConfidence


@dataclass(frozen=True)
class ProgressFeedback:
    pre_session_reports: int
    post_session_reports: int
    completion_percent: float | None
    pain_reports: int
    stopped_sessions: int
    illness_signals: tuple[str, ...]
    interruptions: tuple[str, ...]


@dataclass(frozen=True)
class ProgressCoverage:
    activity_sync_status: str
    activity_period_complete: bool
    activity_history_complete: bool
    oldest_synced_date: date | None
    newest_synced_date: date | None
    planned_duration_complete: bool | None
    planned_distance_complete: bool | None
    completed_duration_complete: bool | None
    completed_distance_complete: bool | None


@dataclass(frozen=True)
class ProgressTrend:
    current_sessions: int
    previous_sessions: int
    session_change: int
    current_duration_s: float | None
    previous_duration_s: float | None
    duration_change_percent: float | None
    consistency_percent: float


@dataclass(frozen=True)
class ProgressRecovery:
    source: str | None
    day: date | None
    score: float | None
    label: str | None
    confidence: float | None
    constraints: tuple[str, ...]


@dataclass(frozen=True)
class ProgressResult:
    period: ProgressPeriod
    goal: ProgressGoal | None
    plan: ProgressPlan | None
    comparison: ProgressComparison
    matching: ProgressMatching
    feedback: ProgressFeedback
    coverage: ProgressCoverage
    trend: ProgressTrend | None
    recovery: ProgressRecovery | None
    uncertainty: tuple[str, ...]


@dataclass(frozen=True)
class _Volume:
    duration_s: float | None
    distance_m: float | None
    duration_complete: bool
    distance_complete: bool


def get_progress(
    session: Session,
    user_id: int,
    *,
    as_of: date,
    days: int = 28,
    goal_id: int | None = None,
) -> ProgressResult:
    if not 7 <= days <= 84:
        raise ValueError("days must be between 7 and 84")

    period = ProgressPeriod(as_of - timedelta(days=days - 1), as_of, days)
    goal, cycle = _select_goal_and_cycle(session, user_id, period, goal_id)
    plan, planned = _plan_context(session, user_id, cycle, period)
    summary = get_training_summary(session, user_id, days=days, as_of=as_of)
    source_available = summary.data_status in {"ok", "empty", "partial"}
    period_complete = _period_complete(summary, period.start, period.end)
    activities = activities_between(
        session,
        user_id,
        datetime.combine(period.start, time.min),
        datetime.combine(period.end + timedelta(days=1), time.min),
    )

    planned_ids = {item.workout.id for item in planned}
    matched = [item for item in activities if item.workout_id in planned_ids]
    matched_ids = {item.workout_id for item in matched}
    unmatched_activities = [item for item in activities if item.workout_id not in planned_ids]
    planned_volume = _planned_volume(planned) if plan is not None else None
    completed_volume = _activity_volume(matched) if period_complete and plan is not None else None
    matched_count = len(matched_ids) if period_complete and plan is not None else None
    planned_count = len(planned) if plan is not None else None
    observed_count = len(activities) if period_complete else None
    unmatched_planned = (
        planned_count - matched_count
        if planned_count is not None and matched_count is not None
        else None
    )
    unmatched_observed = len(unmatched_activities) if period_complete and plan is not None else None
    adherence = (
        round(matched_count * 100 / planned_count, 1)
        if period_complete
        and planned_count is not None
        and planned_count > 0
        and matched_count is not None
        else None
    )

    feedback = _feedback_context(session, user_id, period)
    recovery = _recovery_context(session, user_id, as_of, feedback)
    trend = _trend_context(session, user_id, period, summary) if source_available else None
    linkage_confidence = _linkage_confidence(
        source_available=source_available,
        period_complete=period_complete,
        planned_count=planned_count,
        observed_count=observed_count,
        matched_count=matched_count,
        unmatched_observed=unmatched_observed,
    )
    uncertainty = _uncertainty(
        plan=plan,
        source_available=source_available,
        period_complete=period_complete,
        linkage_confidence=linkage_confidence,
        trend=trend,
        recovery=recovery,
        planned_volume=planned_volume,
    )

    return ProgressResult(
        period=period,
        goal=_goal_progress(goal, as_of),
        plan=plan,
        comparison=ProgressComparison(
            planned_sessions=planned_count,
            completed_planned_sessions=matched_count,
            observed_activity_sessions=observed_count,
            planned_duration_s=planned_volume.duration_s if planned_volume else None,
            completed_duration_s=completed_volume.duration_s if completed_volume else None,
            planned_distance_m=planned_volume.distance_m if planned_volume else None,
            completed_distance_m=completed_volume.distance_m if completed_volume else None,
            adherence_percent=adherence,
        ),
        matching=ProgressMatching(
            matched_sessions=matched_count,
            unmatched_planned_sessions=unmatched_planned,
            unmatched_activity_sessions=unmatched_observed,
            linkage_confidence=linkage_confidence,
        ),
        feedback=feedback,
        coverage=ProgressCoverage(
            activity_sync_status=summary.data_status,
            activity_period_complete=period_complete,
            activity_history_complete=summary.history_complete,
            oldest_synced_date=summary.oldest_synced_date,
            newest_synced_date=summary.newest_synced_date,
            planned_duration_complete=(
                planned_volume.duration_complete if planned_volume else None
            ),
            planned_distance_complete=(
                planned_volume.distance_complete if planned_volume else None
            ),
            completed_duration_complete=(
                completed_volume.duration_complete if completed_volume else None
            ),
            completed_distance_complete=(
                completed_volume.distance_complete if completed_volume else None
            ),
        ),
        trend=trend,
        recovery=recovery,
        uncertainty=uncertainty,
    )


def _select_goal_and_cycle(
    session: Session,
    user_id: int,
    period: ProgressPeriod,
    goal_id: int | None,
) -> tuple[GoalFact | None, TrainingCycleState | None]:
    if goal_id is not None:
        goal = get_active_goal(session, user_id, goal_id=goal_id)
        if goal is None:
            raise ProgressReferenceError("goal not found")
    else:
        goal = None

    cycles = [
        state
        for state in list_accepted_training_cycles(session, user_id)
        if state.cycle.status == "active"
        and state.cycle.start_date <= period.end
        and state.cycle.target_date >= period.start
        and (goal_id is None or state.cycle.goal_id == goal_id)
    ]
    cycle = min(cycles, key=lambda item: (item.cycle.target_date, item.cycle.id), default=None)
    if goal is None and cycle is not None and cycle.cycle.goal_id is not None:
        goal = next(
            (item for item in list_goals(session, user_id) if item.id == cycle.cycle.goal_id),
            None,
        )
    if goal is None and cycle is None:
        goals = list_goals(session, user_id, status="active")
        goal = goals[0] if goals else None
    return goal, cycle


def _plan_context(
    session: Session,
    user_id: int,
    cycle: TrainingCycleState | None,
    period: ProgressPeriod,
) -> tuple[ProgressPlan | None, tuple[CycleWorkoutFact, ...]]:
    if cycle is None:
        return None, ()
    details = list_training_cycle_week_details(
        session,
        user_id,
        cycle.revision.id,
        starts_on=period.start,
        ends_on=period.end,
    )
    current_week = next(
        (
            item
            for item in details
            if item.membership.week_start
            <= period.end
            <= item.membership.week_start + timedelta(days=6)
        ),
        None,
    )
    planned = tuple(
        workout
        for detail in details
        for workout in detail.workouts
        if period.start <= workout.membership.scheduled_for <= period.end
    )
    return (
        ProgressPlan(
            cycle_id=cycle.cycle.id,
            accepted_revision_id=cycle.revision.id,
            phase=current_week.membership.phase if current_week else None,
            phase_week_start=current_week.membership.week_start if current_week else None,
            confidence=cycle.revision.confidence,
        ),
        planned,
    )


def _goal_progress(goal: GoalFact | None, as_of: date) -> ProgressGoal | None:
    if goal is None:
        return None
    return ProgressGoal(
        id=goal.id,
        event_type=goal.event_type,
        event_name=goal.event_name,
        target_date=goal.target_date,
        days_to_target=(goal.target_date - as_of).days if goal.target_date else None,
    )


def _planned_volume(planned: tuple[CycleWorkoutFact, ...]) -> _Volume:
    volume = _volume(
        [item.workout.duration_seconds for item in planned],
        [item.workout.distance_meters for item in planned],
    )
    return _Volume(
        duration_s=volume.duration_s,
        distance_m=volume.distance_m,
        duration_complete=(
            volume.duration_complete and all(item.workout.duration_complete for item in planned)
        ),
        distance_complete=(
            volume.distance_complete and all(item.workout.distance_complete for item in planned)
        ),
    )


def _activity_volume(activities: list[Activity]) -> _Volume:
    return _volume(
        [item.duration_s for item in activities],
        [item.distance_m for item in activities],
    )


def _volume(durations: list[float | None], distances: list[float | None]) -> _Volume:
    return _Volume(
        duration_s=_known_sum(durations),
        distance_m=_known_sum(distances),
        duration_complete=all(value is not None for value in durations),
        distance_complete=all(value is not None for value in distances),
    )


def _known_sum(values: list[float | None]) -> float | None:
    if not values:
        return 0.0
    if not any(value is not None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 2)


def _period_complete(summary: TrainingSummary, start: date, end: date) -> bool:
    return (
        summary.data_status in {"ok", "empty"}
        and summary.oldest_synced_date is not None
        and summary.oldest_synced_date <= start
        and summary.newest_synced_date is not None
        and summary.newest_synced_date >= end
    )


def _feedback_context(
    session: Session,
    user_id: int,
    period: ProgressPeriod,
) -> ProgressFeedback:
    start = datetime.combine(period.start, time.min)
    through = datetime.combine(period.end, time.max)
    pre = list(
        session.scalars(
            select(PreSessionFeedback).where(
                PreSessionFeedback.user_id == user_id,
                PreSessionFeedback.recorded_at >= start,
                PreSessionFeedback.recorded_at <= through,
            )
        )
    )
    post = list(
        session.scalars(
            select(PostSessionFeedback).where(
                PostSessionFeedback.user_id == user_id,
                PostSessionFeedback.recorded_at >= start,
                PostSessionFeedback.recorded_at <= through,
            )
        )
    )
    completions = [item.completion_percent for item in post if item.completion_percent is not None]
    illness = tuple(sorted({item.illness_signal for item in pre if item.illness_signal != "none"}))
    interruptions = []
    for item in post:
        if item.stopped_reason and item.stopped_reason not in interruptions:
            interruptions.append(item.stopped_reason)
    return ProgressFeedback(
        pre_session_reports=len(pre),
        post_session_reports=len(post),
        completion_percent=(round(sum(completions) / len(completions), 1) if completions else None),
        pain_reports=sum(item.pain_present for item in (*pre, *post)),
        stopped_sessions=sum(bool(item.stopped_reason) for item in post),
        illness_signals=illness,
        interruptions=tuple(interruptions[:10]),
    )


def _recovery_context(
    session: Session,
    user_id: int,
    as_of: date,
    feedback: ProgressFeedback,
) -> ProgressRecovery | None:
    state = get_current_recovery_state(session, user_id, as_of=as_of)
    readiness = preferred_readiness(state)
    constraints = []
    if (
        state.sleep_seconds is not None
        and state.sleep_need_seconds is not None
        and state.sleep_need_seconds > 0
        and state.sleep_seconds / state.sleep_need_seconds < 0.8
    ):
        constraints.append("sleep_shortfall")
    if feedback.pain_reports:
        constraints.append("pain_reported")
    if feedback.stopped_sessions:
        constraints.append("session_stopped")
    constraints.extend(f"illness:{signal}" for signal in feedback.illness_signals)
    if state.pacepilot_readiness_limiter and state.pacepilot_readiness_limiter not in constraints:
        constraints.append(state.pacepilot_readiness_limiter)
    if readiness is None and not constraints:
        return None
    return ProgressRecovery(
        source=readiness.source if readiness else None,
        day=readiness.day if readiness else state.health_day or state.fitness_day,
        score=readiness.score if readiness else None,
        label=readiness.label if readiness else None,
        confidence=readiness.confidence if readiness else None,
        constraints=tuple(constraints),
    )


def _trend_context(
    session: Session,
    user_id: int,
    period: ProgressPeriod,
    current: TrainingSummary,
) -> ProgressTrend | None:
    previous_end = period.start - timedelta(days=1)
    previous = get_training_summary(session, user_id, days=period.days, as_of=previous_end)
    if not _period_complete(previous, previous.start, previous.end) or not _period_complete(
        current, period.start, period.end
    ):
        return None
    duration_change = None
    if (
        current.total_duration_s is not None
        and previous.total_duration_s is not None
        and previous.total_duration_s > 0
    ):
        duration_change = round(
            (current.total_duration_s - previous.total_duration_s)
            / previous.total_duration_s
            * 100,
            1,
        )
    return ProgressTrend(
        current_sessions=current.workouts,
        previous_sessions=previous.workouts,
        session_change=current.workouts - previous.workouts,
        current_duration_s=current.total_duration_s,
        previous_duration_s=previous.total_duration_s,
        duration_change_percent=duration_change,
        consistency_percent=current.consistency_percent,
    )


def _linkage_confidence(
    *,
    source_available: bool,
    period_complete: bool,
    planned_count: int | None,
    observed_count: int | None,
    matched_count: int | None,
    unmatched_observed: int | None,
) -> LinkageConfidence:
    if (
        not source_available
        or planned_count in {None, 0}
        or observed_count in {None, 0}
        or matched_count is None
    ):
        return "unavailable"
    if not period_complete or matched_count == 0:
        return "low"
    if unmatched_observed == 0:
        return "high"
    return "medium"


def _uncertainty(
    *,
    plan: ProgressPlan | None,
    source_available: bool,
    period_complete: bool,
    linkage_confidence: LinkageConfidence,
    trend: ProgressTrend | None,
    recovery: ProgressRecovery | None,
    planned_volume: _Volume | None,
) -> tuple[str, ...]:
    items = []
    if plan is None:
        items.append("accepted_plan_unavailable")
    if not source_available:
        items.append("activity_sync_unavailable")
    elif not period_complete:
        items.append("activity_sync_incomplete")
    if linkage_confidence == "unavailable":
        items.append("linkage_unavailable")
    if trend is None:
        items.append("trend_unavailable")
    if recovery is None:
        items.append("recovery_unavailable")
    if planned_volume is not None and not planned_volume.duration_complete:
        items.append("planned_duration_incomplete")
    if planned_volume is not None and not planned_volume.distance_complete:
        items.append("planned_distance_incomplete")
    return tuple(items)
