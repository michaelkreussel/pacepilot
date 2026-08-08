import threading
from collections.abc import Iterator
from contextlib import contextmanager


class GarminAccountBusyError(RuntimeError):
    pass


_account_locks_guard = threading.Lock()
_account_locks: dict[int, threading.Lock] = {}


def _account_lock(account_id: int) -> threading.Lock:
    if account_id < 1:
        raise ValueError("Garmin account ID must be positive")
    with _account_locks_guard:
        return _account_locks.setdefault(account_id, threading.Lock())


@contextmanager
def garmin_account_slot(account_id: int, *, wait: bool = False) -> Iterator[None]:
    lock = _account_lock(account_id)
    if not lock.acquire(blocking=wait):
        raise GarminAccountBusyError("Für dieses Garmin-Konto läuft bereits eine Operation.")
    try:
        yield
    finally:
        lock.release()


def garmin_account_active(account_id: int) -> bool:
    return _account_lock(account_id).locked()
