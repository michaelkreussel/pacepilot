import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.jobs import scheduler as scheduler_module
from app.models import GarminAccount, SyncRun, User


def test_scheduler_does_not_sync_immediately_on_startup(monkeypatch: Any) -> None:
    class FakeScheduler:
        running = False
        job_kwargs: dict[str, object] = {}

        def add_job(self, *_args: object, **kwargs: object) -> None:
            self.job_kwargs = kwargs

        def start(self) -> None:
            self.running = True

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)
    monkeypatch.setattr(scheduler_module, "repair_interrupted_syncs", lambda: None)
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(scheduler_enabled=True, sync_interval_minutes=60),
    )

    scheduler_module.start_scheduler()

    assert fake_scheduler.running
    assert fake_scheduler.job_kwargs["minutes"] == 60
    assert "next_run_time" not in fake_scheduler.job_kwargs


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
