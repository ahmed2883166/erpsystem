"""Bounded execution controls for expensive, read-only report requests.

Waitress/WSGI does not reliably expose a browser disconnect to synchronous Flask
view code. This module therefore enforces a server-side deadline and a non-queuing
concurrency gate; it does not pretend to detect every disconnected client.
"""
import threading
import time
from contextvars import ContextVar

_REPORT_DEADLINE = ContextVar('nexus_report_deadline', default=None)


def set_current_report_deadline(deadline):
    return _REPORT_DEADLINE.set(deadline)


def reset_current_report_deadline(token):
    _REPORT_DEADLINE.reset(token)


def current_report_deadline():
    return _REPORT_DEADLINE.get()


def bounded_env_int(value, default, minimum=1, maximum=120):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


class ReportRequestGate:
    """Limit active reports and allow only a small, time-bounded waiting queue."""

    def __init__(self, max_concurrent=2, max_waiters=4, wait_seconds=10):
        self.max_concurrent = bounded_env_int(max_concurrent, 2, 1, 16)
        self.max_waiters = bounded_env_int(max_waiters, 4, 0, 32)
        self.wait_seconds = bounded_env_int(wait_seconds, 10, 1, 30)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self._waiter_lock = threading.Lock()
        self._waiting = 0

    @property
    def waiting_count(self):
        with self._waiter_lock:
            return self._waiting

    def acquire(self):
        # Fast path: do not enter the waiting queue when a slot is available.
        if self._semaphore.acquire(blocking=False):
            return True

        # Bound the number of requests allowed to occupy Waitress worker threads.
        with self._waiter_lock:
            if self._waiting >= self.max_waiters:
                return False
            self._waiting += 1

        try:
            # Recheck after joining the queue to close the release/acquire race.
            if self._semaphore.acquire(blocking=False):
                return True
            return self._semaphore.acquire(timeout=self.wait_seconds)
        finally:
            with self._waiter_lock:
                self._waiting -= 1

    def release(self):
        self._semaphore.release()


# Match concrete report URL families only. Never classify a page from its
# Flask endpoint name: the application's home page endpoint is named "dashboard".
_REPORT_PATH_PREFIXES = (
    "/reports",
    "/dashboard/management",
    "/trial-balance",
    "/trial_balance",
    "/ledger",
    "/hr/payroll-report",
    "/hr/employee-statement",
)


def is_expensive_report_request(method, endpoint, path):
    """Identify known read-only report URLs, never ordinary pages by endpoint name."""
    if str(method or "").upper() != "GET":
        return False
    request_path = str(path or "").split("?", 1)[0].rstrip("/").lower()
    return any(
        request_path == prefix or request_path.startswith(prefix + "/")
        for prefix in _REPORT_PATH_PREFIXES
    )


def make_sqlite_deadline_callback(deadline):
    """Return a SQLite progress callback that interrupts VM work at the deadline."""
    def should_interrupt():
        return int(time.monotonic() >= deadline)
    return should_interrupt
