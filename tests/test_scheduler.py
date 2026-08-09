import threading
import time
from types import SimpleNamespace
from typing import Any

from app.jobs import scheduler as scheduler_module


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
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(scheduler_enabled=True, sync_interval_minutes=60),
    )

    scheduler_module.start_scheduler()

    assert fake_scheduler.running
    assert fake_scheduler.job_kwargs["minutes"] == 60
    assert "next_run_time" not in fake_scheduler.job_kwargs


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
        assert scheduler_module.queue_account_sync(101, lambda: None)
        assert not scheduler_module.queue_account_sync(101, lambda: None)
        assert scheduler_module.queue_account_sync(202, lambda: None)
        assert both_started.wait(2)
        assert started == {101, 202}
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while scheduler_module._queued_account_ids and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduler_module.stop_scheduler()
