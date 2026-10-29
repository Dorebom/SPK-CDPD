"""L07 owned-process deadline around the unchanged L06 worker protocol.

The supervisor durably records S0 and the spawn attempt before calling this
runner. Communication, permits and their persistence callbacks run away from
the deadline monitor. A stalled OS creation/termination call is not described
as cancelled: it produces a sticky termination-unconfirmed latch. Late work
cannot update a sealed result or authorize another capture.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .process_runner import _failed_result, _valid_challenge, _valid_response, _valid_result
from .worker_interpreter import plan_worker_launch
from .spool import strict_json
from .terminal_failure import is_terminal, terminal_receipt
from .worker import WorkerInputError, WorkerRequest
from .worker_cli import (MAX_REQUEST_BYTES, MAX_PERMIT_BYTES, MAX_RESULT_BYTES,
                         MAX_PERMIT_MESSAGES, PERMIT_SECONDS, PipeProtocolError,
                         _LineReader, _bounded_call, _write_line)


def _spawn(*args, **kwargs):
    from .owned_process import OwnedWorker
    return OwnedWorker.spawn(*args, **kwargs)


def _termination_dict(value: Any) -> dict[str, Any]:
    """Accept only the small process-facts contract, never exception strings."""
    if is_dataclass(value):
        value = asdict(value)
    if type(value) is not dict:
        raise ValueError("INVALID_TERMINATION_RESULT")
    result = {key: value.get(key) for key in
              ("requested", "confirmed", "reason", "exit_code", "active_processes", "native_error")}
    if (type(result["requested"]) is not bool or type(result["confirmed"]) is not bool
            or type(result["reason"]) is not str or len(result["reason"]) > 96
            or any(result[k] is not None and type(result[k]) is not int
                   for k in ("exit_code", "active_processes"))
            or (result["native_error"] is not None and type(result["native_error"]) is not int
                and not (type(result["native_error"]) is str and len(result["native_error"]) <= 96
                         and result["native_error"].isascii() and result["native_error"].isidentifier()))
            or (result["confirmed"] and result["active_processes"] != 0)):
        raise ValueError("INVALID_TERMINATION_RESULT")
    return result


def _spawn_error_facts(error: BaseException) -> dict[str, Any]:
    """Keep primary/cleanup failure identities without exception messages."""
    def fact(value):
        name = type(value).__name__
        name = name if len(name) <= 96 and name.isascii() and name.isidentifier() else "Exception"
        native = getattr(value, 'winerror', None) or getattr(value, 'errno', None)
        return {'type': name, 'native_error': native if type(native) is int else None}
    primary = getattr(error, 'primary_error', None) or error
    cleanup = getattr(error, 'cleanup_errors', ())
    cleanup = cleanup if type(cleanup) in (tuple, list) else ()
    # The native adapter has a fixed, small handle set. Bound an injected
    # process factory's diagnostic metadata independently of its exceptions.
    return {'primary': fact(primary), 'cleanup': [fact(item) for item in cleanup[:32]],
            'cleanup_error_count': len(cleanup), 'cleanup_errors_truncated': len(cleanup) > 32}


def _resource_cleanup_dict(value: Any) -> dict[str, Any]:
    keys = ('pending_handle_count', 'pending_attribute_list_count', 'pending_stream_count')
    if (type(value) is not dict or type(value.get('confirmed')) is not bool
            or any(type(value.get(key)) is not int or value[key] < 0 for key in keys)
            or value['confirmed'] != (not any(value[key] for key in keys))):
        raise ValueError('INVALID_RESOURCE_CLEANUP_RESULT')
    return {key: value[key] for key in ('confirmed', *keys)}


class _Session:
    def __init__(self, deadline: float) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.deadline = deadline
        self.sealed = False
        self.proc = None
        self.reader = None
        self.spawn_started = False
        self.spawn_failed = False
        self.spawn_failure_confirmed = False
        self.spawn_failure_created = False
        self.spawn_failure_pid = None
        self.spawn_failure_facts = None
        self.spawn_failure_error = None
        self.spawn_failure_resources = None
        self.value = None
        self.reason = "NONE"
        self.completed = None
        self.checks = 0
        self.terminal_cause = None
        self.terminal_notice = None
        self.terminal_receipt = None

    def live(self) -> bool:
        return not self.cancel.is_set() and time.monotonic() < self.deadline


class WatchdogWorkerRunner:
    """One capture at a time; a process-lifetime uncertainty never auto-clears.

    ``process_factory`` is an in-process injection seam, not a production CLI
    option. Returned metadata is a detached final snapshot. The owned-process
    adapter must reject late creation before resume and contain descendants.
    """
    def __init__(self, *, process_factory: Callable | None = None,
                 poll_interval: float = 0.05, cleanup_timeout: float = 2.0) -> None:
        if (type(poll_interval) not in (int, float) or not math.isfinite(poll_interval)
                or not 0 < poll_interval <= 0.25
                or type(cleanup_timeout) not in (int, float) or not math.isfinite(cleanup_timeout)
                or not 0 < cleanup_timeout <= 2.0):
            raise ValueError("INVALID_MONITOR_INTERVAL")
        self.process_factory = _spawn if process_factory is None else process_factory
        if not callable(self.process_factory):
            raise TypeError("INVALID_PROCESS_FACTORY")
        self.poll_interval = float(poll_interval)
        self.cleanup_timeout = float(cleanup_timeout)
        self.last_process_metadata: dict[str, Any] = {}
        self.termination_unconfirmed = False
        self.resource_cleanup_unconfirmed = False
        self._retained_cleanup_owner = None
        self._resource_cleanup_facts = None
        self._run_lock = threading.Lock()
        self._active: _Session | None = None

    def _command(self) -> list[str]:
        return [sys.executable, "-B", "-m", "cdpd_chatgpt_profile.worker_cli"]

    def cancel_active(self) -> bool:
        """Cancel only the current owned capture, without changing authority."""
        active = self._active
        if active is None:
            return False
        active.cancel.set()
        return True

    def __call__(self, job: WorkerRequest, *, permit: Callable[[dict], dict]) -> dict:
        return self.run(job, permit=permit)

    def _communicate(self, session: _Session, data: dict, permit: Callable,
                     command: list[str], environment: dict[str, str]) -> None:
        proc = None
        reader = None
        value = None
        reason = "NONE"
        nonces: set[str] = set()
        try:
            # The OS-owned PID must be the interpreter PID, not Windows venv's
            # redirector PID. Resolve before spawn; any unsupported runtime
            # fails closed without creating or resuming a Worker.
            command, environment, _ = plan_worker_launch(command, environment)
            if not session.live():
                raise PipeProtocolError("DEADLINE")
            with session.lock:
                session.spawn_started = True
            try:
                proc = self.process_factory(command, env=environment,
                    deadline_monotonic=session.deadline, cancellation=session.cancel)
            except BaseException as error:
                with session.lock:
                    owner = getattr(error, 'cleanup_owner', None)
                    if owner is not None:
                        # Retain exact native ownership even if this attempt
                        # was already sealed by its independent deadline.
                        self._retained_cleanup_owner = owner
                    if not session.sealed:
                        session.spawn_failed = True
                        created = getattr(error, "process_created", None)
                        session.spawn_failure_created = created if type(created) is bool else None
                        session.spawn_failure_confirmed = (getattr(error, "termination_confirmed", False) is True
                                                           and type(created) is bool)
                        pid = getattr(error, "pid", None)
                        session.spawn_failure_pid = pid if type(pid) is int and pid > 0 else None
                        try:
                            session.spawn_failure_facts = _termination_dict(getattr(error, "termination_result", None))
                        except Exception:
                            pass
                        try:
                            session.spawn_failure_error = _spawn_error_facts(error)
                        except Exception:
                            pass
                        try:
                            session.spawn_failure_resources = _resource_cleanup_dict(
                                getattr(error, 'resource_cleanup', None))
                        except Exception:
                            pass
                raise PipeProtocolError("SPAWN_FAILED") from None
            with session.lock:
                late = session.sealed
                if not late:
                    session.proc = proc
            if late:
                # This is a containment cleanup only. A sealed uncertainty is
                # never upgraded, and this capture is never resumed/replayed.
                try:
                    proc.terminate_and_confirm(timeout_s=self.cleanup_timeout)
                finally:
                    try:
                        proc.close()
                    except Exception:
                        pass
                return
            if not session.live():
                raise PipeProtocolError("DEADLINE")
            reader = _LineReader(proc.stdout.fileno(), maximum=MAX_RESULT_BYTES,
                total=MAX_RESULT_BYTES + (MAX_PERMIT_MESSAGES + 1) * (MAX_PERMIT_BYTES + 1),
                messages=MAX_PERMIT_MESSAGES + 1)
            with session.lock:
                if not session.sealed:
                    session.reader = reader
            _write_line(proc.stdin.fileno(), data, maximum=MAX_REQUEST_BYTES, deadline=session.deadline)
            while True:
                if not session.live():
                    raise PipeProtocolError("DEADLINE")
                kind, raw = reader.next(session.deadline)
                if kind == "EOF":
                    if value is None:
                        raise PipeProtocolError("MISSING_RESULT")
                    break
                if value is not None:
                    raise PipeProtocolError("OUTPUT_AFTER_RESULT")
                message = strict_json(raw, max_bytes=MAX_RESULT_BYTES)
                if type(message) is not dict:
                    raise PipeProtocolError("MESSAGE_INVALID")
                if message.get("schema") == "cdpd.capture-worker-permit-check/v1":
                    if (len(raw) > MAX_PERMIT_BYTES or len(nonces) >= MAX_PERMIT_MESSAGES
                            or not _valid_challenge(message, data, nonces)):
                        raise PipeProtocolError("PERMIT_INVALID")
                    nonces.add(message["nonce"])
                    with session.lock:
                        if session.sealed or not session.live():
                            raise PipeProtocolError("DEADLINE")
                        session.checks += 1
                        if is_terminal(message):
                            session.terminal_cause = session.terminal_cause or message["phase"].removeprefix("TERMINAL:")
                            if session.terminal_notice is None:
                                session.terminal_notice = dict(message)
                    # Record an authenticated first notice before persistence
                    # can block. Only this communication daemon may block;
                    # the owned-process deadline monitor remains independent.
                    if is_terminal(message):
                        prepare = getattr(permit, "prepare", None)
                        if callable(prepare):
                            prepare(dict(message))
                    if not session.live():
                        raise PipeProtocolError("DEADLINE")
                    permit_deadline = min(session.deadline, time.monotonic() + PERMIT_SECONDS)
                    try:
                        response = _bounded_call(lambda: permit(dict(message)), permit_deadline)
                    except PipeProtocolError as error:
                        raise PipeProtocolError("PERMIT_TIMEOUT" if error.code == "DEADLINE"
                                                else "PERMIT_CALLBACK_FAILED") from None
                    if not _valid_response(response, message):
                        if is_terminal(message):
                            response = terminal_receipt(message)
                        else:
                            raise PipeProtocolError("PERMIT_INVALID")
                    if not session.live():
                        raise PipeProtocolError("DEADLINE")
                    with session.lock:
                        if session.sealed:
                            raise PipeProtocolError("DEADLINE")
                        if is_terminal(message):
                            session.terminal_receipt = dict(response)
                    _write_line(proc.stdin.fileno(), response, maximum=MAX_PERMIT_BYTES, deadline=permit_deadline)
                elif not _valid_result(message, data, proc.pid):
                    raise PipeProtocolError("RESULT_INVALID")
                else:
                    with session.lock:
                        first_cause = session.terminal_cause
                    if first_cause is not None and (message["structure"] is not None
                            or message["failure_code"] != first_cause):
                        raise PipeProtocolError("RESULT_INVALID")
                    value = message
            remaining = session.deadline - time.monotonic()
            if remaining <= 0 or session.cancel.is_set():
                raise PipeProtocolError("DEADLINE")
            exit_code = proc.wait(timeout=remaining)
            if not session.live():
                raise PipeProtocolError("DEADLINE")
            if exit_code != 0:
                raise PipeProtocolError("CHILD_EXITED_ERROR")
        except PipeProtocolError as error:
            reason = error.code
        except subprocess.TimeoutExpired:
            reason = "DEADLINE"
        except BaseException:
            reason = "SPAWN_FAILED" if proc is None else "PROTOCOL_INVALID"
        finally:
            with session.lock:
                if not session.sealed:
                    session.value = value if reason == "NONE" else None
                    session.reason = reason
                    session.completed = time.monotonic()
            session.done.set()

    def _cleanup(self, proc) -> dict[str, Any]:
        # Native cleanup itself can fail or stall. Bound the caller's wait;
        # never turn a timeout into a confirmed process-termination claim.
        deadline = time.monotonic() + self.cleanup_timeout
        try:
            return _termination_dict(_bounded_call(
                lambda: proc.terminate_and_confirm(timeout_s=self.cleanup_timeout), deadline))
        except BaseException:
            return {"requested": True, "confirmed": False,
                    "reason": "WORKER_TERMINATION_UNCONFIRMED", "exit_code": None,
                    "active_processes": None, "native_error": None}

    def run(self, job: WorkerRequest, *, permit: Callable[[dict], dict]) -> dict:
        if not isinstance(job, WorkerRequest) or not callable(permit):
            raise WorkerInputError()
        data = WorkerRequest.from_dict(job.to_dict()).to_dict()
        if not self._run_lock.acquire(blocking=False):
            raise WorkerInputError("WORKER_ALREADY_ACTIVE")
        try:
            return self._run_locked(data, permit)
        finally:
            self._active = None
            self._run_lock.release()

    def _run_locked(self, data: dict, permit: Callable) -> dict:
        if self.termination_unconfirmed or self.resource_cleanup_unconfirmed:
            block_reason = ('WORKER_TERMINATION_UNCONFIRMED' if self.termination_unconfirmed
                            else 'WORKER_RESOURCE_CLEANUP_UNCONFIRMED')
            self.last_process_metadata = {
                "schema": "cdpd.l07-capture-process-metadata/v1", "spawn_count": 0, "pid": None,
                "termination": "NOT_STARTED", "failure_reason": block_reason,
                "termination_unconfirmed": self.termination_unconfirmed, "new_worker_prohibited": True,
                "resource_cleanup_unconfirmed": self.resource_cleanup_unconfirmed,
                "timed_out": False, "elapsed_ms": 0, "sealed": True}
            if self._resource_cleanup_facts is not None:
                self.last_process_metadata['resource_cleanup'] = deepcopy(self._resource_cleanup_facts)
                self.last_process_metadata['resource_cleanup_scope'] = 'PRIOR_OWNED_WORKER'
            return _failed_result(data, pid=0, termination="NOT_STARTED")
        started = time.monotonic()
        deadline = started + data["capture_request"]["timeout_seconds"]
        session = _Session(deadline)
        self._active = session
        command = self._command()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        communication = threading.Thread(target=self._communicate,
            args=(session, data, permit, command, environment), daemon=True,
            name="cdpd-l07-worker-communication")
        communication.start()
        monitor_reason = None
        while not session.done.is_set():
            if session.cancel.is_set():
                monitor_reason = "CANCELLED"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                monitor_reason = "DEADLINE"
                break
            session.done.wait(min(self.poll_interval, remaining))
        # Seal while holding only the in-memory state lock. No I/O, permit,
        # process query or writer lock is ever taken in the monitoring loop.
        with session.lock:
            if monitor_reason is None and (session.completed is None or session.completed > deadline):
                monitor_reason = "DEADLINE"
            session.sealed = True
            session.cancel.set()
            proc = session.proc
            reader = session.reader
            reason = monitor_reason or session.reason
            value = deepcopy(session.value) if reason == "NONE" else None
            first_cause = session.terminal_cause
            notice = deepcopy(session.terminal_notice)
            receipt = deepcopy(session.terminal_receipt)
            checks = session.checks
            spawn_started = session.spawn_started
            spawn_failed = session.spawn_failed
            spawn_failure_confirmed = session.spawn_failure_confirmed
            spawn_failure_created = session.spawn_failure_created
            spawn_failure_pid = session.spawn_failure_pid
            spawn_failure_facts = deepcopy(session.spawn_failure_facts)
            spawn_failure_error = deepcopy(session.spawn_failure_error)
            spawn_failure_resources = deepcopy(session.spawn_failure_resources)
        monitor_decided = time.monotonic()
        if reader is not None:
            reader.stop.set()
        pid = proc.pid if proc is not None else spawn_failure_pid
        spawn_facts_consistent = None
        if proc is not None:
            termination_facts = self._cleanup(proc)
        elif spawn_failed and spawn_failure_facts is not None:
            # A returned, validated UNCONFIRMED cleanup result still carries
            # real request/query facts; it is not an unreturned spawn call.
            termination_facts = spawn_failure_facts
            spawn_facts_consistent = (
                spawn_failure_facts['confirmed'] == spawn_failure_confirmed
                and ((spawn_failure_created is True and spawn_failure_pid is not None)
                     or (spawn_failure_created is False and spawn_failure_pid is None
                         and not spawn_failure_facts['requested']
                         and spawn_failure_facts['exit_code'] is None)))
            if not spawn_facts_consistent and termination_facts['confirmed']:
                termination_facts = {**termination_facts, 'confirmed': False,
                    'reason': 'SPAWN_FAILURE_FACTS_INCONSISTENT'}
        elif spawn_failed and spawn_failure_confirmed:
            termination_facts = spawn_failure_facts or {
                "requested": spawn_failure_created, "confirmed": True,
                "reason": "SPAWN_FAILURE_CONTAINED" if spawn_failure_created else "NOT_CREATED",
                "exit_code": None, "active_processes": 0, "native_error": None}
        elif not spawn_started:
            termination_facts = {"requested": False, "confirmed": True,
                "reason": "NOT_CREATED", "exit_code": None, "active_processes": 0, "native_error": None}
        else:
            termination_facts = {"requested": False, "confirmed": False,
                "reason": "SPAWN_RETURN_UNCONFIRMED", "exit_code": None,
                "active_processes": None, "native_error": None}
        confirmed = termination_facts["confirmed"]
        if spawn_failure_resources is not None and not spawn_failure_resources['confirmed']:
            self.resource_cleanup_unconfirmed = True
            self._resource_cleanup_facts = deepcopy(spawn_failure_resources)
            value = None
        if not confirmed:
            self.termination_unconfirmed = True
            value = None
            termination = "UNCONFIRMED"
        elif proc is None:
            termination = "NOT_STARTED" if not spawn_failure_created else "KILLED"
        elif reason == "NONE" and termination_facts["requested"]:
            # A valid leader result does not license a surviving descendant.
            reason = "DESCENDANTS_AFTER_RESULT"
            value = None
            termination = "KILLED"
        elif reason == "NONE":
            termination = "NORMAL"
        else:
            termination = "KILLED" if termination_facts["requested"] else "EXITED_ERROR"
        created_known = proc is not None or not spawn_started or (spawn_failed and type(spawn_failure_created) is bool)
        spawn_count = int(proc is not None or spawn_failure_created is True) if created_known else None
        metadata = {
            "schema": "cdpd.l07-capture-process-metadata/v1", "spawn_count": spawn_count,
            "spawn_count_known": created_known,
            "spawn_attempt_count": int(spawn_started), "pid": pid,
            "child_exit_code": termination_facts["exit_code"], "termination": termination,
            "failure_reason": ('WORKER_TERMINATION_UNCONFIRMED' if not confirmed else
                'WORKER_RESOURCE_CLEANUP_UNCONFIRMED' if self.resource_cleanup_unconfirmed else reason),
            "protocol_failure_reason": reason, "termination_facts": termination_facts,
            "termination_unconfirmed": not confirmed,
            "new_worker_prohibited": self.termination_unconfirmed or self.resource_cleanup_unconfirmed,
            "resource_cleanup_unconfirmed": self.resource_cleanup_unconfirmed,
            "timed_out": reason in {"DEADLINE", "PERMIT_TIMEOUT"}, "permit_checks": checks,
            "stdout_bytes": 0 if reader is None else reader.bytes_received,
            "stderr_policy": "DISCARD_NO_FORWARDING", "command": list(command), "platform": sys.platform,
            "started_monotonic": started, "deadline_monotonic": deadline,
            "monitor_decision_monotonic": monitor_decided,
            "deadline_overrun_ms": max(0, int((monitor_decided - deadline) * 1000)),
            "poll_interval_seconds": self.poll_interval, "cleanup_timeout_seconds": self.cleanup_timeout,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)), "sealed": True,
            "supervision_scope": (getattr(proc, "supervision_scope", "OWNED_PROCESS_BACKEND_UNAVAILABLE")
                                  if proc is not None else "NO_RETURNED_OWNED_PROCESS"),
        }
        if not confirmed:
            metadata["termination_failure_code"] = "WORKER_TERMINATION_UNCONFIRMED"
        if spawn_failure_error is not None:
            metadata["spawn_error"] = spawn_failure_error
        if spawn_failure_resources is not None:
            metadata['resource_cleanup'] = spawn_failure_resources
        if spawn_facts_consistent is not None:
            metadata['spawn_failure_facts_consistent'] = spawn_facts_consistent
            if not spawn_facts_consistent:
                metadata['reported_spawn_failure_facts'] = spawn_failure_facts
        if notice is not None:
            metadata["terminal_notice"] = notice
        if receipt is not None:
            metadata["terminal_receipt"] = receipt
        self.last_process_metadata = deepcopy(metadata)
        if proc is not None:
            # Handle/pipe closure is also kept off the bounded caller path.
            # OwnedWorker keeps exact ownership and makes close idempotent.
            def close_owned():
                try:
                    proc.close()
                except BaseException:
                    pass
            threading.Thread(target=close_owned, daemon=True, name="cdpd-l07-owned-close").start()
        # The unchanged result contract reserves pid=0 for NOT_STARTED. Process
        # uncertainty is independently explicit above; do not invent a PID.
        return value if value is not None else _failed_result(data, pid=pid or 0,
            termination=termination if pid else "NOT_STARTED", original_failure=first_cause)
