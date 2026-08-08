from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GarminSyncState
from app.models.user import utcnow


def get_or_create_sync_state(session: Session, user_id: int, resource: str) -> GarminSyncState:
    state = session.scalar(
        select(GarminSyncState).where(
            GarminSyncState.user_id == user_id,
            GarminSyncState.resource == resource,
        )
    )
    if state is None:
        state = GarminSyncState(user_id=user_id, resource=resource)
        session.add(state)
    return state


def mark_sync_attempt(state: GarminSyncState) -> None:
    state.status = "running"
    state.last_attempt_at = utcnow()
    state.error = None


def mark_sync_success(
    state: GarminSyncState,
    *,
    oldest_date: date | None = None,
    newest_date: date | None = None,
    backfill_cursor_date: date | None = None,
    cursor: str | None = None,
    backfill_complete: bool | None = None,
) -> None:
    if oldest_date is not None:
        state.oldest_synced_date = min(
            value for value in (state.oldest_synced_date, oldest_date) if value is not None
        )
    if newest_date is not None:
        state.newest_synced_date = max(
            value for value in (state.newest_synced_date, newest_date) if value is not None
        )
    state.backfill_cursor_date = backfill_cursor_date
    state.cursor = cursor
    if backfill_complete is not None:
        state.backfill_complete = backfill_complete
    state.status = "ok"
    state.last_success_at = utcnow()
    state.error = None


def mark_sync_error(state: GarminSyncState, error: str) -> None:
    state.status = "error"
    state.error = error[:1000]
