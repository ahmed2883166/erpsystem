"""Process-local FIFO gate prototype for coordinating SQLite writers.

This module is intentionally opt-in and is not wired into production database
connections. It coordinates threads in one Python process only; SQLite remains
the cross-process locking authority.
"""
from collections import deque
from contextlib import contextmanager
import threading
import time


class SQLiteWriteGate:
    """A fair, process-local gate that admits one writer at a time in FIFO order."""

    def __init__(self):
        self._condition = threading.Condition()
        self._waiters = deque()
        self._held = False

    @contextmanager
    def hold(self, timeout=None):
        """Acquire the gate in FIFO order and yield time spent waiting.

        A timed-out waiter is removed without disturbing the order of the
        remaining waiters. The gate is always released, including on errors.
        """
        ticket = object()
        started = time.monotonic()
        deadline = None if timeout is None else started + max(0.0, float(timeout))
        acquired = False
        with self._condition:
            self._waiters.append(ticket)
            while self._held or self._waiters[0] is not ticket:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                    raise TimeoutError("Timed out waiting for the SQLite write gate.")
                self._condition.wait(remaining)
            self._waiters.popleft()
            self._held = True
            acquired = True
        try:
            yield time.monotonic() - started
        finally:
            if acquired:
                with self._condition:
                    self._held = False
                    self._condition.notify_all()

    @property
    def waiting_count(self):
        """Return the number of threads currently queued behind the active writer."""
        with self._condition:
            return len(self._waiters)
