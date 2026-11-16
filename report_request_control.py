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
    """A non-blocking gate: excess report requests are rejected, never queued."""

    def __init__(self, max_concurrent=2):
        self.max_concurrent = bounded_env_int(max_concurrent, 2, 1, 16)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)

    def acquire(self):
        return self._semaphore.acquire(blocking=False)

    def release(self):
        self._semaphore.release()


_REPORT_ENDPOINT_MARKERS = (
    "report", "dashboard", "statement", "ledger", "trial_balance",
    "cash_flow", "cashflow",
)
_REPORT_PATH_PREFIXES = (
    "/reports/", "/dashboard/", "/trial-balance", "/trial_balance",
    "/ledger", "/hr/payroll-report", "/hr/employee-statement",
)


def is_expensive_report_request(method, endpoint, path):
    """Identify read-only report views without throttling normal data-entry pages."""
    if str(method or "").upper() != "GET":
        return False
    endpoint_name = str(endpoint or "").lower()
    request_path = str(path or "").lower()
    return (
        any(marker in endpoint_name for marker in _REPORT_ENDPOINT_MARKERS)
        or any(request_path.startswith(prefix) for prefix in _REPORT_PATH_PREFIXES)
    )


def make_sqlite_deadline_callback(deadline):
    """Return a SQLite progress callback that interrupts VM work at the deadline."""
    def should_interrupt():
        return int(time.monotonic() >= deadline)
    return should_interrupt
