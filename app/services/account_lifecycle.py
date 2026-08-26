import base64
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base
from app.models import GarminAccount, User
from app.models.user import utcnow
from app.services.garmin.client import cancel_garmin_account_logins
from app.services.garmin.locks import garmin_account_slot

EXPORT_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountDeletionResult:
    user_id: int
    garmin_account_id: int | None


class AccountExportBusyError(RuntimeError):
    pass


_export_locks_guard = threading.Lock()
_export_locks: dict[int, threading.Lock] = {}


@contextmanager
def _user_export_slot(user_id: int) -> Iterator[None]:
    with _export_locks_guard:
        lock = _export_locks.setdefault(user_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise AccountExportBusyError("Für dieses Konto wird bereits ein Export erstellt.")
    try:
        yield
    finally:
        lock.release()


def _ids(rows: list[dict[str, Any]]) -> set[int]:
    return {int(row["id"]) for row in rows}


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Unsupported export value: {type(value).__name__}")


def _table_rows(
    session: Session,
    table_name: str,
    owner_column: str,
    owner_ids: Iterable[int],
) -> list[dict[str, Any]]:
    values = tuple(owner_ids)
    if not values:
        return []
    table = Base.metadata.tables[table_name]
    statement = select(table).where(table.c[owner_column].in_(values))
    if table.primary_key.columns:
        statement = statement.order_by(*table.primary_key.columns)
    return [dict(row) for row in session.execute(statement).mappings()]


def collect_user_rows(session: Session, user_id: int) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}

    def add(table: str, column: str, values: Iterable[int]) -> list[dict[str, Any]]:
        selected = _table_rows(session, table, column, values)
        rows[table] = selected
        return selected

    add("users", "id", [user_id])
    add("oauth_identities", "user_id", [user_id])
    accounts = add("garmin_accounts", "user_id", [user_id])
    account_ids = _ids(accounts)
    add("garmin_devices", "account_id", account_ids)

    activities = add("activities", "user_id", [user_id])
    for activity in activities:
        for field in ("raw_file", "details_file"):
            activity[field] = _portable_activity_path(user_id, activity.get(field))
    activity_ids = _ids(activities)
    add("activity_zones", "activity_id", activity_ids)
    add("activity_splits", "activity_id", activity_ids)
    add("activity_exercise_sets", "activity_id", activity_ids)

    health_days = add("daily_health", "user_id", [user_id])
    add("sleep_stages", "daily_health_id", _ids(health_days))
    add("daily_fitness", "user_id", [user_id])

    sync_runs = add("sync_runs", "user_id", [user_id])
    add("sync_events", "sync_run_id", _ids(sync_runs))
    add("garmin_sync_states", "user_id", [user_id])
    add("daily_data_statuses", "user_id", [user_id])

    workouts = add("workouts", "user_id", [user_id])
    workout_ids = _ids(workouts)
    add("workout_steps", "workout_id", workout_ids)
    add("workout_revisions", "workout_id", workout_ids)
    add("workout_validation_runs", "workout_id", workout_ids)
    add("workout_events", "owner_user_id", [user_id])
    bindings = add("workout_garmin_bindings", "workout_id", workout_ids)
    binding_ids = _ids(bindings)
    add("workout_garmin_remote_identities", "binding_id", binding_ids)
    operations = add("workout_garmin_operations", "binding_id", binding_ids)
    add("workout_garmin_attempts", "operation_id", _ids(operations))

    conversations = add("coach_conversations", "user_id", [user_id])
    conversation_ids = _ids(conversations)
    messages = add("coach_messages", "conversation_id", conversation_ids)
    add("coach_assistant_runs", "conversation_id", conversation_ids)
    add("coach_tool_calls", "message_id", _ids(messages))

    add("pre_session_feedback", "user_id", [user_id])
    add("post_session_feedback", "user_id", [user_id])
    add("athlete_planning_profiles", "user_id", [user_id])
    add("athlete_goals", "user_id", [user_id])
    add("athlete_availability", "user_id", [user_id])
    add("performance_anchors", "user_id", [user_id])
    add("training_plans", "user_id", [user_id])
    add("training_plan_revisions", "owner_user_id", [user_id])
    add("training_plan_workouts", "owner_user_id", [user_id])
    add("training_cycles", "user_id", [user_id])
    add("training_cycle_revisions", "owner_user_id", [user_id])
    add("training_cycle_weeks", "owner_user_id", [user_id])

    missing = set(Base.metadata.tables) - set(rows)
    if missing:
        raise RuntimeError(f"User export is missing tables: {', '.join(sorted(missing))}")
    return rows


def _user_activity_directory(user_id: int) -> Path:
    return get_settings().data_dir / "raw" / "activities" / f"user-{user_id}"


