import hmac

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select

from app.auth import CurrentUser
from app.config import get_settings
from app.database import SessionDep
from app.models import Workout, WorkoutRevision, WorkoutValidationRun
from app.services.observability import decision_trace, operational_metrics

router = APIRouter(prefix="/api")


@router.get("/workouts/{workout_id}/revisions/{revision_id}/decision-trace")
def workout_decision_trace(
    workout_id: int,
    revision_id: int,
    session: SessionDep,
    user: CurrentUser,
) -> dict[str, object]:
    revision = session.scalar(
        select(WorkoutRevision)
        .join(Workout, Workout.id == WorkoutRevision.workout_id)
        .where(
            Workout.id == workout_id,
            Workout.user_id == user.id,
            WorkoutRevision.id == revision_id,
        )
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision nicht gefunden")
    runs = list(
        session.scalars(
            select(WorkoutValidationRun).where(
                WorkoutValidationRun.workout_id == workout_id,
                WorkoutValidationRun.revision_id == revision_id,
            )
        )
    )
    return decision_trace(revision, runs)


@router.get("/metrics")
def metrics(
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    expected = get_settings().metrics_bearer_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if expected is None or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=404, detail="Not found")
    return operational_metrics(session)
