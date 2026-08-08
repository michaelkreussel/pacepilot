import threading
import time
from types import SimpleNamespace
from typing import Any

from app.jobs import scheduler as scheduler_module


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
