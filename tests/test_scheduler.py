import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.jobs import scheduler as scheduler_module
from app.models import (
    GarminAccount,
    SyncRun,
    User,
    Workout,
    WorkoutGarminAttempt,
    WorkoutGarminBinding,
    WorkoutGarminOperation,
    WorkoutRevision,
)
from app.services.planning.workout_definition import default_definition


def test_scheduler_does_not_sync_immediately_on_startup(monkeypatch: Any) -> None:
    class FakeScheduler:
        running = False
        jobs: list[dict[str, object]] = []

        def add_job(self, *_args: object, **kwargs: object) -> None:
            self.jobs.append(kwargs)

        def start(self) -> None:
            self.running = True

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)
    monkeypatch.setattr(scheduler_module, "repair_interrupted_syncs", lambda: None)
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(
            scheduler_enabled=True,
            sync_interval_minutes=60,
            garmin_operation_stale_minutes=15,
        ),
    )

    scheduler_module.start_scheduler()

    assert fake_scheduler.running
    assert [job["id"] for job in fake_scheduler.jobs] == [
        "garmin-sync",
        "garmin-operation-repair",
    ]
    assert fake_scheduler.jobs[0]["minutes"] == 60
    assert fake_scheduler.jobs[1]["minutes"] == 5
    assert all("next_run_time" not in job for job in fake_scheduler.jobs)


def test_startup_repair_runs_when_periodic_scheduler_is_disabled(monkeypatch: Any) -> None:
    repaired: list[bool] = []
    monkeypatch.setattr(scheduler_module, "repair_interrupted_syncs", lambda: repaired.append(True))
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(scheduler_enabled=False),
    )

    scheduler_module.start_scheduler()

    assert repaired == [True]


