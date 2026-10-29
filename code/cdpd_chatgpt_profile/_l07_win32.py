"""Narrow Win32 handle API for L07. No browser or consumer is ever assigned.

Imported on non-Windows for type-safe test collection, instantiated only on
Windows. Native outcomes remain NOT_RUN on Linux; injected tests are models.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes as W
import os
import subprocess


@dataclass
class CreatedProcess:
    pid: int
    process: object
    thread: object
    stdin: object = None
    stdout: object = None
    cleanup_resources: object = None


class NativeCleanupResources:
    """Owner of exact resources whose first close failed; no lookup or retry loop."""
    def __init__(self, api, *, handles=(), fds=(), attributes=None):
        self.api = api
        self.handles = list(handles)
        self.fds = list(fds)
        self.attributes = attributes

    def close(self):
        errors = []
        if self.attributes is not None:
            try:
                self.api.k.DeleteProcThreadAttributeList(self.attributes)
            except BaseException as error:
                errors.append(error)
            else:
                self.attributes = None
        for handle in tuple(self.handles):
            try:
                self.api.close_handle(handle)
            except BaseException as error:
                errors.append(error)
            else:
                self.handles.remove(handle)
        for fd in tuple(self.fds):
            try:
                os.close(fd)
            except BaseException as error:
                errors.append(error)
            else:
                self.fds.remove(fd)
        return tuple(errors)

    def snapshot(self):
        return {'confirmed': not self.handles and not self.fds and self.attributes is None,
                'pending_handle_count': len(self.handles),
                'pending_attribute_list_count': int(self.attributes is not None),
                'pending_stream_count': len(self.fds)}


class NativeCreationError(OSError):
    def __init__(self, error, created, *, cleanup_errors=(), job=None,
                 unclosed_handles=(), cleanup_resources=None):
        super().__init__(getattr(error, 'winerror', None) or getattr(error, 'errno', None), 'WIN32_CREATE_FAILED')
        self.created = created
        self.primary_error = error
        self.cleanup_errors = tuple(cleanup_errors)
        self.job = job
        self.unclosed_handles = tuple(unclosed_handles)
        self.cleanup_resources = cleanup_resources


class Win32API:
    """All kill/close/query calls consume held handles, never a looked-up PID."""

    def __init__(self, *, kernel=None):
        if kernel is None and os.name != 'nt':
            raise OSError('WINDOWS_REQUIRED')
        self.k = C.WinDLL('kernel32', use_last_error=True) if kernel is None else kernel
        H, D, P, Z = W.HANDLE, W.DWORD, C.c_void_p, C.c_size_t

        class SA(C.Structure):
            _fields_ = [('nLength', D), ('lpSecurityDescriptor', P), ('bInheritHandle', W.BOOL)]
        class SI(C.Structure):
            _fields_ = [('cb', D), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR),
                ('dwX', D), ('dwY', D), ('dwXSize', D), ('dwYSize', D), ('dwXCountChars', D),
                ('dwYCountChars', D), ('dwFillAttribute', D), ('dwFlags', D), ('wShowWindow', W.WORD),
                ('cbReserved2', W.WORD), ('lpReserved2', C.POINTER(W.BYTE)),
                ('hStdInput', H), ('hStdOutput', H), ('hStdError', H)]
        class SIEX(C.Structure):
            _fields_ = [('StartupInfo', SI), ('lpAttributeList', P)]
        class PI(C.Structure):
            _fields_ = [('hProcess', H), ('hThread', H), ('dwProcessId', D), ('dwThreadId', D)]
        class BASIC_LIMIT(C.Structure):
            _fields_ = [('PerProcessUserTimeLimit', C.c_longlong), ('PerJobUserTimeLimit', C.c_longlong),
                ('LimitFlags', D), ('MinimumWorkingSetSize', Z), ('MaximumWorkingSetSize', Z),
                ('ActiveProcessLimit', D), ('Affinity', Z), ('PriorityClass', D), ('SchedulingClass', D)]
        class IO_COUNTERS(C.Structure):
            _fields_ = [(n, C.c_ulonglong) for n in ('ReadOperationCount','WriteOperationCount',
                'OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class EXT_LIMIT(C.Structure):
            _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', Z), ('JobMemoryLimit', Z), ('PeakProcessMemoryUsed', Z), ('PeakJobMemoryUsed', Z)]
        class ACCOUNTING(C.Structure):
            _fields_ = [(n, C.c_longlong) for n in ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')] + [
                (n, D) for n in ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]

        self.SA, self.SIEX, self.PI, self.EXT_LIMIT, self.ACCOUNTING = SA, SIEX, PI, EXT_LIMIT, ACCOUNTING
        signatures = {
            'CreateJobObjectW': ([P, W.LPCWSTR], H),
            'SetInformationJobObject': ([H, C.c_int, P, D], W.BOOL),
            'QueryInformationJobObject': ([H, C.c_int, P, D, P], W.BOOL),
            'SetHandleInformation': ([H, D, D], W.BOOL),
            'GetHandleInformation': ([H, C.POINTER(D)], W.BOOL),
            'CreatePipe': ([C.POINTER(H), C.POINTER(H), P, D], W.BOOL),
            'CreateFileW': ([W.LPCWSTR, D, D, P, D, D, H], H),
            'InitializeProcThreadAttributeList': ([P, D, D, C.POINTER(Z)], W.BOOL),
            'UpdateProcThreadAttribute': ([P, D, Z, P, Z, P, P], W.BOOL),
            'DeleteProcThreadAttributeList': ([P], None),
            'CreateProcessW': ([W.LPCWSTR, W.LPWSTR, P, P, W.BOOL, D, P, W.LPCWSTR, P, C.POINTER(PI)], W.BOOL),
            'IsProcessInJob': ([H, H, C.POINTER(W.BOOL)], W.BOOL),
            'ResumeThread': ([H], D),
            'WaitForSingleObject': ([H, D], D),
            'GetExitCodeProcess': ([H, C.POINTER(D)], W.BOOL),
            'TerminateJobObject': ([H, W.UINT], W.BOOL),
            'TerminateProcess': ([H, W.UINT], W.BOOL),
            'CloseHandle': ([H], W.BOOL),
            'OpenProcess': ([D, W.BOOL, D], H),
            'GetProcessTimes': ([H, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], W.BOOL),
            'GetCurrentProcess': ([], H),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.k, name); fn.argtypes = args; fn.restype = result

    @staticmethod
    def _check(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def create_job(self):
        job = self._check(self.k.CreateJobObjectW(None, None))
        try:
            self._check(self.k.SetHandleInformation(job, 1, 0))
            flags = W.DWORD()
            self._check(self.k.GetHandleInformation(job, C.byref(flags)))
            if flags.value & 1:
                raise OSError('JOB_HANDLE_INHERITABLE')
            limits = self.EXT_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
            self._check(self.k.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
            observed = self.EXT_LIMIT()
            self._check(self.k.QueryInformationJobObject(job, 9, C.byref(observed), C.sizeof(observed), None))
            if observed.BasicLimitInformation.LimitFlags != 0x2000:
                raise OSError('JOB_LIMITS_NOT_CONFIRMED')
            return job
        except BaseException as error:
            cleanup_errors = []
            try:
                self.close_handle(job)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            # Job setup precedes CreateProcessW. Preserve the setup failure
            # even when closing the newly allocated Job also fails.
            raise NativeCreationError(error, None, cleanup_errors=cleanup_errors,
                job=job if cleanup_errors else None,
                unclosed_handles=(job,) if cleanup_errors else (),
                cleanup_resources=NativeCleanupResources(self, handles=(job,))
                    if cleanup_errors else None) from None

    def create_suspended(self, argv, cwd, env, job):
        import msvcrt
        handles = set()
        attrs = None
        initialized = False
        created = None
        primary_error = None
        cleanup_errors = []
        unclosed_handles = []
        unclosed_fds = []
        pending_attributes = None
        try:
            security = self.SA(C.sizeof(self.SA), None, True)
            child_in, parent_in = W.HANDLE(), W.HANDLE()
            parent_out, child_out = W.HANDLE(), W.HANDLE()
            self._check(self.k.CreatePipe(C.byref(child_in), C.byref(parent_in), C.byref(security), 0))
            handles.update((child_in.value, parent_in.value))
            self._check(self.k.CreatePipe(C.byref(parent_out), C.byref(child_out), C.byref(security), 0))
            handles.update((parent_out.value, child_out.value))
            self._check(self.k.SetHandleInformation(parent_in, 1, 0))
            self._check(self.k.SetHandleInformation(parent_out, 1, 0))
            null = self.k.CreateFileW('NUL', 0x40000000, 3, C.byref(security), 3, 0x80, None)
            if null in (None, C.c_void_p(-1).value):
                raise C.WinError(C.get_last_error())
            handles.add(null)
            size = C.c_size_t()
            self.k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            if not size.value:
                raise C.WinError(C.get_last_error())
            attrs = C.create_string_buffer(size.value)
            self._check(self.k.InitializeProcThreadAttributeList(attrs, 2, 0, C.byref(size)))
            initialized = True
            jobs = (W.HANDLE * 1)(job)
            inherited = (W.HANDLE * 3)(child_in.value, child_out.value, null)
            # The values and attribute buffer remain alive until Delete... below.
            self._check(self.k.UpdateProcThreadAttribute(attrs, 0, 0x0002000D, jobs, C.sizeof(jobs), None, None))
            self._check(self.k.UpdateProcThreadAttribute(attrs, 0, 0x00020002, inherited, C.sizeof(inherited), None, None))
            startup = self.SIEX()
            startup.StartupInfo.cb = C.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = child_in
            startup.StartupInfo.hStdOutput = child_out
            startup.StartupInfo.hStdError = null
            startup.lpAttributeList = C.cast(attrs, C.c_void_p)
            info = self.PI()
            command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = None
            if env is not None:
                if any(not isinstance(k, str) or not isinstance(v, str) or not k or '\0' in k + v or '=' in k for k, v in env.items()):
                    raise ValueError('INVALID_ENVIRONMENT')
                environment = C.create_unicode_buffer('\0'.join(k + '=' + env[k] for k in sorted(env, key=str.casefold)) + '\0\0')
            self._check(self.k.CreateProcessW(argv[0], command, None, None, True,
                0x4 | 0x80000 | 0x400 | 0x8000000, environment, cwd, C.byref(startup), C.byref(info)))
            created = CreatedProcess(info.dwProcessId, info.hProcess, info.hThread)
            # Only parent ends become Python-owned raw streams. Child ends close
            # here, so EOF cannot be held open by the supervisor itself.
            fd = msvcrt.open_osfhandle(parent_in.value, os.O_WRONLY | os.O_BINARY)
            handles.remove(parent_in.value)
            try:
                created.stdin = os.fdopen(fd, 'wb', buffering=0)
            except BaseException:
                try:
                    os.close(fd)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    unclosed_fds.append(fd)
                raise
            fd = msvcrt.open_osfhandle(parent_out.value, os.O_RDONLY | os.O_BINARY)
            handles.remove(parent_out.value)
            try:
                created.stdout = os.fdopen(fd, 'rb', buffering=0)
            except BaseException:
                try:
                    os.close(fd)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    unclosed_fds.append(fd)
                raise
        except BaseException as error:
            primary_error = error
        finally:
            if initialized:
                try:
                    self.k.DeleteProcThreadAttributeList(attrs)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    pending_attributes = attrs
            # Attempt every cleanup independently. In particular, no finally
            # exception may hide a successful CreateProcessW or its handles.
            for handle in sorted(handles):
                try:
                    self.close_handle(handle)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    unclosed_handles.append(handle)
        if primary_error is not None or cleanup_errors:
            if primary_error is None:
                primary_error = cleanup_errors.pop(0)
            resources = NativeCleanupResources(self, handles=unclosed_handles, fds=unclosed_fds,
                                               attributes=pending_attributes)
            if created is not None:
                created.cleanup_resources = resources
            raise NativeCreationError(primary_error, created,
                cleanup_errors=cleanup_errors, job=job,
                unclosed_handles=unclosed_handles,
                cleanup_resources=resources) from None
        return created

    def is_in_job(self, process, job):
        answer = W.BOOL()
        self._check(self.k.IsProcessInJob(process, job, C.byref(answer)))
        return bool(answer.value)

    def resume(self, thread):
        value = self.k.ResumeThread(thread)
        if value != 1:
            raise OSError('PRIMARY_THREAD_NOT_EXACTLY_SUSPENDED')

    def poll_process(self, process):
        state = self.k.WaitForSingleObject(process, 0)
        if state == 258:
            return None
        if state != 0:
            raise C.WinError(C.get_last_error())
        code = W.DWORD()
        self._check(self.k.GetExitCodeProcess(process, C.byref(code)))
        return int(code.value)

    def active_processes(self, job):
        value = self.ACCOUNTING()
        self._check(self.k.QueryInformationJobObject(job, 1, C.byref(value), C.sizeof(value), None))
        return int(value.ActiveProcesses)

    def terminate_job(self, job):
        self._check(self.k.TerminateJobObject(job, 0xC000013A))

    def terminate_process(self, process):
        self._check(self.k.TerminateProcess(process, 0xC000013A))

    def close_handle(self, handle):
        if handle:
            self._check(self.k.CloseHandle(handle))

    def open_process(self, pid):
        return self._check(self.k.OpenProcess(0x00100000 | 0x1000, False, pid))

    def creation_identity(self, handle):
        creation, exit_, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
        self._check(self.k.GetProcessTimes(handle, C.byref(creation), C.byref(exit_), C.byref(kernel), C.byref(user)))
        return 'win32-filetime:' + str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)

    def current_process_in_job(self):
        return self.is_in_job(self.k.GetCurrentProcess(), None)
