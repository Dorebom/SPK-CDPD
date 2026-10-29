"""One-shot Worker over dedicated inherited pipes; no Bundle or control writes.

stdin carries one WorkerRequest followed only by replies to fresh permit
challenges. stdout carries those challenges followed by one temporary result.
The production module exposes no fixture, alternate-origin, or command flag.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from typing import Callable

from cdpd.collector import CaptureError
from .spool import canonical_json, strict_json
from .worker import WorkerRequest, run_capture_worker

MAX_REQUEST_BYTES = 131072
MAX_PERMIT_BYTES = 4096
MAX_RESULT_BYTES = 2228224
MAX_PERMIT_MESSAGES = 64
PERMIT_SECONDS = 1.0


class PipeProtocolError(Exception):
    """Internal fixed error codes; never retains raw IPC or exception strings."""
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _LineReader:
    """A bounded pipe reader that also works with Windows anonymous pipes.

    os.read avoids buffered-stdin locks held by daemon threads at interpreter
    shutdown. The finite queue and aggregate cap bound a misbehaving peer.
    """
    def __init__(self, fd: int, *, maximum: int, total: int, messages: int) -> None:
        self.fd, self.maximum, self.total, self.messages = fd, maximum, total, messages
        self.queue: queue.Queue = queue.Queue(maxsize=2)
        self.stop = threading.Event()
        self.bytes_received = 0
        self.thread = threading.Thread(target=self._read, daemon=True, name="cdpd-worker-pipe-reader")
        self.thread.start()

    def _put(self, item: tuple[str, object]) -> None:
        while not self.stop.is_set():
            try:
                self.queue.put(item, timeout=0.05)
                return
            except queue.Full:
                pass

    def _read(self) -> None:
        pending = bytearray()
        count = 0
        try:
            while not self.stop.is_set():
                count_bytes = min(65536, self.maximum - len(pending) + 1,
                                  self.total - self.bytes_received + 1)
                raw = os.read(self.fd, max(1, count_bytes))
                if not raw:
                    self._put(("ERROR", "TRUNCATED_LINE") if pending else ("EOF", None))
                    return
                self.bytes_received += len(raw)
                if self.bytes_received > self.total:
                    self._put(("ERROR", "OUTPUT_TOO_LARGE"))
                    return
                pending.extend(raw)
                while b"\n" in pending:
                    end = pending.index(10)
                    if end > self.maximum:
                        self._put(("ERROR", "OUTPUT_TOO_LARGE"))
                        return
                    line = bytes(pending[:end])
                    del pending[:end + 1]
                    count += 1
                    if count > self.messages:
                        self._put(("ERROR", "TOO_MANY_MESSAGES"))
                        return
                    self._put(("LINE", line))
                if len(pending) > self.maximum:
                    self._put(("ERROR", "OUTPUT_TOO_LARGE"))
                    return
        except (OSError, ValueError):
            self._put(("ERROR", "PIPE_CLOSED"))

    def next(self, deadline: float) -> tuple[str, object]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PipeProtocolError("DEADLINE")
        try:
            item = self.queue.get(timeout=remaining)
        except queue.Empty:
            raise PipeProtocolError("DEADLINE") from None
        if time.monotonic() > deadline:
            raise PipeProtocolError("DEADLINE")
        if item[0] == "ERROR":
            raise PipeProtocolError(str(item[1]))
        return item


def _bounded_call(callback: Callable, deadline: float):
    """Wait boundedly; a late callback result is never reused or transmitted."""
    done: queue.Queue = queue.Queue(maxsize=1)
    def invoke() -> None:
        try:
            value = callback()
            done.put((True, value))
        except BaseException:
            done.put((False, None))
    threading.Thread(target=invoke, daemon=True, name="cdpd-worker-bounded-call").start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PipeProtocolError("DEADLINE")
    try:
        ok, value = done.get(timeout=remaining)
    except queue.Empty:
        raise PipeProtocolError("DEADLINE") from None
    if time.monotonic() > deadline:
        raise PipeProtocolError("DEADLINE")
    if not ok:
        raise PipeProtocolError("CALL_FAILED")
    return value


def _write_line(fd: int, value: dict, *, maximum: int, deadline: float) -> None:
    try:
        raw = canonical_json(value)
    except Exception:
        raise PipeProtocolError("MESSAGE_INVALID") from None
    if len(raw) > maximum:
        raise PipeProtocolError("OUTPUT_TOO_LARGE")
    raw += b"\n"
    def write() -> None:
        remaining = memoryview(raw)
        while remaining:
            if time.monotonic() > deadline:
                raise PipeProtocolError("DEADLINE")
            count = os.write(fd, remaining)
            if count <= 0:
                raise PipeProtocolError("PIPE_CLOSED")
            remaining = remaining[count:]
    _bounded_call(write, deadline)


def serve_worker(*, connection_factory: Callable | None = None,
                 input_fd: int = 0, output_fd: int = 1) -> int:
    """Serve exactly one request. Injection is an in-process test seam only."""
    reader = _LineReader(input_fd, maximum=MAX_REQUEST_BYTES,
                         total=MAX_REQUEST_BYTES + (MAX_PERMIT_MESSAGES + 1) * (MAX_PERMIT_BYTES + 1),
                         messages=MAX_PERMIT_MESSAGES + 1)
    try:
        kind, raw = reader.next(time.monotonic() + PERMIT_SECONDS)
        if kind != "LINE":
            raise PipeProtocolError("INPUT_INVALID")
        job = WorkerRequest.from_dict(strict_json(raw, max_bytes=MAX_REQUEST_BYTES))
        local_deadline = time.monotonic() + job.to_dict()["capture_request"]["timeout_seconds"]
        checks = 0
        def permit(challenge: dict) -> dict:
            nonlocal checks
            checks += 1
            if checks > MAX_PERMIT_MESSAGES:
                raise CaptureError("CLOCK_OR_KEEPER_LOST")
            deadline = min(local_deadline, time.monotonic() + PERMIT_SECONDS)
            try:
                _write_line(output_fd, challenge, maximum=MAX_PERMIT_BYTES, deadline=deadline)
                kind, raw = reader.next(deadline)
                if kind != "LINE":
                    raise PipeProtocolError("PIPE_CLOSED")
                return strict_json(raw, max_bytes=MAX_PERMIT_BYTES)
            except Exception:
                raise CaptureError("CLOCK_OR_KEEPER_LOST") from None
        if connection_factory is None:
            from .capture_transport import ReadOnlyCaptureConnection
            connection_factory = ReadOnlyCaptureConnection
        value = run_capture_worker(job, permit=permit, connection_factory=connection_factory)
        _write_line(output_fd, value, maximum=MAX_RESULT_BYTES, deadline=local_deadline)
        return 0
    except Exception:
        # No raw exception, request bytes, DOM, or stderr reaches the parent.
        try:
            _write_line(output_fd, {"schema": "cdpd.capture-worker-protocol-error/v1",
                        "code": "INPUT_OR_WORKER_PROTOCOL_FAILED", "pid": os.getpid()},
                        maximum=MAX_PERMIT_BYTES, deadline=time.monotonic() + 0.1)
        except Exception:
            pass
        return 2
    finally:
        reader.stop.set()


def main() -> int:
    if len(sys.argv) != 1:
        return 64
    # FIX007: a direct CPython base interpreter must retain the caller's
    # *actual* Windows venv. Deny before receiving a request if its pinned
    # runtime/venv identity is not preserved. No protocol or authority bypass.
    from .worker_interpreter import worker_runtime_attested
    if not worker_runtime_attested():
        return 65
    return serve_worker()


if __name__ == "__main__":
    raise SystemExit(main())
