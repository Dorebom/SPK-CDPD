"""Execution controls only. Windows API injections do not establish native acceptance."""
from dataclasses import asdict
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
from cdpd_chatgpt_profile import owned_process as m
from cdpd_chatgpt_profile._l07_win32 import CreatedProcess, NativeCreationError, Win32API


class FakeNative:
    def __init__(self):
        self.calls = []
        self.rc = None
        self.active = 1
        self.membership = True
        self.create_delay = 0
        self.resume_error = False
        self.kill_error = False
        self.confirm_kill = True
        self.created = CreatedProcess(4321, 11, 12, io.BytesIO(), io.BytesIO())
    def create_job(self):
        self.calls.append(('create_job_noninheritable_kill_on_close',))
        return 7
    def create_suspended(self, argv, cwd, env, job):
        self.calls.append(('create_suspended_with_job_list', job))
        if self.create_delay: time.sleep(self.create_delay)
        return self.created
    def is_in_job(self, process, job):
        self.calls.append(('membership', process, job))
        return self.membership
    def resume(self, thread):
        self.calls.append(('resume', thread))
        if self.resume_error: raise OSError(5, 'fixture')
    def close_handle(self, handle): self.calls.append(('close', handle))
    def poll_process(self, process): return self.rc
    def active_processes(self, job): return self.active
    def terminate_job(self, job):
        self.calls.append(('kill_job', job))
        if self.kill_error: raise OSError(5, 'fixture')
        if self.confirm_kill: self.active, self.rc = 0, 0xC000013A
    def terminate_process(self, process):
        self.calls.append(('kill_owned_handle', process))
        if self.confirm_kill: self.rc = 0xC000013A
    def open_process(self, pid): return 51
    def creation_identity(self, handle): return 'win32-filetime:1234'


