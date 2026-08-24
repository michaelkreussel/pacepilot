import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Activity, PostSessionFeedback

type FeedbackSource = Literal["garmin", "manual"]


@dataclass(frozen=True)
class EffectiveActivityFeedback:
    activity_id: int
    effort: float | None
    effort_source: FeedbackSource | None
    effort_feedback_id: int | None
    feel: int | None
    feel_source: FeedbackSource | None
    feel_feedback_id: int | None


def _valid_effort(value: float | int | None) -> float | None:
    if value is None or not math.isfinite(value) or not 1 <= value <= 10:
        return None
    return float(value)


def _valid_feel(value: int | None) -> int | None:
    return value if value is not None and 1 <= value <= 5 else None


def effective_activity_feedback(
    session: Session,
    user_id: int,
    activities: Sequence[Activity],
) -> dict[int, EffectiveActivityFeedback]:
    activity_ids = [activity.id for activity in activities]
    manual_rows = (
        list(
            session.scalars(
                select(PostSessionFeedback)
                .where(
                    PostSessionFeedback.user_id == user_id,
                    PostSessionFeedback.activity_id.in_(activity_ids),
                )
                .order_by(PostSessionFeedback.recorded_at.desc(), PostSessionFeedback.id.desc())
            )
        )
        if activity_ids
        else []
    )
    manual_effort: dict[int, tuple[float, int]] = {}
    manual_feel: dict[int, tuple[int, int]] = {}
    for row in manual_rows:
        if row.activity_id is None:
            continue
        effort = _valid_effort(row.session_rpe)
        feel = _valid_feel(row.overall_feel)
        if effort is not None and row.activity_id not in manual_effort:
            manual_effort[row.activity_id] = (effort, row.id)
        if feel is not None and row.activity_id not in manual_feel:
            manual_feel[row.activity_id] = (feel, row.id)

    result: dict[int, EffectiveActivityFeedback] = {}
    for activity in activities:
        garmin_effort = _valid_effort(activity.workout_rpe)
        garmin_feel = _valid_feel(activity.workout_feel)
        fallback_effort = manual_effort.get(activity.id)
        fallback_feel = manual_feel.get(activity.id)
        result[activity.id] = EffectiveActivityFeedback(
            activity_id=activity.id,
            effort=garmin_effort if garmin_effort is not None else _first(fallback_effort),
            effort_source=(
                "garmin" if garmin_effort is not None else "manual" if fallback_effort else None
            ),
            effort_feedback_id=None if garmin_effort is not None else _second(fallback_effort),
            feel=garmin_feel if garmin_feel is not None else _first(fallback_feel),
            feel_source=(
                "garmin" if garmin_feel is not None else "manual" if fallback_feel else None
            ),
            feel_feedback_id=None if garmin_feel is not None else _second(fallback_feel),
        )
    return result


def _first[T](value: tuple[T, int] | None) -> T | None:
    return value[0] if value else None


def _second[T](value: tuple[T, int] | None) -> int | None:
    return value[1] if value else None
