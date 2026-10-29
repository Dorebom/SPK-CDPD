"""L07-owned Worker lifetime and registered process identity.

Windows: atomic job-list assignment at suspended creation, membership check,
then resume; private KILL_ON_JOB_CLOSE Job and explicit pipe handle allowlist.
Linux: a dedicated session/process group and an exec wrapper that arms a
parent-death signal for the leader. It is not a Windows acceptance substitute;
descendant escape and whole-tree cleanup after Supervisor death are not assured.

The deadline/cancellation monitor is independent of DOM, pipe and disk calls.
Python cannot forcibly interrupt CreateProcess/Popen itself. Late creation is
cleaned up without Windows resume. All uncertain termination stays uncertain.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

from ._l07_win32 import Win32API, NativeCreationError, NativeCleanupResources


@dataclass(frozen=True)
class TerminationResult:
    requested: bool
    confirmed: bool
    reason: str
    exit_code: int | None
    active_processes: int | None
    native_error: int | str | None = None


class OwnedProcessError(RuntimeError):
    def __init__(self, code, *, termination_confirmed=True, process_created=False,
                 pid=None, native_error=None, termination_result=None,
                 primary_error=None, cleanup_errors=(), process_reference=None,
                 resource_cleanup=None, cleanup_owner=None):
        super().__init__(code)
        self.code = code
        self.termination_confirmed = termination_confirmed
        self.process_created = process_created
        self.pid = pid
        self.native_error = native_error
        self.termination_result = termination_result
        self.primary_error = primary_error
        self.cleanup_errors = tuple(cleanup_errors)
        self.process_reference = process_reference
        self.resource_cleanup = resource_cleanup
        self.cleanup_owner = cleanup_owner


def _windows():
    return sys.platform == 'win32'


def _cancelled(cancellation):
    return cancellation is not None and cancellation.is_set()


def _native_error(error):
    return getattr(error, 'winerror', None) or getattr(error, 'errno', None) or type(error).__name__


def _linux_stat(pid):
    raw = Path('/proc', str(pid), 'stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    return fields[0], int(fields[2]), fields[19]


def _require_linux_proc_namespace():
    # Some execution sandboxes expose host /proc inside a different PID
    # namespace. Never query or signal a group based on that mismatched view.
    observed = Path('/proc/self/stat').read_text().split(' ', 1)[0]
    if observed != str(os.getpid()):
        raise OwnedProcessError('PROC_PID_NAMESPACE_MISMATCH')


class _PosixBackend:
    scope = 'LINUX_OWNED_PROCESS_GROUP_SYNTHETIC_ONLY'

    def __init__(self, argv, cwd, env):
        if not sys.platform.startswith('linux') or not hasattr(os, 'WNOWAIT'):
            raise OwnedProcessError('UNSUPPORTED_PROCESS_BACKEND')
        _require_linux_proc_namespace()
        # Do not use preexec_fn in this multithreaded Supervisor. The separate
        # interpreter arms PDEATHSIG and checks our still-current parent PID
        # before execve. Exec preserves the owned PID, process group and pipes.
        wrapper = str(Path(__file__).with_name('_l07_posix_exec.py').resolve())
        command = [sys.executable, '-I', '-S', '-B', wrapper, str(os.getpid()), '--', *argv]
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, close_fds=True,
            start_new_session=True)
        self.pid = self.proc.pid
        self.stdin, self.stdout = self.proc.stdin, self.proc.stdout
        self._exit = None
        self._ownership_lost = False
        self._closed = False

    def poll(self):
        if self._exit is not None:
            return self._exit
        try:
            observed = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            self._ownership_lost = True
            raise OwnedProcessError('OWNED_CHILD_REAPED_EXTERNALLY', termination_confirmed=False, process_created=True, pid=self.pid)
        if observed is not None:
            self._exit = observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status
        return self._exit

    def active(self):
        # Keeping the owned leader unreaped until close reserves its PID/PGID;
        # a terminal leader cannot be confused with a later PID reuse.
        self.poll()
        if self._ownership_lost:
            raise OwnedProcessError('OWNERSHIP_UNCONFIRMED', termination_confirmed=False)
        count = 0
        with os.scandir('/proc') as entries:
            for item in entries:
                if not item.name.isdecimal():
                    continue
                try:
                    state, group, _ = _linux_stat(int(item.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    # A process we cannot inspect cannot establish emptiness.
                    raise OwnedProcessError('PROCESS_GROUP_QUERY_DENIED', termination_confirmed=False)
                if group == self.pid and state not in {'Z', 'X'}:
                    count += 1
        return count

    def kill(self):
        if self._ownership_lost or self._closed:
            raise OwnedProcessError('OWNERSHIP_UNCONFIRMED', termination_confirmed=False)
        self.poll()
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Reap only our child, and only after ownership operations are over.
        if self._exit is not None:
            self.proc.wait(timeout=0)


class _WindowsBackend:
    scope = 'WINDOWS_PRIVATE_JOB_OBJECT'

    def __init__(self, api, job, created):
        self.api, self.job, self.created = api, job, created
        self.pid = created.pid
        self.stdin, self.stdout = created.stdin, created.stdout

    def poll(self):
        return self.api.poll_process(self.created.process)

    def active(self):
        return self.api.active_processes(self.job)

    def kill(self):
        self.api.terminate_job(self.job)
        # In the exceptional membership-failure path, the owned suspended
        # process must also be stopped by its exact returned handle.
        if not self.api.is_in_job(self.created.process, self.job) and self.poll() is None:
            self.api.terminate_process(self.created.process)

    def close(self):
        errors = []
        for name, handle in [('thread', self.created.thread), ('process', self.created.process), ('job', self.job)]:
            if handle is not None:
                try:
                    self.api.close_handle(handle)
                except BaseException as error:
                    errors.append(error)
                else:
                    if name == 'job': self.job = None
                    else: setattr(self.created, name, None)
        if self.created.cleanup_resources is not None:
            errors.extend(self.created.cleanup_resources.close())
        if errors:
            raise OwnedProcessError('OWNED_HANDLE_CLOSE_FAILED',
                termination_confirmed=False, process_created=True, pid=self.pid,
                native_error=_native_error(errors[0]), primary_error=errors[0],
                cleanup_errors=errors[1:]) from None

    def resource_snapshot(self):
        extra = self.created.cleanup_resources
        snapshot = (extra.snapshot() if extra is not None else {
            'confirmed': True, 'pending_handle_count': 0,
            'pending_attribute_list_count': 0, 'pending_stream_count': 0})
        snapshot['pending_handle_count'] += sum(handle is not None for handle in (
            self.created.thread, self.created.process, self.job))
        snapshot['confirmed'] = not any(snapshot[key] for key in (
            'pending_handle_count', 'pending_attribute_list_count', 'pending_stream_count'))
        return snapshot


class OwnedWorker:
    """A small Popen-compatible boundary with independent deadline enforcement."""

    def __init__(self, backend, argv, deadline_monotonic, cancellation):
        self._backend = backend
        self.args = list(argv)
        self.pid, self.stdin, self.stdout = backend.pid, backend.stdin, backend.stdout
        self.supervision_scope = backend.scope
        self.deadline_monotonic = deadline_monotonic
        self._cancellation = cancellation
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._requested = False
        self._last_exit = None
        self._result = None
        self._deadline_expired = False
        self._native_failure = None
        self._cleanup_errors = []
        self._watcher = None

    @classmethod
    def spawn(cls, argv, *, cwd=None, env=None, deadline_monotonic, cancellation=None):
        if (not isinstance(argv, (list, tuple)) or not argv or any(type(a) is not str or '\0' in a for a in argv)
                or not os.path.isabs(argv[0])):
            raise OwnedProcessError('INVALID_ARGV')
        if type(deadline_monotonic) not in (int, float) or not math.isfinite(deadline_monotonic):
            raise OwnedProcessError('INVALID_DEADLINE')
        if cancellation is not None and not callable(getattr(cancellation, 'is_set', None)):
            raise OwnedProcessError('INVALID_CANCELLATION')
        if time.monotonic() >= deadline_monotonic or _cancelled(cancellation):
            raise OwnedProcessError('DEADLINE_OR_CANCELLED_BEFORE_SPAWN')
        worker = None
        job = None
        api = None
        created = None
        try:
            if _windows():
                api = Win32API()
                job = api.create_job()
                try:
                    created = api.create_suspended(list(argv), cwd, env, job)
                except NativeCreationError as error:
                    created = error.created
                    if created is not None:
                        try:
                            worker = cls(_WindowsBackend(api, job, created), argv, deadline_monotonic, cancellation)
                        except BaseException as cleanup_error:
                            error.cleanup_errors += (cleanup_error,)
                    raise
                worker = cls(_WindowsBackend(api, job, created), argv, deadline_monotonic, cancellation)
                worker._start_monitor()
                if not api.is_in_job(created.process, job):
                    raise OwnedProcessError('JOB_ASSIGNMENT_NOT_CONFIRMED')
                if time.monotonic() >= deadline_monotonic or _cancelled(cancellation):
                    raise OwnedProcessError('DEADLINE_OR_CANCELLED_BEFORE_RESUME')
                api.resume(created.thread)
                api.close_handle(created.thread)
                created.thread = None
            else:
                worker = cls(_PosixBackend(list(argv), cwd, env), argv, deadline_monotonic, cancellation)
                worker._start_monitor()
            if time.monotonic() >= deadline_monotonic or _cancelled(cancellation):
                raise OwnedProcessError('DEADLINE_OR_CANCELLED_AFTER_SPAWN')
            return worker
        except BaseException as error:
            primary_error = getattr(error, 'primary_error', None) or error
            cleanup_errors = list(getattr(error, 'cleanup_errors', ()))
            if job is None and isinstance(error, NativeCreationError):
                job = error.job
            # Keep an immutable identity snapshot before close retires handles.
            reference = None if created is None else {
                'pid': created.pid, 'process': created.process,
                'thread': created.thread, 'job': job,
            }
            if worker is not None:
                try:
                    result = worker.terminate_and_confirm(timeout_s=2.0)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    result = TerminationResult(worker._requested, False,
                        'WORKER_TERMINATION_UNCONFIRMED', worker._last_exit,
                        None, _native_error(cleanup_error))
                    # Closing may attempt containment; it cannot reclassify
                    # this already unknown termination as confirmed.
                    with worker._lock:
                        worker._result = result
                        worker._stop.set()
                try:
                    worker.close()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                cleanup_errors.extend(worker.cleanup_errors)
                resources = worker.resource_cleanup
                raise OwnedProcessError(getattr(error, 'code', 'OWNED_SPAWN_FAILED'),
                    termination_confirmed=result.confirmed, process_created=True, pid=worker.pid,
                    native_error=_native_error(primary_error), termination_result=result,
                    primary_error=primary_error, cleanup_errors=cleanup_errors,
                    process_reference=reference, resource_cleanup=resources,
                    cleanup_owner=worker if not resources['confirmed'] else None) from None
            native_resources = getattr(error, 'cleanup_resources', None)
            if created is not None:
                # The Python owner itself could not be constructed. Retire
                # every held resource once, preserving the created PID and an
                # unknown lifetime; Job close alone is not exit evidence.
                native_resources = native_resources or created.cleanup_resources or NativeCleanupResources(api)
                for handle in (created.thread, created.process, job):
                    if handle is not None and handle not in native_resources.handles:
                        native_resources.handles.append(handle)
                cleanup_errors.extend(native_resources.close())
                for stream in (created.stdin, created.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except BaseException as cleanup_error:
                            cleanup_errors.append(cleanup_error)
                resources = native_resources.snapshot()
                resources['pending_stream_count'] += sum(
                    stream is not None and not stream.closed for stream in (created.stdin, created.stdout))
                resources['confirmed'] = not any(resources[key] for key in (
                    'pending_handle_count', 'pending_attribute_list_count', 'pending_stream_count'))
                result = TerminationResult(False, False,
                    'WORKER_TERMINATION_UNCONFIRMED', None, None,
                    _native_error(primary_error))
                raise OwnedProcessError('OWNED_SPAWN_FAILED',
                    termination_confirmed=False, process_created=True, pid=created.pid,
                    native_error=_native_error(primary_error), termination_result=result,
                    primary_error=primary_error, cleanup_errors=cleanup_errors,
                    process_reference=reference, resource_cleanup=resources,
                    cleanup_owner=(created, native_resources) if not resources['confirmed'] else None) from None
            if job is not None and (native_resources is None or job not in native_resources.handles):
                try:
                    api.close_handle(job)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    if native_resources is None:
                        native_resources = NativeCleanupResources(api, handles=(job,))
                    else:
                        native_resources.handles.append(job)
            if native_resources is not None:
                cleanup_errors.extend(native_resources.close())
            resources = (native_resources.snapshot() if native_resources is not None else {
                'confirmed': True, 'pending_handle_count': 0,
                'pending_attribute_list_count': 0, 'pending_stream_count': 0})
            if isinstance(error, OwnedProcessError) and not cleanup_errors:
                raise
            raise OwnedProcessError(getattr(error, 'code', 'OWNED_SPAWN_FAILED'),
                native_error=_native_error(primary_error), primary_error=primary_error,
                cleanup_errors=cleanup_errors, resource_cleanup=resources,
                cleanup_owner=native_resources if not resources['confirmed'] else None) from None

    @property
    def returncode(self):
        return self.poll()

    @property
    def termination_result(self):
        with self._lock: return self._result

    @property
    def cleanup_errors(self):
        with self._lock: return tuple(self._cleanup_errors)

    @property
    def resource_cleanup(self):
        with self._lock:
            snapshot = (self._backend.resource_snapshot() if isinstance(self._backend, _WindowsBackend)
                else {'confirmed': self._closed, 'pending_handle_count': 0,
                      'pending_attribute_list_count': 0, 'pending_stream_count': 0})
            snapshot['pending_stream_count'] += sum(
                stream is not None and not stream.closed for stream in (self.stdin, self.stdout))
            snapshot['confirmed'] = self._closed and not any(snapshot[key] for key in (
                'pending_handle_count', 'pending_attribute_list_count', 'pending_stream_count'))
            return snapshot

    @property
    def deadline_expired(self):
        with self._lock: return self._deadline_expired

    def _start_monitor(self):
        with self._lock:
            if self._watcher is None:
                self._watcher = threading.Thread(target=self._monitor, name='cdpd-l07-owned-deadline', daemon=True)
                self._watcher.start()

    def _monitor(self):
        while not self._stop.is_set():
            remaining = self.deadline_monotonic - time.monotonic()
            if remaining <= 0 or _cancelled(self._cancellation):
                with self._lock: self._deadline_expired = remaining <= 0
                self.terminate_and_confirm(timeout_s=2.0)
                return
            self._stop.wait(min(0.05, remaining))

    def poll(self):
        with self._lock:
            if self._closed: return self._last_exit
            self._last_exit = self._backend.poll()
            return self._last_exit

    def wait(self, timeout=None):
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('INVALID_WAIT_TIMEOUT')
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None: return code
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.01 if end is None else max(0, min(0.01, end-time.monotonic())))

    def kill(self):
        """Request one termination of only the held job/group; no blocking wait."""
        with self._lock:
            if self._closed or self._requested or (self._result is not None and self._result.confirmed):
                return
            self._requested = True
            try:
                self._backend.kill()
            except Exception as error:
                self._native_failure = _native_error(error)
                raise

    def terminate_and_confirm(self, timeout_s=2.0):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 2:
            raise ValueError('TERMINATION_CONFIRMATION_BOUND_MUST_BE_0_TO_2_SECONDS')
        end = time.monotonic() + timeout_s
        active = None
        with self._lock:
            if self._result is not None:
                return self._result
            if self._closed:
                return self._result or TerminationResult(self._requested, False, 'WORKER_TERMINATION_UNCONFIRMED', self._last_exit, None, 'CLOSED_WITHOUT_CONFIRMATION')
        while True:
            with self._lock:
                if self._result is not None:
                    return self._result
                if self._closed:
                    return self._result or TerminationResult(self._requested, False, 'WORKER_TERMINATION_UNCONFIRMED', self._last_exit, None, 'CLOSED_WITHOUT_CONFIRMATION')
                try:
                    self._last_exit = self._backend.poll()
                    active = self._backend.active()
                    # A job query of zero alone cannot authenticate the leader
                    # when a failed native membership check left it outside.
                    if active == 0 and self._last_exit is not None:
                        self._result = TerminationResult(self._requested, True,
                            'OWNED_TREE_TERMINATED' if self._requested else 'ALREADY_EXITED', self._last_exit, 0, self._native_failure)
                        self._stop.set()
                        return self._result
                except Exception as error:
                    self._native_failure = _native_error(error)
                if not self._requested:
                    try: self.kill()
                    except Exception: pass
            if time.monotonic() >= end:
                with self._lock:
                    if self._result is not None:
                        return self._result
                    self._result = TerminationResult(self._requested, False, 'WORKER_TERMINATION_UNCONFIRMED', self._last_exit, active, self._native_failure)
                    return self._result
            time.sleep(min(0.02, max(0, end-time.monotonic())))

    def close(self):
        with self._lock:
            if self._closed: return
            needs_cleanup = self._result is None
        if needs_cleanup:
            self.terminate_and_confirm(timeout_s=2.0)
        with self._lock:
            if self._closed: return
            self._stop.set()
            self._closed = True
            try:
                self._backend.close()
            except BaseException as error:
                self._native_failure = _native_error(error)
                self._cleanup_errors.append(getattr(error, 'primary_error', None) or error)
                self._cleanup_errors.extend(getattr(error, 'cleanup_errors', ()))
            for stream in (self.stdin, self.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        self._cleanup_errors.append(error)


class RegisteredProcess:
    """Read-only fixed process identity. No terminate capability is exposed."""
    def __init__(self, pid, identity, handle, api=None):
        self.pid, self.identity, self._handle, self._api = pid, identity, handle, api
        self._closed = False

    @classmethod
    def _open(cls, pid):
        if type(pid) is not int or pid <= 0:
            raise OwnedProcessError('INVALID_REGISTERED_PID')
        if _windows():
            api = Win32API(); handle = api.open_process(pid)
            try:
                return cls(pid, api.creation_identity(handle), handle, api)
            except Exception:
                api.close_handle(handle); raise
        if not sys.platform.startswith('linux') or not hasattr(os, 'pidfd_open'):
            raise OwnedProcessError('PIDFD_REQUIRED')
        _require_linux_proc_namespace()
        fd = os.pidfd_open(pid, 0)
        try:
            if select.select([fd], [], [], 0)[0]:
                raise OwnedProcessError('REGISTERED_PROCESS_ALREADY_EXITED')
            _, _, start = _linux_stat(pid)
            if select.select([fd], [], [], 0)[0]:
                raise OwnedProcessError('REGISTERED_PROCESS_IDENTITY_CHANGED')
            return cls(pid, 'linux-proc-starttime:' + start, fd)
        except Exception:
            os.close(fd); raise

    @classmethod
    def open(cls, pid, expected_creation_identity):
        if type(expected_creation_identity) is not str or not expected_creation_identity:
            raise OwnedProcessError('EXPECTED_CREATION_IDENTITY_REQUIRED')
        registered = cls._open(pid)
        if registered.identity != expected_creation_identity:
            registered.close()
            raise OwnedProcessError('REGISTERED_PROCESS_IDENTITY_MISMATCH')
        return registered

    def is_alive(self):
        if self._closed:
            raise OwnedProcessError('REGISTERED_HANDLE_CLOSED')
        if self._api is not None:
            return self._api.poll_process(self._handle) is None
        return not bool(select.select([self._handle], [], [], 0)[0])

    def close(self):
        if not self._closed:
            self._closed = True
            if self._api is None: os.close(self._handle)
            else: self._api.close_handle(self._handle)


def process_creation_identity(pid):
    registered = RegisteredProcess._open(pid)
    try: return registered.identity
    finally: registered.close()


def current_process_in_job():
    """Windows safety preflight; False on the Linux synthetic backend only."""
    return Win32API().current_process_in_job() if _windows() else False
