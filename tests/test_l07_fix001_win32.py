"""F003 closure controls through actual ctypes adapter, runner and restart loader.
Synthetic Win32 calls, real local pipes; no Windows process, browser or network.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import ctypes as C
import json
import os
import sys
import time
import types
from unittest.mock import patch
import pytest
from cdpd_chatgpt_profile import owned_process as owned
from cdpd_chatgpt_profile._l07_win32 import NativeCreationError, Win32API
from cdpd_chatgpt_profile.spool import canonical_json
from cdpd_chatgpt_profile.watchdog_runner import WatchdogWorkerRunner
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_capture_worker import Permit, request_data
from test_l07_supervisor_boundaries import supervisor_fixture, claim_fixture


class Function:
    def __init__(self, function): self.function = function
    def __call__(self, *args): return self.function(*args)


class Kernel:
    """Exact-call fault budgets make cleanup ownership and retries observable."""
    def __init__(self, failures=None, *, lifetime='exited'):
        self.failures, self.lifetime = dict(failures or {}), lifetime
        self.calls, self.fds, self.child_handles = [], set(), set()
        self.pipe_count = self.create_count = self.error = 0
        names = ('CreateJobObjectW SetInformationJobObject QueryInformationJobObject '
            'SetHandleInformation GetHandleInformation CreatePipe CreateFileW '
            'InitializeProcThreadAttributeList UpdateProcThreadAttribute '
            'DeleteProcThreadAttributeList CreateProcessW IsProcessInJob ResumeThread '
            'WaitForSingleObject GetExitCodeProcess TerminateJobObject TerminateProcess '
            'CloseHandle OpenProcess GetProcessTimes GetCurrentProcess').split()
        for name in names: setattr(self, name, Function(lambda *args: 1))
        self.CreateJobObjectW = Function(lambda *args: 9001)
        self.GetHandleInformation = Function(lambda h, v: (setattr(v._obj, 'value', 0), 1)[1])
        self.SetInformationJobObject = Function(self.setup_job)
        self.QueryInformationJobObject = Function(self.query)
        self.CreatePipe, self.CreateFileW = Function(self.pipe), Function(self.null)
        self.InitializeProcThreadAttributeList = Function(
            lambda value, count, flags, size: (setattr(size._obj, 'value', 128), int(value is not None))[1])
        self.UpdateProcThreadAttribute, self.DeleteProcThreadAttributeList = Function(self.update), Function(self.delete)
        self.CreateProcessW, self.CloseHandle = Function(self.create), Function(self.close)
        self.IsProcessInJob = Function(lambda p, j, v: (setattr(v._obj, 'value', 1), 1)[1])
        self.ResumeThread = Function(lambda h: (self.calls.append(['resume', h]), 1)[1])
        self.WaitForSingleObject = Function(self.wait)
        self.GetExitCodeProcess = Function(lambda h, v: (setattr(v._obj, 'value', 0), 1)[1])
        self.TerminateJobObject = Function(self.terminate)

    def inject(self, name):
        budget = self.failures.get(name, 0)
        if budget == 0: return False
        if budget > 0: self.failures[name] -= 1
        self.calls.append(['injected_failure', name])
        return True

    def setup_job(self, *args):
        if self.inject('job_setup'):
            self.error = 13; return 0
        return 1

    def update(self, *args):
        if self.inject('attribute_update'):
            self.error = 14; return 0
        return 1

    def query(self, handle, kind, value, size, result):
        self.calls.append(['query', handle, kind])
        if kind == 9: value._obj.BasicLimitInformation.LimitFlags = 0x2000
        elif self.lifetime == 'query_failure':
            self.error = 50; return 0
        else: value._obj.ActiveProcesses = 0 if self.lifetime == 'exited' else 1
        return 1

    def pipe(self, read, write, security, size):
        a, b = os.pipe(); self.fds.update((a, b))
        read._obj.value, write._obj.value = a, b
        self.child_handles.add(a if self.pipe_count == 0 else b)
        self.pipe_count += 1; return 1

    def null(self, *args):
        handle = os.open(os.devnull, os.O_WRONLY)
        self.fds.add(handle); self.child_handles.add(handle); return handle

    def delete(self, attributes):
        self.calls.append(['delete_attributes'])
        if self.inject('attribute_delete'): raise OSError(7, 'SYNTHETIC_PRIVATE_CLEANUP_TEXT')

    def create(self, executable, command, security, thread_security, inherit,
               flags, environment, cwd, startup, info):
        if self.inject('create_process'):
            self.error = 87; return 0
        self.create_count += 1; self.calls.append(['create_process', 999])
        info._obj.hProcess, info._obj.hThread, info._obj.dwProcessId = 9002, 9003, 999
        return 1

    def convert(self, handle, flags):
        if self.inject('pipe_conversion'): raise OSError(12, 'SYNTHETIC_PRIVATE_PRIMARY_TEXT')
        self.fds.discard(handle); return handle

    def wait(self, handle, timeout):
        self.calls.append(['wait', handle])
        return 0 if self.lifetime in {'exited', 'query_failure', 'descendant_alive'} else 258

    def terminate(self, handle, exit_code):
        self.calls.append(['terminate_job', handle])
        if self.lifetime == 'terminate_failure':
            self.error = 5; return 0
        if self.lifetime == 'terminates': self.lifetime = 'exited'
        return 1

    def close(self, handle):
        handle = getattr(handle, 'value', handle); self.calls.append(['close', handle])
        name = {9001: 'job_close', 9002: 'process_close', 9003: 'thread_close'}.get(handle)
        if name is None and handle in self.child_handles: name = 'child_close'
        if name and self.inject(name):
            self.error = {'job_close': 11, 'process_close': 15, 'thread_close': 16}.get(name, 6); return 0
        if handle in self.fds: self.fds.remove(handle); os.close(handle)
        return 1

    def cleanup(self):
        for descriptor in self.fds: os.close(descriptor)
        self.fds.clear()


@contextmanager
def native(kernel):
    api = Win32API(kernel=kernel)
    with ExitStack() as stack:
        stack.enter_context(patch.object(owned, '_windows', return_value=True))
        stack.enter_context(patch.object(owned, 'Win32API', return_value=api))
        stack.enter_context(patch.dict(sys.modules, {'msvcrt': types.SimpleNamespace(open_osfhandle=kernel.convert)}))
        stack.enter_context(patch.object(os, 'O_BINARY', 0, create=True))
        stack.enter_context(patch.object(C, 'get_last_error', side_effect=lambda: kernel.error, create=True))
        stack.enter_context(patch.object(C, 'WinError', side_effect=lambda number: OSError(number, 'SYNTHETIC_PRIVATE_NATIVE_TEXT'), create=True))
        try: yield api
        finally: kernel.cleanup()


def spawn():
    return owned.OwnedWorker.spawn([sys.executable, '-B', '-c', 'pass'], deadline_monotonic=time.monotonic() + 5)


def record(request, expected, actual):
    request.node._l07_evidence = {'expected': expected, 'actual': actual,
        'ac': ['AC03'], 'wd': ['WD-03', 'WD-05'], 'finding': 'L07-F-003',
        'scope': 'SYNTHETIC_WIN32_API_REAL_LOCAL_PIPES_NO_NATIVE_WINDOWS_PROCESS'}


@pytest.mark.parametrize('fault,number', [('child_close', 6), ('attribute_delete', 7), ('pipe_conversion', 12)])
def test_native_post_create_failure_retains_process_and_remaining_cleanup(fault, number, request):
    kernel = Kernel({fault: 1})
    with native(kernel) as api:
        job = api.create_job()
        with pytest.raises(NativeCreationError) as caught:
            api.create_suspended([sys.executable], None, None, job)
        error = caught.value
        actual = {'process_created': error.created is not None, 'pid': error.created.pid,
                  'process': error.created.process, 'thread': error.created.thread, 'job': error.job,
                  'primary_errno': error.primary_error.errno,
                  'resume_count': sum(call[0] == 'resume' for call in kernel.calls)}
        expected = {'process_created': True, 'pid': 999, 'process': 9002,
                    'thread': 9003, 'job': 9001, 'primary_errno': number, 'resume_count': 0}
        record(request, expected, actual); assert actual == expected
        assert error.cleanup_resources is error.created.cleanup_resources
        error.cleanup_resources.close()
        for stream in (error.created.stdin, error.created.stdout):
            if stream is not None: stream.close()


@pytest.mark.parametrize('fault,number', [('create_process', 87), ('attribute_update', 14)])
def test_native_pre_create_failure_is_not_a_created_worker(fault, number, request):
    kernel = Kernel({fault: 1})
    with native(kernel) as api:
        with pytest.raises(NativeCreationError) as caught:
            api.create_suspended([sys.executable], None, None, api.create_job())
        actual = {'process_created': caught.value.created is not None, 'actual_create_count': kernel.create_count,
                  'primary_errno': caught.value.primary_error.errno,
                  'cleanup_confirmed': caught.value.cleanup_resources.snapshot()['confirmed']}
        expected = {'process_created': False, 'actual_create_count': 0, 'primary_errno': number, 'cleanup_confirmed': True}
        record(request, expected, actual); assert actual == expected


def test_primary_conversion_failure_survives_multiple_secondary_cleanup_faults(request):
    kernel = Kernel({'pipe_conversion': 1, 'attribute_delete': 1, 'child_close': -1})
    with native(kernel) as api:
        with pytest.raises(NativeCreationError) as caught:
            api.create_suspended([sys.executable], None, None, api.create_job())
        error = caught.value
        actual = {'primary_errno': error.primary_error.errno,
                  'secondary_errnos': [value.errno for value in error.cleanup_errors], 'created_pid': error.created.pid,
                  'all_child_close_attempted': all(['close', value] in kernel.calls for value in kernel.child_handles),
                  'held_failed_child_handles': sorted(error.cleanup_resources.handles)}
        expected = {'primary_errno': 12, 'secondary_errnos': [7, 6, 6, 6], 'created_pid': 999,
                    'all_child_close_attempted': True, 'held_failed_child_handles': sorted(kernel.child_handles)}
        record(request, expected, actual); assert actual == expected


@pytest.mark.parametrize('fault,number', [('child_close', 6), ('attribute_delete', 7), ('pipe_conversion', 12)])
def test_owned_failure_confirms_exit_only_through_saved_process_and_job(fault, number, request):
    kernel = Kernel({fault: 1})
    with native(kernel):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        actual = {'created': error.process_created, 'pid': error.pid, 'termination_confirmed': error.termination_confirmed,
                  'active_processes': error.termination_result.active_processes, 'primary_errno': error.native_error,
                  'resource_cleanup_confirmed': error.resource_cleanup['confirmed'],
                  'saved_process_queried': ['wait', 9002] in kernel.calls,
                  'saved_job_queried': ['query', 9001, 1] in kernel.calls,
                  'resumed': any(call[0] == 'resume' for call in kernel.calls)}
        expected = {'created': True, 'pid': 999, 'termination_confirmed': True, 'active_processes': 0,
                    'primary_errno': number, 'resource_cleanup_confirmed': True, 'saved_process_queried': True,
                    'saved_job_queried': True, 'resumed': False}
        record(request, expected, actual); assert actual == expected
        assert error.process_reference == {'pid': 999, 'process': 9002, 'thread': 9003, 'job': 9001}


@pytest.mark.parametrize('fault,number', [('job_close', 11), ('process_close', 15), ('thread_close', 16)])
def test_post_failure_handle_close_fault_preserves_primary_and_confirmed_lifetime(fault, number, request):
    kernel = Kernel({'pipe_conversion': 1, fault: -1})
    with native(kernel):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        actual = {'primary_errno': error.native_error, 'secondary_errnos': [value.errno for value in error.cleanup_errors],
                  'process_created': error.process_created, 'pid': error.pid, 'termination_confirmed': error.termination_confirmed,
                  'resource_cleanup': error.resource_cleanup, 'owner_retained': error.cleanup_owner is not None}
        expected = {'primary_errno': 12, 'secondary_errnos': [number], 'process_created': True, 'pid': 999,
                    'termination_confirmed': True, 'resource_cleanup': {'confirmed': False, 'pending_handle_count': 1,
                        'pending_attribute_list_count': 0, 'pending_stream_count': 0}, 'owner_retained': True}
        record(request, expected, actual); assert actual == expected


def test_cleanup_exception_does_not_overwrite_creation_identity_or_primary_error(request):
    kernel = Kernel({'pipe_conversion': 1, 'job_close': -1})
    with native(kernel), patch.object(owned.OwnedWorker, 'terminate_and_confirm',
            side_effect=OSError(20, 'SYNTHETIC_PRIVATE_TERMINATION_TEXT')):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        actual = {'process_created': error.process_created, 'pid': error.pid, 'primary_errno': error.native_error,
                  'secondary_errnos': [value.errno for value in error.cleanup_errors],
                  'termination_confirmed': error.termination_confirmed,
                  'termination_reason': error.termination_result.reason,
                  'cleanup_owner_retained': error.cleanup_owner is not None}
        expected = {'process_created': True, 'pid': 999, 'primary_errno': 12, 'secondary_errnos': [20, 11],
                    'termination_confirmed': False, 'termination_reason': 'WORKER_TERMINATION_UNCONFIRMED',
                    'cleanup_owner_retained': True}
        record(request, expected, actual); assert actual == expected


def test_job_setup_and_cleanup_failures_preserve_precreation_fact(request):
    kernel = Kernel({'job_setup': 1, 'job_close': -1})
    with native(kernel):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        actual = {'process_created': error.process_created, 'pid': error.pid, 'actual_create_count': kernel.create_count,
                  'termination_confirmed': error.termination_confirmed, 'primary_errno': error.native_error,
                  'secondary_errnos': [value.errno for value in error.cleanup_errors],
                  'resource_cleanup_confirmed': error.resource_cleanup['confirmed'],
                  'pending_handles': error.resource_cleanup['pending_handle_count']}
        expected = {'process_created': False, 'pid': None, 'actual_create_count': 0, 'termination_confirmed': True,
                    'primary_errno': 13, 'secondary_errnos': [11, 11], 'resource_cleanup_confirmed': False, 'pending_handles': 1}
        record(request, expected, actual); assert actual == expected


def restart_from_metadata(tmp_path, metadata):
    supervisor = supervisor_fixture(tmp_path)
    directory, outcome = claim_fixture(supervisor)
    outcome['process_metadata'] = deepcopy(metadata)
    outcome['worker_blocked'] = metadata['new_worker_prohibited']
    (directory / 'OUTCOME.json').write_bytes(canonical_json(outcome))
    assert not (supervisor.root / 'WORKER_BLOCKED.json').exists()
    supervisor._load_records()
    return supervisor


@pytest.mark.parametrize('lifetime', ['running', 'query_failure', 'terminate_failure', 'descendant_alive'])
def test_unknown_lifetime_preserves_spawn_count_and_blocks_next_worker_and_restart(lifetime, tmp_path, request):
    kernel = Kernel({'child_close': 1}, lifetime=lifetime)
    runner = WatchdogWorkerRunner(poll_interval=.01, cleanup_timeout=.1)
    with native(kernel):
        result = runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
        first = deepcopy(runner.last_process_metadata)
        second = runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
        restarted = restart_from_metadata(tmp_path, first)
        actual = {'actual_create_count': kernel.create_count, 'spawn_count': first['spawn_count'],
                  'spawn_count_known': first['spawn_count_known'], 'pid': first['pid'],
                  'termination': first['termination'], 'confirmed': first['termination_facts']['confirmed'],
                  'lifetime_latch': runner.termination_unconfirmed, 'second_attempt_started': second['pid'] != 0,
                  'restart_blocked': restarted.worker_blocked, 'replay_queued': bool(restarted._pending),
                  'structure_present': result['structure'] is not None}
        expected = {'actual_create_count': 1, 'spawn_count': 1, 'spawn_count_known': True, 'pid': 999,
                    'termination': 'UNCONFIRMED', 'confirmed': False, 'lifetime_latch': True,
                    'second_attempt_started': False, 'restart_blocked': True, 'replay_queued': False, 'structure_present': False}
        record(request, expected, {'summary': actual, 'first_process_metadata': first, 'calls': kernel.calls})
        assert actual == expected
        assert first['termination_facts']['reason'] == 'WORKER_TERMINATION_UNCONFIRMED'
        assert first['spawn_error']['primary']['native_error'] == 6
        assert 'SYNTHETIC_PRIVATE' not in json.dumps(first)
        assert not any(call[0] == 'resume' for call in kernel.calls)


def test_saved_handle_confirmed_cleanup_allows_a_separate_later_attempt(request):
    kernels = []
    def factory(*args, **kwargs):
        kernel = Kernel({'child_close': 1}); kernels.append(kernel)
        with native(kernel): return owned.OwnedWorker.spawn(*args, **kwargs)
    runner = WatchdogWorkerRunner(process_factory=factory, poll_interval=.01, cleanup_timeout=.1)
    metadata = []
    for _ in range(2):
        runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
        metadata.append(deepcopy(runner.last_process_metadata))
    actual = {'attempt_count': len(kernels), 'actual_created_count': sum(kernel.create_count for kernel in kernels),
              'reported_spawn_counts': [v['spawn_count'] for v in metadata], 'reported_pids': [v['pid'] for v in metadata],
              'termination_confirmed': [v['termination_facts']['confirmed'] for v in metadata],
              'resource_cleanup_confirmed': [v['resource_cleanup']['confirmed'] for v in metadata],
              'new_worker_prohibited': [v['new_worker_prohibited'] for v in metadata]}
    expected = {'attempt_count': 2, 'actual_created_count': 2, 'reported_spawn_counts': [1, 1],
                'reported_pids': [999, 999], 'termination_confirmed': [True, True],
                'resource_cleanup_confirmed': [True, True], 'new_worker_prohibited': [False, False]}
    record(request, expected, {'summary': actual, 'metadata': metadata}); assert actual == expected


@pytest.mark.parametrize('fault,expected_handles,expected_attributes', [
    ('child_close', 3, 0), ('job_close', 1, 0), ('attribute_delete', 0, 1)])
def test_remaining_resources_block_without_falsifying_confirmed_process_exit(
        fault, expected_handles, expected_attributes, tmp_path, request):
    failures = {fault: -1}
    if fault == 'job_close': failures['pipe_conversion'] = 1
    kernel = Kernel(failures)
    runner = WatchdogWorkerRunner(poll_interval=.01, cleanup_timeout=.1)
    with native(kernel):
        runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
        first = deepcopy(runner.last_process_metadata)
        runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
        restarted = restart_from_metadata(tmp_path, first)
        actual = {'actual_create_count': kernel.create_count, 'termination_confirmed': first['termination_facts']['confirmed'],
                  'termination_unconfirmed': first['termination_unconfirmed'],
                  'resource_cleanup_unconfirmed': first['resource_cleanup_unconfirmed'],
                  'resource_cleanup': first['resource_cleanup'], 'new_worker_prohibited': first['new_worker_prohibited'],
                  'failure_reason': first['failure_reason'], 'retained_owner': runner._retained_cleanup_owner is not None,
                  'restart_blocked': restarted.worker_blocked}
        expected = {'actual_create_count': 1, 'termination_confirmed': True, 'termination_unconfirmed': False,
                    'resource_cleanup_unconfirmed': True,
                    'resource_cleanup': {'confirmed': False, 'pending_handle_count': expected_handles,
                        'pending_attribute_list_count': expected_attributes, 'pending_stream_count': 0},
                    'new_worker_prohibited': True, 'failure_reason': 'WORKER_RESOURCE_CLEANUP_UNCONFIRMED',
                    'retained_owner': True, 'restart_blocked': True}
        record(request, expected, {'summary': actual, 'first_process_metadata': first, 'calls': kernel.calls}); assert actual == expected


def test_runner_records_precreation_failure_with_zero_actual_spawns(request):
    kernel = Kernel({'create_process': 1}); runner = WatchdogWorkerRunner(poll_interval=.01, cleanup_timeout=.1)
    with native(kernel):
        runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
        metadata = runner.last_process_metadata
        actual = {'actual_create_count': kernel.create_count, 'spawn_count': metadata['spawn_count'], 'pid': metadata['pid'],
                  'termination_reason': metadata['termination_facts']['reason'],
                  'termination_confirmed': metadata['termination_facts']['confirmed'],
                  'new_worker_prohibited': metadata['new_worker_prohibited'],
                  'primary_errno': metadata['spawn_error']['primary']['native_error']}
        expected = {'actual_create_count': 0, 'spawn_count': 0, 'pid': None, 'termination_reason': 'NOT_CREATED',
                    'termination_confirmed': True, 'new_worker_prohibited': False, 'primary_errno': 87}
        record(request, expected, actual); assert actual == expected


def test_second_pipe_conversion_failure_closes_first_owned_stream(request):
    kernel = Kernel()
    converted = []
    original = kernel.convert
    def conversion(handle, flags):
        if converted: raise OSError(12, 'SYNTHETIC_PRIVATE_SECOND_PIPE')
        converted.append(handle); return original(handle, flags)
    kernel.convert = conversion
    with native(kernel):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        with pytest.raises(OSError): os.fstat(converted[0])
        actual = {'created': error.process_created, 'pid': error.pid,
                  'primary_errno': error.native_error, 'termination_confirmed': error.termination_confirmed,
                  'resource_cleanup_confirmed': error.resource_cleanup['confirmed'], 'first_stream_closed': True}
        expected = {'created': True, 'pid': 999, 'primary_errno': 12, 'termination_confirmed': True,
                    'resource_cleanup_confirmed': True, 'first_stream_closed': True}
        record(request, expected, actual); assert actual == expected


def test_fdopen_and_descriptor_close_faults_preserve_primary_and_crt_ownership(request):
    kernel = Kernel(); original_close = os.close; failures = []
    def close(descriptor):
        if not failures:
            failures.append(descriptor); raise OSError(10, 'SYNTHETIC_PRIVATE_CRT_CLOSE')
        return original_close(descriptor)
    with native(kernel), patch.object(os, 'fdopen', side_effect=OSError(9, 'SYNTHETIC_PRIVATE_FDOPEN')), \
            patch.object(os, 'close', side_effect=close):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        with pytest.raises(OSError): os.fstat(failures[0])
        actual = {'created': error.process_created, 'pid': error.pid, 'primary_errno': error.native_error,
                  'secondary_errnos': [value.errno for value in error.cleanup_errors],
                  'termination_confirmed': error.termination_confirmed,
                  'resource_cleanup_confirmed': error.resource_cleanup['confirmed'], 'crt_descriptor_closed': True}
        expected = {'created': True, 'pid': 999, 'primary_errno': 9, 'secondary_errnos': [10],
                    'termination_confirmed': True, 'resource_cleanup_confirmed': True, 'crt_descriptor_closed': True}
        record(request, expected, actual); assert actual == expected


def test_python_owner_construction_failure_keeps_native_creation_and_original_error(request):
    kernel = Kernel({'pipe_conversion': 1})
    with native(kernel), patch.object(owned.OwnedWorker, '__init__',
            side_effect=RuntimeError('SYNTHETIC_PRIVATE_OWNER_CONSTRUCTION')):
        with pytest.raises(owned.OwnedProcessError) as caught: spawn()
        error = caught.value
        actual = {'created': error.process_created, 'pid': error.pid,
                  'primary_errno': error.native_error,
                  'secondary_types': [type(value).__name__ for value in error.cleanup_errors],
                  'termination_confirmed': error.termination_confirmed,
                  'resource_cleanup_confirmed': error.resource_cleanup['confirmed'],
                  'resumed': any(call[0] == 'resume' for call in kernel.calls)}
        expected = {'created': True, 'pid': 999, 'primary_errno': 12, 'secondary_types': ['RuntimeError'],
                    'termination_confirmed': False, 'resource_cleanup_confirmed': True, 'resumed': False}
        record(request, expected, actual); assert actual == expected


@pytest.mark.parametrize('created', [True, False])
def test_contradictory_spawn_confirmation_never_grants_another_attempt(created, request):
    calls = []
    def factory(*args, **kwargs):
        calls.append('attempt')
        raise owned.OwnedProcessError('OWNED_SPAWN_FAILED', process_created=created,
            pid=999 if created else None, termination_confirmed=False,
            termination_result=owned.TerminationResult(True, True, 'OWNED_TREE_TERMINATED', 0, 0))
    runner = WatchdogWorkerRunner(process_factory=factory, poll_interval=.01, cleanup_timeout=.1)
    runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
    first = deepcopy(runner.last_process_metadata)
    runner(WorkerRequest.from_dict(request_data(timeout=5.0)), permit=Permit())
    actual = {'factory_calls': len(calls), 'used_confirmation': first['termination_facts']['confirmed'],
              'reported_confirmation_preserved': first['reported_spawn_failure_facts']['confirmed'],
              'facts_consistent': first['spawn_failure_facts_consistent'],
              'lifetime_blocked': runner.termination_unconfirmed, 'new_worker_prohibited': first['new_worker_prohibited']}
    expected = {'factory_calls': 1, 'used_confirmation': False, 'reported_confirmation_preserved': True,
                'facts_consistent': False, 'lifetime_blocked': True, 'new_worker_prohibited': True}
    record(request, expected, {'summary': actual, 'metadata': first}); assert actual == expected