class WindowsInjectedControls(unittest.TestCase):
    def spawn(self, api, *, budget=5, cancellation=None):
        with patch.object(m, '_windows', return_value=True), patch.object(m, 'Win32API', return_value=api):
            return m.OwnedWorker.spawn([sys.executable, '-B', '-c', 'pass'],
                deadline_monotonic=time.monotonic()+budget, cancellation=cancellation)

    def test_suspended_job_membership_then_resume_order(self):
        api = FakeNative(); p = self.spawn(api)
        try:
            self.assertEqual(api.calls[:5], [('create_job_noninheritable_kill_on_close',),
                ('create_suspended_with_job_list', 7), ('membership', 11, 7), ('resume', 12), ('close', 12)])
            self.assertEqual(p.pid, 4321)
            self.assertEqual(p.supervision_scope, 'WINDOWS_PRIVATE_JOB_OBJECT')
            self.assertIsNone(p.poll())
            result = p.terminate_and_confirm(.1)
            self.assertTrue(result.confirmed)
            self.assertEqual(asdict(result)['active_processes'], 0)
        finally: p.close()

    def test_failed_membership_never_resumes_and_kills_exact_handle(self):
        api = FakeNative(); api.membership = False; api.confirm_kill = False
        # Job query can be zero while suspended child is outside the job.
        api.active = 0
        api.terminate_process = lambda handle: (api.calls.append(('kill_owned_handle', handle)), setattr(api, 'rc', 91))
        with self.assertRaises(m.OwnedProcessError) as caught:
            self.spawn(api)
        self.assertEqual(caught.exception.code, 'JOB_ASSIGNMENT_NOT_CONFIRMED')
        self.assertTrue(caught.exception.process_created)
        self.assertTrue(caught.exception.termination_confirmed)
        self.assertFalse(any(c[0] == 'resume' for c in api.calls))
        self.assertIn(('kill_owned_handle', 11), api.calls)

    def test_late_create_is_never_resumed(self):
        api = FakeNative(); api.create_delay = .05
        with self.assertRaises(m.OwnedProcessError) as caught:
            self.spawn(api, budget=.02)
        self.assertEqual(caught.exception.code, 'DEADLINE_OR_CANCELLED_BEFORE_RESUME')
        self.assertTrue(caught.exception.termination_confirmed)
        self.assertFalse(any(c[0] == 'resume' for c in api.calls))

    def test_cancel_during_creation_is_never_resumed(self):
        api = FakeNative(); cancel = threading.Event()
        original = api.create_suspended
        def create(*args):
            result = original(*args); cancel.set(); return result
        api.create_suspended = create
        with self.assertRaises(m.OwnedProcessError) as caught:
            self.spawn(api, cancellation=cancel)
        self.assertEqual(caught.exception.code, 'DEADLINE_OR_CANCELLED_BEFORE_RESUME')
        self.assertFalse(any(c[0] == 'resume' for c in api.calls))

    def test_resume_failure_cleans_owned_job(self):
        api = FakeNative(); api.resume_error = True
        with self.assertRaises(m.OwnedProcessError) as caught: self.spawn(api)
        self.assertTrue(caught.exception.termination_confirmed)
        self.assertIn(('kill_job', 7), api.calls)

    def test_creation_failure_after_process_exists_preserves_cleanup_fact(self):
        api = FakeNative()
        def fail(*args): raise NativeCreationError(OSError(5, 'fixture'), api.created)
        api.create_suspended = fail
        with self.assertRaises(m.OwnedProcessError) as caught: self.spawn(api)
        self.assertTrue(caught.exception.process_created)
        self.assertEqual(caught.exception.pid, 4321)
        self.assertTrue(caught.exception.termination_confirmed)
        self.assertFalse(any(c[0] == 'resume' for c in api.calls))

    def test_termination_unconfirmed_is_sticky_even_after_late_exit(self):
        api = FakeNative(); api.confirm_kill = False
        p = self.spawn(api)
        try:
            first = p.terminate_and_confirm(.01)
            self.assertFalse(first.confirmed)
            self.assertEqual(first.reason, 'WORKER_TERMINATION_UNCONFIRMED')
            api.active, api.rc = 0, 0
            self.assertEqual(p.terminate_and_confirm(.1), first)
        finally: p.close()

    def test_terminate_api_failure_is_not_success(self):
        api = FakeNative(); api.kill_error = True
        p = self.spawn(api)
        try:
            result = p.terminate_and_confirm(.01)
            self.assertFalse(result.confirmed)
            self.assertEqual(result.native_error, 5)
        finally: p.close()

    def test_leader_exit_does_not_prove_descendant_exit(self):
        api = FakeNative(); api.rc = 0; api.active = 1; api.confirm_kill = False
        p = self.spawn(api)
        try:
            self.assertEqual(p.poll(), 0)
            self.assertFalse(p.terminate_and_confirm(.01).confirmed)
            self.assertIn(('kill_job', 7), api.calls)
        finally: p.close()

    def test_normal_empty_job_requires_no_kill(self):
        api = FakeNative(); api.rc = 0; api.active = 0
        p = self.spawn(api)
        try:
            result = p.terminate_and_confirm(.1)
            self.assertTrue(result.confirmed)
            self.assertFalse(result.requested)
            self.assertEqual(result.reason, 'ALREADY_EXITED')
        finally: p.close()
        self.assertFalse(any(c[0] == 'kill_job' for c in api.calls))

    def test_concurrent_kill_and_close_are_exact_handle_idempotent(self):
        api = FakeNative(); p = self.spawn(api)
        threads = [threading.Thread(target=p.terminate_and_confirm, kwargs={'timeout_s':.2}) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        p.close(); p.close()
        self.assertEqual(api.calls.count(('kill_job', 7)), 1)
        self.assertEqual(api.calls.count(('close', 11)), 1)
        self.assertEqual(api.calls.count(('close', 7)), 1)

    def test_independent_monitor_terminates_without_caller_poll(self):
        api = FakeNative(); p = self.spawn(api, budget=.08)
        try:
            time.sleep(.14)
            self.assertTrue(p.deadline_expired)
            self.assertTrue(p.termination_result.confirmed)
            self.assertIn(('kill_job', 7), api.calls)
        finally: p.close()

    def test_monitor_can_kill_while_resume_call_is_stalled(self):
        api = FakeNative(); release = threading.Event(); entered = threading.Event(); failures = []
        def resume(thread):
            api.calls.append(('resume', thread)); entered.set(); release.wait(1)
        api.resume = resume
        def launch():
            try: self.spawn(api, budget=.05)
            except m.OwnedProcessError as error: failures.append(error)
        launching = threading.Thread(target=launch)
        launching.start()
        try:
            self.assertTrue(entered.wait(.5))
            limit = time.monotonic()+.5
            while ('kill_job',7) not in api.calls and time.monotonic()<limit:
                time.sleep(.01)
            self.assertIn(('kill_job',7), api.calls)
        finally:
            release.set(); launching.join(2)
        self.assertEqual(len(failures),1)
        self.assertTrue(failures[0].termination_confirmed)

    def test_native_registered_identity_uses_same_open_handle(self):
        api = FakeNative()
        with patch.object(m, '_windows', return_value=True), patch.object(m, 'Win32API', return_value=api):
            p = m.RegisteredProcess.open(123, 'win32-filetime:1234')
            self.assertTrue(p.is_alive())
            api.rc = 0
            self.assertFalse(p.is_alive())
            p.close()
            self.assertIn(('close', 51), api.calls)
            with self.assertRaises(m.OwnedProcessError): m.RegisteredProcess.open(123, 'win32-filetime:9999')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux synthetic process controls only')
class RealPosixControls(unittest.TestCase):
    def spawn(self, script, budget=3):
        return m.OwnedWorker.spawn([sys.executable, '-B', '-u', '-c', script], deadline_monotonic=time.monotonic()+budget)

    def test_real_binary_pipes_and_normal_exit(self):
        p = self.spawn('import sys; sys.stdout.buffer.write(sys.stdin.buffer.readline()); sys.stdout.buffer.flush()')
        try:
            p.stdin.write(b'fixed-pipe-data\n')
            self.assertEqual(p.stdout.readline(), b'fixed-pipe-data\n')
            self.assertEqual(p.wait(timeout=1), 0)
            result = p.terminate_and_confirm(.2)
            self.assertTrue(result.confirmed)
            self.assertFalse(result.requested)
        finally: p.close()

    def test_exec_wrapper_preserves_pid_group_argv_environment_cwd_and_pipes(self):
        script = ('import json,os,sys; '
                  'print(json.dumps({"pid":os.getpid(),"group":os.getpgrp(),'
                  '"parent":os.getppid(),"args":sys.argv[1:],"cwd":os.getcwd(),'
                  '"environment":os.environ["CDPD_L07_EXEC_PROBE"]}),flush=True); '
                  'sys.stdout.buffer.write(sys.stdin.buffer.readline());sys.stdout.buffer.flush()')
        args = ['argument with spaces', 'literal"quote', 'Tokyo-\u6771\u4eac']
        command = [sys.executable, '-B', '-u', '-c', script, *args]
        env = dict(os.environ, CDPD_L07_EXEC_PROBE='fixed-value')
        with tempfile.TemporaryDirectory() as directory:
            p = m.OwnedWorker.spawn(command, cwd=directory, env=env,
                                   deadline_monotonic=time.monotonic()+3)
            try:
                self.assertTrue(select.select([p.stdout], [], [], 2)[0])
                value = json.loads(p.stdout.readline())
                self.assertEqual(value, {'pid':p.pid,'group':p.pid,'parent':os.getpid(),
                    'args':args,'cwd':directory,'environment':'fixed-value'})
                self.assertEqual(p.args, command)
                p.stdin.write(b'unchanged-binary-protocol\n')
                self.assertEqual(p.stdout.readline(), b'unchanged-binary-protocol\n')
                self.assertEqual(p.wait(timeout=1), 0)
                self.assertTrue(p.terminate_and_confirm(.2).confirmed)
            finally: p.close()

    def test_exec_wrapper_parent_mismatch_exits_without_running_target(self):
        wrapper = ROOT/'code/cdpd_chatgpt_profile/_l07_posix_exec.py'
        completed = subprocess.run([sys.executable, '-I', '-S', '-B', str(wrapper),
            str(os.getpid()+100000000), '--', sys.executable, '-B', '-c', 'print("must-not-execute")'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        self.assertEqual(completed.returncode,125)
        self.assertEqual(completed.stdout,b'')
        self.assertEqual(completed.stderr,b'')

    def test_exec_wrapper_prctl_failure_never_executes_target(self):
        from cdpd_chatgpt_profile import _l07_posix_exec as wrapper
        with patch.object(wrapper, '_arm_parent_death', side_effect=OSError(1,'fixture')), \
                patch.object(wrapper.os, 'execve') as execute:
            self.assertEqual(wrapper.main([str(os.getpid()), '--', sys.executable, '-c', 'pass']),125)
        execute.assert_not_called()

    def test_supervisor_death_stops_owned_leader_spawned_by_live_thread(self):
        # The only kill targets are this test's parent and its identified Worker.
        # Descendant tree cleanup is deliberately not asserted by this control.
        worker_script = 'import os,time;print(os.getpid(),flush=True);time.sleep(8)'
        parent_script = (
            'import json,sys,threading,time\n'
            f'sys.path.insert(0,{str(ROOT/"code")!r})\n'
            'from cdpd_chatgpt_profile.owned_process import OwnedWorker,process_creation_identity\n'
            'def own():\n'
            f' p=OwnedWorker.spawn([sys.executable,"-B","-u","-c",{worker_script!r}],deadline_monotonic=time.monotonic()+8)\n'
            ' reported=int(p.stdout.readline())\n'
            ' print(json.dumps({"pid":p.pid,"reported":reported,"identity":process_creation_identity(p.pid)}),flush=True)\n'
            ' threading.Event().wait(8)\n'
            'threading.Thread(target=own,daemon=True).start()\n'
            'threading.Event().wait(8)\n')
        parent = subprocess.Popen([sys.executable,'-B','-u','-c',parent_script],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            start_new_session=True)
        worker = None
        try:
            self.assertTrue(select.select([parent.stdout],[],[],3)[0])
            value = json.loads(parent.stdout.readline())
            self.assertEqual(value['pid'],value['reported'])
            worker = m.RegisteredProcess.open(value['pid'],value['identity'])
            self.assertTrue(worker.is_alive())
            parent.kill(); parent.wait(timeout=2)
            deadline = time.monotonic()+2
            while worker.is_alive() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertFalse(worker.is_alive())
        finally:
            if parent.poll() is None:
                parent.kill();parent.wait(timeout=2)
            if worker is not None:
                if worker.is_alive():
                    signal.pidfd_send_signal(worker._handle,signal.SIGKILL)
                worker.close()
            parent.stdout.close()

    def test_real_deadline_kills_only_owned_group_and_keeps_sentinel(self):
        sentinel = subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(20)'])
        p = self.spawn('import time; time.sleep(20)', budget=.15)
        started = time.monotonic()
        try:
            code = p.wait(timeout=2)
            self.assertLess(code, 0)
            self.assertLess(time.monotonic()-started, 1.5)
            self.assertIsNone(sentinel.poll())
            self.assertTrue(p.deadline_expired)
            self.assertTrue(p.terminate_and_confirm(.2).confirmed)
        finally:
            p.close(); sentinel.kill(); sentinel.wait(timeout=2)

    def test_real_descendant_is_killed_after_leader_normal_exit(self):
        script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(20)']); print(p.pid,flush=True)"
        p = self.spawn(script)
        try:
            child = int(p.stdout.readline())
            identity = m.process_creation_identity(child)
            registered = m.RegisteredProcess.open(child, identity)
            try:
                self.assertEqual(p.wait(timeout=1), 0)
                self.assertTrue(registered.is_alive())
                result = p.terminate_and_confirm(1)
                self.assertTrue(result.requested)
                self.assertTrue(result.confirmed)
                self.assertFalse(registered.is_alive())
            finally: registered.close()
        finally: p.close()

    def test_real_registered_process_needs_creation_identity_and_pidfd(self):
        p = subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(20)'])
        registered = None
        try:
            identity = m.process_creation_identity(p.pid)
            registered = m.RegisteredProcess.open(p.pid, identity)
            self.assertTrue(registered.is_alive())
            with self.assertRaises(m.OwnedProcessError): m.RegisteredProcess.open(p.pid, identity+'0')
            p.kill(); p.wait(timeout=2)
            self.assertFalse(registered.is_alive())
        finally:
            if registered: registered.close()
            if p.poll() is None: p.kill(); p.wait(timeout=2)

    def test_cancel_before_spawn_creates_no_process(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(m.OwnedProcessError) as caught:
            m.OwnedWorker.spawn([sys.executable, '-c', 'pass'], deadline_monotonic=time.monotonic()+1, cancellation=cancel)
        self.assertFalse(caught.exception.process_created)
        self.assertTrue(caught.exception.termination_confirmed)

    def test_proc_pid_namespace_mismatch_refuses_before_spawn(self):
        with patch.object(m.os, 'getpid', return_value=999999999), patch.object(m.subprocess, 'Popen') as start:
            with self.assertRaises(m.OwnedProcessError) as caught:
                self.spawn('pass')
        self.assertEqual(caught.exception.code,'PROC_PID_NAMESPACE_MISMATCH')
        self.assertFalse(caught.exception.process_created)
        start.assert_not_called()

    def test_wait_timeout_does_not_reset_absolute_deadline(self):
        p = self.spawn('import time; time.sleep(20)', budget=.2)
        try:
            with self.assertRaises(subprocess.TimeoutExpired): p.wait(timeout=.01)
            self.assertLess(p.wait(timeout=1), 0)
            self.assertTrue(p.deadline_expired)
        finally: p.close()


class FakeFunction:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


class CtypesAdapterConfiguration(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Injected handle mapping uses Linux fds')
    def test_actual_adapter_builds_noninherited_job_and_exact_handle_allowlist(self):
        calls = []; kernel = types.SimpleNamespace(); open_fds = set()
        def function(name, fn): setattr(kernel, name, FakeFunction(fn))
        for name in ('CreateJobObjectW','SetInformationJobObject','QueryInformationJobObject','SetHandleInformation',
            'GetHandleInformation','CreatePipe','CreateFileW','InitializeProcThreadAttributeList','UpdateProcThreadAttribute',
            'DeleteProcThreadAttributeList','CreateProcessW','IsProcessInJob','ResumeThread','WaitForSingleObject',
            'GetExitCodeProcess','TerminateJobObject','TerminateProcess','CloseHandle','OpenProcess','GetProcessTimes','GetCurrentProcess'):
            function(name, lambda *args: 1)
        function('CreateJobObjectW', lambda security,name: (calls.append(('job',security,name)),9001)[1])
        function('SetHandleInformation', lambda h,mask,flags: (calls.append(('inherit',getattr(h,'value',h),mask,flags)),1)[1])
        function('GetHandleInformation', lambda h,p: (setattr(p._obj,'value',0),1)[1])
        function('SetInformationJobObject', lambda h,c,p,n: (calls.append(('limits',h,c,p._obj.BasicLimitInformation.LimitFlags)),1)[1])
        function('QueryInformationJobObject', lambda h,c,p,n,x: (setattr(p._obj.BasicLimitInformation,'LimitFlags',0x2000),1)[1])
        def pipe(r,w,sa,size):
            a,b=os.pipe(); open_fds.update((a,b)); r._obj.value=a; w._obj.value=b
            calls.append(('pipe',a,b,sa._obj.bInheritHandle)); return 1
        function('CreatePipe',pipe)
        def null(*args):
            fd=os.open(os.devnull,os.O_WRONLY); open_fds.add(fd); return fd
        function('CreateFileW',null)
        def init(p,count,flags,size):
            size._obj.value=128; return int(p is not None)
        function('InitializeProcThreadAttributeList',init)
        def attr(p,flags,key,value,size,previous,result):
            calls.append(('attribute',key,tuple(value))); return 1
        function('UpdateProcThreadAttribute',attr)
        def create(exe,command,ps,ts,inherit,flags,env,cwd,startup,info):
            calls.append(('create',exe,inherit,flags,ps,ts))
            info._obj.hProcess=9002; info._obj.hThread=9003; info._obj.dwProcessId=999
            return 1
        function('CreateProcessW',create)
        def close(h):
            calls.append(('close',h))
            if h in open_fds: open_fds.remove(h); os.close(h)
            return 1
        function('CloseHandle',close)
        fake_msvcrt = types.SimpleNamespace(open_osfhandle=lambda handle,flags: handle)
        api=Win32API(kernel=kernel); job=api.create_job(); created=None
        try:
            with patch.dict(sys.modules, {'msvcrt':fake_msvcrt}), patch.object(os,'O_BINARY',0,create=True):
                created=api.create_suspended([sys.executable,'-B','-c','pass'],None,{},job)
            self.assertIn(('inherit',9001,1,0),calls)
            self.assertIn(('limits',9001,9,0x2000),calls)
            jobs=[x for x in calls if x[:2]==('attribute',0x2000D)]
            handles=[x for x in calls if x[:2]==('attribute',0x20002)]
            self.assertEqual(jobs,[('attribute',0x2000D,(9001,))])
            self.assertEqual(len(handles[0][2]),3)
            self.assertNotIn(9001,handles[0][2])
            create_call=[x for x in calls if x[0]=='create'][0]
            self.assertTrue(create_call[2])
            self.assertEqual(create_call[3] & (4|0x80000|0x400),4|0x80000|0x400)
            self.assertFalse(create_call[3] & 0x01000000)  # No CREATE_BREAKAWAY_FROM_JOB.
            self.assertIsNone(create_call[4]); self.assertIsNone(create_call[5])
        finally:
            if created:
                for stream in (created.stdin,created.stdout):
                    if stream:
                        open_fds.discard(stream.fileno()); stream.close()
            for fd in open_fds: os.close(fd)


if __name__ == '__main__': unittest.main()
