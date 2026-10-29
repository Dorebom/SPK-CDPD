"""Test-only input scheduling for the independent Linux WD04 fixture.

The journal writer parks at process_pending entry, before entering the accepted
L05 method and before taking its quota lock. Publications still use the real,
non-waiting FileMailbox exactly once. This module never grants a receipt,
changes a watchdog clock/deadline, or runs in the product Supervisor.
"""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time


PUBLICATION_BARRIER_TIMEOUT = 3.0


class PublicationBarrierError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class PublicationBarrier:
    """One explicit, bounded publication interval at a writer poll boundary."""

    def __init__(self, *, timeout=PUBLICATION_BARRIER_TIMEOUT):
        if type(timeout) not in (int, float) or not 0 < timeout <= 3.0:
            raise ValueError("invalid publication barrier timeout")
        self.timeout = timeout
        self._condition = threading.Condition()
        self._serial = 0
        self._requested = None
        self._paused = None
        self._deadline = None
        self._failure = None
        self._closed = False

    def request_pause(self):
        """Arm a request without blocking the Supervisor monitor/RPC thread."""
        with self._condition:
            if self._closed or self._failure or self._requested is not None:
                raise PublicationBarrierError("FIXTURE_PUBLICATION_PAUSE_INVALID")
            self._serial += 1
            self._requested = self._serial
            self._deadline = time.monotonic() + self.timeout
            return self._requested

    def checkpoint(self):
        """Called only by the fixture writer, outside every quota transaction."""
        with self._condition:
            if self._closed:
                return
            if self._failure:
                raise PublicationBarrierError(self._failure)
            token = self._requested
            if token is None:
                return
            if time.monotonic() >= self._deadline:
                self._expire()
            self._paused = token
            self._condition.notify_all()
            try:
                while self._requested == token and not self._closed:
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        self._expire()
                    self._condition.wait(remaining)
                if self._failure:
                    raise PublicationBarrierError(self._failure)
            finally:
                self._paused = None
                self._condition.notify_all()

    def _expire(self):
        self._failure = "FIXTURE_PUBLICATION_PAUSE_EXPIRED"
        self._requested = None
        self._condition.notify_all()
        raise PublicationBarrierError(self._failure)

    def resume(self, token):
        with self._condition:
            if type(token) is not int or token != self._requested or self._closed or self._failure:
                raise PublicationBarrierError("FIXTURE_PUBLICATION_RESUME_INVALID")
            if time.monotonic() >= self._deadline:
                self._expire()
            self._requested = None
            self._condition.notify_all()

    def close(self):
        """Release a parked writer on fixture shutdown, including stdin EOF."""
        with self._condition:
            self._closed = True
            self._requested = None
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {"requested_token": self._requested, "paused_token": self._paused,
                    "failure": self._failure, "closed": self._closed}


@contextmanager
def publication_batch(supervisor):
    """Wait for an explicit park acknowledgement, then always request release.

    There is no publication retry here. A publication exception propagates,
    and a cleanup exception cannot replace that primary failure.
    """
    response = supervisor.rpc("PAUSE_PUBLICATION")
    assert response.get("status") == "PUBLICATION_PAUSE_REQUESTED", response
    token = response.get("token")
    assert type(token) is int and token > 0, response
    primary = None
    try:
        status = supervisor.wait(
            lambda value: value.get("publication_barrier", {}).get("paused_token") == token,
            timeout=PUBLICATION_BARRIER_TIMEOUT,
        )
        assert status["writer_alive"] and status["writer_error"] is None, status
        barrier = status["publication_barrier"]
        assert barrier["requested_token"] == token and barrier["failure"] is None, barrier
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            response = supervisor.rpc("RESUME_PUBLICATION", token=token)
            assert response.get("status") == "PUBLICATION_RESUMED", response
        except BaseException:
            if primary is None:
                raise
            primary.add_note("FIXTURE_PUBLICATION_RELEASE_FAILED; primary failure preserved")