def test_interrupted_syncs_become_retryable_after_restart(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    with session_factory() as session:
        user = User(display_name="Restart")
        session.add(user)
        session.flush()
        account = GarminAccount(
            user_id=user.id,
            connected_at=datetime.now(UTC).replace(tzinfo=None),
            sync_status="running",
        )
        run = SyncRun(user_id=user.id, status="running", stage="health")
        session.add_all([account, run])
        session.commit()
        account_id = account.id
        run_id = run.id

    monkeypatch.setattr(scheduler_module, "SessionLocal", session_factory)
    scheduler_module.repair_interrupted_syncs()

    with session_factory() as session:
        account = session.get(GarminAccount, account_id)
        run = session.get(SyncRun, run_id)
        assert account is not None
        assert account.sync_status == "error"
        assert "erneut starten" in (account.sync_error or "")
        assert run is not None
        assert run.status == "error"
        assert run.finished_at is not None


def test_interrupted_workout_attempt_becomes_unknown_after_restart(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_factory() as session:
        user = User(display_name="Restart")
        session.add(user)
        session.flush()
        workout = Workout(
            user_id=user.id,
            name="Run",
            sport="running",
            status="confirmed",
            definition=default_definition().model_dump(mode="json"),
        )
        session.add(workout)
        session.flush()
        revision = WorkoutRevision(
            workout_id=workout.id,
            revision_number=1,
            name="Run",
            sport="running",
            definition_version=1,
            definition=default_definition().model_dump(mode="json"),
            content_hash="a" * 64,
        )
        binding = WorkoutGarminBinding(workout_id=workout.id)
        session.add_all([revision, binding])
        session.flush()
        operation = WorkoutGarminOperation(
            workout_id=workout.id,
            binding_id=binding.id,
            operation_type="upload",
            revision_id=revision.id,
            idempotency_key="b" * 64,
            status="pending",
            created_at=now,
        )
        session.add(operation)
        session.flush()
        attempt = WorkoutGarminAttempt(
            operation_id=operation.id,
            attempt_number=1,
            attempt_kind="execute",
            status="pending",
            started_at=now,
        )
        session.add(attempt)
        session.commit()
        operation_id = operation.id
        attempt_id = attempt.id
        binding_id = binding.id

    monkeypatch.setattr(scheduler_module, "SessionLocal", session_factory)
    scheduler_module.repair_interrupted_syncs()

    with session_factory() as session:
        operation = session.get(WorkoutGarminOperation, operation_id)
        attempt = session.get(WorkoutGarminAttempt, attempt_id)
        binding = session.get(WorkoutGarminBinding, binding_id)
        assert operation is not None and operation.status == "unknown"
        assert attempt is not None and attempt.status == "unknown"
        assert binding is not None and binding.content_status == "unknown"


def test_interrupted_operation_without_attempt_is_retryable(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    with session_factory() as session:
        user = User(display_name="Restart")
        session.add(user)
        session.flush()
        workout = Workout(
            user_id=user.id,
            name="Run",
            sport="running",
            status="confirmed",
            definition=default_definition().model_dump(mode="json"),
        )
        session.add(workout)
        session.flush()
        revision = WorkoutRevision(
            workout_id=workout.id,
            revision_number=1,
            name="Run",
            sport="running",
            definition_version=1,
            definition=default_definition().model_dump(mode="json"),
            content_hash="c" * 64,
        )
        binding = WorkoutGarminBinding(workout_id=workout.id)
        session.add_all([revision, binding])
        session.flush()
        operation = WorkoutGarminOperation(
            workout_id=workout.id,
            binding_id=binding.id,
            operation_type="upload",
            revision_id=revision.id,
            idempotency_key="d" * 64,
            status="pending",
        )
        session.add(operation)
        session.commit()
        operation_id = operation.id
        binding_id = binding.id

    monkeypatch.setattr(scheduler_module, "SessionLocal", session_factory)
    scheduler_module.repair_interrupted_syncs()

    with session_factory() as session:
        operation = session.get(WorkoutGarminOperation, operation_id)
        binding = session.get(WorkoutGarminBinding, binding_id)
        assert operation is not None and operation.status == "retryable"
        assert binding is not None and binding.content_status == "retryable"


def test_stale_pending_attempt_becomes_unknown_while_process_is_running(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=20)
    with session_factory() as session:
        user = User(display_name="Stale")
        session.add(user)
        session.flush()
        session.add(GarminAccount(user_id=user.id, sync_status="connected"))
        workout = Workout(
            user_id=user.id,
            name="Run",
            sport="running",
            status="confirmed",
            definition=default_definition().model_dump(mode="json"),
        )
        session.add(workout)
        session.flush()
        revision = WorkoutRevision(
            workout_id=workout.id,
            revision_number=1,
            name="Run",
            sport="running",
            definition_version=1,
            definition=default_definition().model_dump(mode="json"),
            content_hash="e" * 64,
        )
        binding = WorkoutGarminBinding(workout_id=workout.id)
        session.add_all([revision, binding])
        session.flush()
        operation = WorkoutGarminOperation(
            workout_id=workout.id,
            binding_id=binding.id,
            operation_type="upload",
            revision_id=revision.id,
            idempotency_key="f" * 64,
            status="pending",
            created_at=old,
        )
        session.add(operation)
        session.flush()
        attempt = WorkoutGarminAttempt(
            operation_id=operation.id,
            attempt_number=1,
            attempt_kind="execute",
            status="pending",
            started_at=old,
        )
        fresh_operation = WorkoutGarminOperation(
            workout_id=workout.id,
            binding_id=binding.id,
            operation_type="upload",
            revision_id=revision.id,
            idempotency_key="1" * 64,
            status="pending",
            created_at=old,
        )
        session.add_all([attempt, fresh_operation])
        session.flush()
        fresh_attempt = WorkoutGarminAttempt(
            operation_id=fresh_operation.id,
            attempt_number=1,
            attempt_kind="execute",
            status="pending",
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(fresh_attempt)
        session.commit()
        operation_id = operation.id
        attempt_id = attempt.id
        fresh_operation_id = fresh_operation.id

    monkeypatch.setattr(scheduler_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(garmin_operation_stale_minutes=15),
    )
    scheduler_module.repair_stale_garmin_operations()

    with session_factory() as session:
        operation = session.get(WorkoutGarminOperation, operation_id)
        attempt = session.get(WorkoutGarminAttempt, attempt_id)
        fresh_operation = session.get(WorkoutGarminOperation, fresh_operation_id)
        assert operation is not None and operation.status == "unknown"
        assert operation.error_code == "garmin.operation_stale"
        assert attempt is not None and attempt.status == "unknown"
        assert fresh_operation is not None and fresh_operation.status == "pending"


def test_periodic_sync_only_queues_accounts_with_an_expired_successful_sync(
    session_factory: sessionmaker[Session], monkeypatch: Any
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_factory() as session:
        users = [User(display_name=name) for name in ("Due", "Recent", "Initial", "Offline")]
        session.add_all(users)
        session.flush()
        accounts = [
            GarminAccount(
                user_id=users[0].id,
                connected_at=now,
                last_sync_at=now - timedelta(minutes=61),
                sync_status="ok",
            ),
            GarminAccount(
                user_id=users[1].id,
                connected_at=now,
                last_sync_at=now - timedelta(minutes=30),
                sync_status="ok",
            ),
            GarminAccount(user_id=users[2].id, connected_at=now, sync_status="connected"),
            GarminAccount(
                user_id=users[3].id,
                last_sync_at=now - timedelta(minutes=90),
                sync_status="not_connected",
            ),
        ]
        session.add_all(accounts)
        session.commit()
        due_account_id = accounts[0].id

    queued: list[int] = []

    def record_queue(account_id: int, mark_queued: Any) -> bool:
        if not mark_queued():
            return False
        queued.append(account_id)
        return True

    monkeypatch.setattr(scheduler_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(sync_interval_minutes=60),
    )
    monkeypatch.setattr(scheduler_module, "queue_account_sync", record_queue)

    scheduler_module.synchronize_accounts()

    assert queued == [due_account_id]
    with session_factory() as session:
        account = session.get(GarminAccount, due_account_id)
        assert account is not None and account.sync_status == "queued"


def test_account_queue_runs_different_accounts_concurrently(monkeypatch: Any) -> None:
    started: set[int] = set()
    started_lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def synchronize(account_id: int, *, wait_for_slot: bool = False) -> None:
        assert wait_for_slot is True
        with started_lock:
            started.add(account_id)
            if len(started) == 2:
                both_started.set()
        assert release.wait(2)

    monkeypatch.setattr(scheduler_module, "synchronize_account", synchronize)
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(garmin_sync_workers=2),
    )

    try:
        assert not scheduler_module.queue_account_sync(303, lambda: False)
        assert scheduler_module.queue_account_sync(101, lambda: True)
        assert not scheduler_module.queue_account_sync(101, lambda: True)
        assert scheduler_module.queue_account_sync(202, lambda: True)
        assert both_started.wait(2)
        assert started == {101, 202}
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while scheduler_module._queued_account_ids and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduler_module.stop_scheduler()