def _portable_activity_path(user_id: int, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        relative = Path(value).resolve().relative_to(_user_activity_directory(user_id).resolve())
    except ValueError:
        return None
    return (Path("raw/activities") / relative).as_posix()


def _account_token_directory(account_id: int) -> Path:
    return get_settings().garmin_token_dir / f"account-{account_id}"


def _export_directory() -> Path:
    path = get_settings().data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _raw_files(root: Path) -> Iterator[tuple[Path, Path]]:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path, path.relative_to(root)


def create_user_export(session: Session, user: User) -> Path:
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user.id))
    lock = garmin_account_slot(account.id) if account is not None else nullcontext()
    with _user_export_slot(user.id), lock:
        rows = collect_user_rows(session, user.id)
        with tempfile.NamedTemporaryFile(
            prefix=f"user-{user.id}-",
            suffix=".zip",
            dir=_export_directory(),
            delete=False,
        ) as temporary:
            export_path = Path(temporary.name)
        raw_manifest: list[dict[str, object]] = []
        try:
            with ZipFile(export_path, "w", compression=ZIP_DEFLATED) as archive:
                for table_name, table_rows in sorted(rows.items()):
                    archive.writestr(
                        f"database/{table_name}.json",
                        json.dumps(
                            table_rows,
                            ensure_ascii=False,
                            indent=2,
                            default=_json_default,
                        ),
                    )
                for source, relative in _raw_files(_user_activity_directory(user.id)):
                    archive_name = Path("raw/activities") / relative
                    content = source.read_bytes()
                    archive.writestr(archive_name.as_posix(), content)
                    raw_manifest.append(
                        {
                            "path": archive_name.as_posix(),
                            "bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
                manifest = {
                    "schema_version": EXPORT_SCHEMA_VERSION,
                    "exported_at": utcnow().isoformat(timespec="seconds") + "Z",
                    "table_counts": {name: len(items) for name, items in sorted(rows.items())},
                    "raw_files": raw_manifest,
                    "excluded": [
                        "Garmin access tokens",
                        "session secrets and cookies",
                        "application logs and host backups",
                    ],
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
        except Exception:
            export_path.unlink(missing_ok=True)
            raise
        return export_path


def remove_export(path: Path) -> None:
    if path.resolve().parent != _export_directory().resolve():
        raise ValueError("Refusing to remove an export outside the export directory")
    path.unlink(missing_ok=True)


def _stage_directory(path: Path) -> tuple[Path, Path] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("Refusing to erase account data through a symbolic link")
    staged = path.with_name(f".{path.name}.deleting-{uuid4().hex}")
    path.replace(staged)
    return path, staged


@contextmanager
def _staged_directories(paths: Iterable[Path]) -> Iterator[list[tuple[Path, Path]]]:
    staged: list[tuple[Path, Path]] = []
    try:
        for path in paths:
            item = _stage_directory(path)
            if item is not None:
                staged.append(item)
        yield staged
    except Exception:
        for original, temporary in reversed(staged):
            if temporary.exists() and not original.exists():
                temporary.replace(original)
        raise


def delete_user_account(session: Session, user: User) -> AccountDeletionResult:
    user_id = user.id
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user_id))
    account_id = account.id if account is not None else None
    lock = garmin_account_slot(account_id) if account_id is not None else nullcontext()
    paths = [_user_activity_directory(user_id)]
    if account_id is not None:
        paths.append(_account_token_directory(account_id))

    with lock:
        if account_id is not None:
            cancel_garmin_account_logins(account_id=account_id, user_id=user_id)
        with _staged_directories(paths) as staged:
            try:
                session.execute(delete(User).where(User.id == user_id))
                session.commit()
            except Exception:
                session.rollback()
                raise
        for _, temporary in staged:
            try:
                shutil.rmtree(temporary)
            except OSError:
                logger.exception("Account data cleanup remains quarantined")
        for export in _export_directory().glob(f"user-{user_id}-*.zip"):
            try:
                export.unlink(missing_ok=True)
            except OSError:
                logger.exception("Account export cleanup failed")
    return AccountDeletionResult(user_id=user_id, garmin_account_id=account_id)


def _repair_staged_directory(
    session: Session,
    temporary: Path,
    *,
    entity: type[User] | type[GarminAccount],
    entity_id: int,
    original: Path,
) -> None:
    if session.get(entity, entity_id) is None:
        shutil.rmtree(temporary)
    elif not original.exists():
        temporary.replace(original)
    else:
        logger.error("Account cleanup quarantine conflicts with an existing directory")


def repair_account_lifecycle(session: Session) -> None:
    raw_root = get_settings().data_dir / "raw" / "activities"
    token_root = get_settings().garmin_token_dir
    for root, pattern, entity, prefix in (
        (raw_root, r"^\.user-(\d+)\.deleting-[0-9a-f]+$", User, "user"),
        (token_root, r"^\.account-(\d+)\.deleting-[0-9a-f]+$", GarminAccount, "account"),
    ):
        if not root.exists():
            continue
        for temporary in root.iterdir():
            match = re.fullmatch(pattern, temporary.name)
            if match is None or not temporary.is_dir() or temporary.is_symlink():
                continue
            entity_id = int(match.group(1))
            _repair_staged_directory(
                session,
                temporary,
                entity=entity,
                entity_id=entity_id,
                original=root / f"{prefix}-{entity_id}",
            )
    export_root = get_settings().data_dir / "exports"
    if export_root.exists():
        for export in export_root.glob("user-*.zip"):
            export.unlink(missing_ok=True)
