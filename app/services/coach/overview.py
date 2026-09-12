"""Read-only projection for the training-first coach page."""

from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, Workout, WorkoutRevision
from app.repositories.activities import activities_between
from app.repositories.workouts import workouts_between
from app.services.analytics.activity_semantics import is_running_sport
from app.services.analytics.progress import get_progress
from app.services.planning.daily_adaptation import DailyAdaptationError, DailyAdaptationService
from app.services.planning.daily_recommendation import DailyRecommendation
from app.services.planning.workout_definition import (
    RepeatBlockV2,
    StepBlockV2,
    TimeEnd,
    workout_metrics,
)
from app.services.planning.workout_views import GOAL_TYPE_LABELS, revision_view


def overview_context(session: Session, user: User, today: DailyRecommendation) -> dict[str, object]:
    start = today.as_of - timedelta(days=today.as_of.weekday())
    end = start + timedelta(days=6)
    activities = [
        a
        for a in activities_between(
            session,
            user.id,
            datetime.combine(start, time.min),
            datetime.combine(min(end, today.as_of) + timedelta(days=1), time.min),
        )
        if is_running_sport(a.activity_type)
    ]
    linked = {a.workout_id for a in activities if a.workout_id is not None}
    workouts = [
        w for w in workouts_between(session, user.id, start, end) if is_running_sport(w.sport)
    ]
    proposals = [
        (workout, revision_view(revision))
        for workout, revision in session.execute(
            select(Workout, WorkoutRevision)
            .join(WorkoutRevision, Workout.current_revision_id == WorkoutRevision.id)
            .where(
                Workout.user_id == user.id,
                Workout.deleted_at.is_(None),
                Workout.accepted_revision_id.is_(None),
                Workout.approval_status == "proposed",
                WorkoutRevision.suggested_for >= start,
                WorkoutRevision.suggested_for <= end,
            )
            .order_by(WorkoutRevision.suggested_for, Workout.id)
        )
    ]
    days = [
        {
            "date": start + timedelta(days=i),
            "activities": [
                a for a in activities if a.started_at.date() == start + timedelta(days=i)
            ],
            "workouts": [w for w in workouts if w.scheduled_for == start + timedelta(days=i)],
            "proposals": [
                (w, r) for w, r in proposals if r.suggested_for == start + timedelta(days=i)
            ],
        }
        for i in range(7)
    ]
    adaptation = None
    adaptation_notice = None
    if today.state == "scheduled" and today.workout_id is not None:
        try:
            adaptation = DailyAdaptationService(session, user, as_of=today.as_of).assess_today(
                today.workout_id
            )
        except DailyAdaptationError as exc:
            adaptation_notice = str(exc)
    progress = get_progress(session, user.id, as_of=today.as_of)
    accepted_revision = (
        session.get(WorkoutRevision, today.revision_id) if today.revision_id else None
    )
    work_differences = {}
    if adaptation is not None:
        for candidate in adaptation.assessment.candidates:
            work_differences[candidate.adaptation_class.value] = sum(
                block.iterations * step.end.seconds
                for block in (candidate.definition.blocks if candidate.definition else [])
                if isinstance(block, RepeatBlockV2)
                for step in block.children
                if isinstance(step, StepBlockV2)
                and step.step_type == "interval"
                and isinstance(step.end, TimeEnd)
            )
    return {
        "week_days": days,
        "week_start": start,
        "week_end": end,
        "linked_workouts": linked,
        "completed_seconds": sum(a.duration_s or 0 for a in activities),
        "remaining_seconds": sum(
            workout_metrics(w.definition).duration_seconds
            for w in workouts
            if w.id not in linked and w.scheduled_for >= today.as_of
        ),
        "progress": progress,
        "goal_labels": GOAL_TYPE_LABELS,
        "adaptation_preview": adaptation,
        "adaptation_notice": adaptation_notice,
        "today_revision": revision_view(accepted_revision) if accepted_revision else None,
        "adaptation_work_seconds": work_differences,
    }
