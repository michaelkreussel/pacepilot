import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Activity,
    DailyDataStatus,
    DailyFitness,
    DailyHealth,
    GarminAccount,
    GarminDevice,
    GarminSyncState,
    SyncRun,
    WorkoutGarminBinding,
    WorkoutGarminRemoteIdentity,
)
from app.services.garmin.client import cancel_garmin_account_logins
from app.services.garmin.locks import garmin_account_slot

logger = logging.getLogger(__name__)


def garmin_principal_fingerprint(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def record_connected_principal(session: Session, account: GarminAccount, email: str) -> None:
    fingerprint = garmin_principal_fingerprint(email)
    identities = list(
        session.scalars(
            select(WorkoutGarminRemoteIdentity).where(
                WorkoutGarminRemoteIdentity.garmin_account_id == account.id
            )
        )
    )
    principal_changed = account.principal_fingerprint not in {None, fingerprint}
    unverified_binding_ids = {
        identity.binding_id
        for identity in identities
        if identity.principal_fingerprint != fingerprint
    }
    if principal_changed or unverified_binding_ids:
        binding_ids = (
            {identity.binding_id for identity in identities}
            if principal_changed
            else unverified_binding_ids
        )
        bindings = list(
            session.scalars(
                select(WorkoutGarminBinding).where(WorkoutGarminBinding.id.in_(binding_ids))
            )
        )
        for binding in bindings:
            binding.content_status = "unknown"
            binding.calendar_status = "unknown"
            binding.device_status = "unknown"
            binding.last_error_code = "garmin.principal_changed"
            binding.last_error_message = (
                "Das verbundene Garmin-Konto hat gewechselt. "
                "Bestehende Remote-IDs müssen manuell geprüft werden."
            )
    account.principal_fingerprint = fingerprint


@dataclass(frozen=True)
class GarminDataDeletionResult:
    activities: int
    health_days: int
    fitness_days: int
    sync_runs: int
    workouts_unlinked: int


def _account_token_directory(account_id: int) -> Path:
    if account_id < 1:
        raise ValueError("Garmin account ID must be positive")
    return Path(get_settings().garmin_token_dir) / f"account-{account_id}"


def _user_activity_directory(user_id: int) -> Path:
    if user_id < 1:
        raise ValueError("User ID must be positive")
    return get_settings().data_dir / "raw" / "activities" / f"user-{user_id}"


def _remove_directory(path: Path, expected_parent: Path) -> None:
    if not path.exists():
        return
    if path.parent.resolve() != expected_parent.resolve():
        raise ValueError("Refusing to delete a directory outside the Garmin data root")
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


def _reset_connection(account: GarminAccount) -> None:
    account.email = None
    account.connected_at = None
    account.last_sync_at = None
    account.rate_limit_until = None
    account.sync_status = "not_connected"
    account.sync_error = None


def _user_row_count(session: Session, model: Any, user_id: int) -> int:
    statement = select(func.count()).select_from(model).where(model.user_id == user_id)
    return session.scalar(statement) or 0


def disconnect_garmin_account(session: Session, account: GarminAccount) -> None:
    with garmin_account_slot(account.id):
        cancel_garmin_account_logins(account_id=account.id, user_id=account.user_id)
        token_directory = _account_token_directory(account.id)
        _remove_directory(token_directory, token_directory.parent)
        _reset_connection(account)
        session.commit()
        logger.info(
            "Garmin account disconnected",
            extra={"garmin_account_id": account.id, "sync_user_id": account.user_id},
        )


def delete_garmin_data(session: Session, account: GarminAccount) -> GarminDataDeletionResult:
    user_id = account.user_id
    account_id = account.id
    with garmin_account_slot(account_id):
        activity_directory = _user_activity_directory(user_id)
        _remove_directory(activity_directory, activity_directory.parent)

        activities = _user_row_count(session, Activity, user_id)
        health_days = _user_row_count(session, DailyHealth, user_id)
        fitness_days = _user_row_count(session, DailyFitness, user_id)
        sync_runs = _user_row_count(session, SyncRun, user_id)
        session.execute(delete(Activity).where(Activity.user_id == user_id))
        session.execute(delete(DailyHealth).where(DailyHealth.user_id == user_id))
        session.execute(delete(DailyFitness).where(DailyFitness.user_id == user_id))
        session.execute(delete(DailyDataStatus).where(DailyDataStatus.user_id == user_id))
        session.execute(delete(GarminSyncState).where(GarminSyncState.user_id == user_id))
        session.execute(delete(SyncRun).where(SyncRun.user_id == user_id))
        session.execute(delete(GarminDevice).where(GarminDevice.account_id == account_id))

        # Remote identities and operation history are a minimal deduplication ledger. Removing
        # them while Garmin still holds the workout could cause a later duplicate upload.
        workouts_unlinked = 0
        account.last_sync_at = None
        account.rate_limit_until = None
        account.sync_error = None
        if account.connected_at is not None:
            account.sync_status = "connected"
        session.commit()

        result = GarminDataDeletionResult(
            activities=activities,
            health_days=health_days,
            fitness_days=fitness_days,
            sync_runs=sync_runs,
            workouts_unlinked=workouts_unlinked,
        )
        logger.info(
            "Garmin data deleted",
            extra={
                "garmin_account_id": account_id,
                "sync_user_id": user_id,
                "activities_deleted": result.activities,
                "health_days_deleted": result.health_days,
                "fitness_days_deleted": result.fitness_days,
                "sync_runs_deleted": result.sync_runs,
                "workouts_unlinked": result.workouts_unlinked,
            },
        )
        return result
