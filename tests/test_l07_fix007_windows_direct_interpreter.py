"""FIX007 interpreter PID and Job containment contracts, synthetic only.

All POSIX-safe tests mock *planning facts* (not the runtime itself). Native
Windows tests run only under the exact pinned CPython 3.12.14 environment;
there is no browser, SHIRABE, network, or Issue/Stable approval here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from cdpd_chatgpt_profile import worker_interpreter as wi


def runtime(tmp_path, *, ver=(3, 12, 14), implementation='cpython', base_missing=False):
    venv, home = tmp_path/'env', tmp_path/'base'
    (venv/'Scripts').mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    exe, base = venv/'Scripts'/'python.exe', home/'python.exe'
    exe.write_bytes(b'PYTHON-VENV-REDIRECTOR')
    if not base_missing:
        base.write_bytes(b'PYTHON-INTERPRETER')
    (venv/'pyvenv.cfg').write_text('home = base\n')
    return wi._Runtime('win32', implementation, ver, str(exe), str(base),str(venv),str(home))


def plan(monkeypatch, tmp_path, *, ver=(3, 12, 14), prod=True):
    rt = runtime(tmp_path, ver=ver)
    monkeypatch.setattr(wi, '_runtime', lambda: rt)
    argv = [rt.executable, '-B', '-m', 'cdpd_chatgpt_profile.worker_cli'] if prod else [rt.executable,'-B','-c','print(1)']
    return wi.plan_worker_launch(argv, {'__PYVENV_LAUNCHER__':'UNTRUSTED', 'PYTHONHOME':'OLD', 'LANG':'C'})


def test_fix007_linux_launch_is_unchanged_and_copies_environment(monkeypatch):
    monkeypatch.setattr(wi, '_runtime', lambda: wi._Runtime('linux','cpython',(3,13,5),'/usr/bin/python','/usr/bin/python','/usr','/usr'))
    env={'PYTHONPATH':'CODE'}
    argv,e,strategy=wi.plan_worker_launch(['/usr/bin/python','-B','-m','cdpd_chatgpt_profile.worker_cli'],env)
    assert argv[0]=='/usr/bin/python' and e == env and e is not env and strategy=='UNCHANGED_NONWINDOWS'


def test_fix007_pinned_windows_base_is_direct_and_attestation_bound(tmp_path,monkeypatch):
    rt=runtime(tmp_path);monkeypatch.setattr(wi,'_runtime',lambda:rt)
    cmd,e,mode=wi.plan_worker_launch([rt.executable,'-B','-m','cdpd_chatgpt_profile.worker_cli'],
                                        {'__PYVENV_LAUNCHER__':'untrusted','PYTHONHOME':'untrusted','EXTRA':'SAFE'})
    assert cmd == [rt.base_executable,'-B','-m','cdpd_chatgpt_profile.worker_cli']
    assert e['__PYVENV_LAUNCHER__']==rt.executable
    assert 'PYTHONHOME' not in e and e['EXTRA']=='SAFE'
    assert e['CDPD_L07_EXPECTED_VENV_PREFIX']==rt.prefix
    assert e['CDPD_L07_EXPECTED_VENV_EXECUTABLE']==rt.executable
    assert e['CDPD_L07_EXPECTED_BASE_EXECUTABLE']==rt.base_executable
    assert mode=='WIN_CP31214_DIRECT_BASE_ATTESTED'
    assert wi.worker_runtime_attested(e)


def test_fix007_pinned_fixture_direct_interpreter_does_not_claim_product_attestation(tmp_path,monkeypatch):
    cmd,e,mode=plan(monkeypatch,tmp_path,prod=False)
    assert cmd[0]!=e['__PYVENV_LAUNCHER__'] and mode=='WIN_CP31214_DIRECT_BASE_ATTESTED'
    assert not any(k.startswith('CDPD_L07_EXPECTED_') for k in e)


@pytest.mark.parametrize('ver',[(3,12,13),(3,12,15),(3,13,5)])
def test_fix007_other_windows_venv_python_versions_fail_closed(tmp_path,monkeypatch,ver):
    with pytest.raises(wi.WorkerInterpreterError) as e:
        plan(monkeypatch,tmp_path,ver=ver)
    assert e.value.code=='UNSUPPORTED_WINDOWS_VENV_INTERPRETER'


def test_fix007_missing_trusted_base_python_fails_closed(tmp_path,monkeypatch):
    rt=runtime(tmp_path,base_missing=True);monkeypatch.setattr(wi,'_runtime',lambda:rt)
    with pytest.raises(wi.WorkerInterpreterError) as e:
        wi.plan_worker_launch([rt.executable],{})
    assert e.value.code=='UNTRUSTED_WINDOWS_VENV_PATH'


def test_fix007_wrong_venv_redirector_location_fails_closed(tmp_path,monkeypatch):
    rt=runtime(tmp_path)
    rt=wi._Runtime(rt.platform,rt.implementation,rt.version,str(tmp_path/'outside'/'python.exe'),rt.base_executable,rt.prefix,rt.base_prefix)
    monkeypatch.setattr(wi,'_runtime',lambda:rt)
    with pytest.raises(wi.WorkerInterpreterError):wi.plan_worker_launch([rt.executable],{})


def test_fix007_python_non_cpython_fails_closed(tmp_path,monkeypatch):
    rt=runtime(tmp_path,implementation='pypy');monkeypatch.setattr(wi,'_runtime',lambda:rt)
    with pytest.raises(wi.WorkerInterpreterError):wi.plan_worker_launch([rt.executable],{})


def test_fix007_attestation_rejects_tampering(tmp_path,monkeypatch):
    rt=runtime(tmp_path);monkeypatch.setattr(wi,'_runtime',lambda:rt)
    _,env,_=wi.plan_worker_launch([rt.executable,'-B','-m','cdpd_chatgpt_profile.worker_cli'],{})
    for key in ['CDPD_L07_EXPECTED_VENV_PREFIX','CDPD_L07_EXPECTED_VENV_EXECUTABLE','CDPD_L07_EXPECTED_BASE_EXECUTABLE']:
        changed=dict(env);changed[key]=str(tmp_path/'other')
        assert wi.worker_runtime_attested(changed) is False
        changed.pop(key)
        assert wi.worker_runtime_attested(changed) is False
        changed=dict(env);changed[key]=None
        assert wi.worker_runtime_attested(changed) is False
    assert wi.worker_runtime_attested(env) is True


def test_fix007_base_python_custom_command_never_has_launcher_hint(tmp_path,monkeypatch):
    rt=runtime(tmp_path);monkeypatch.setattr(wi,'_runtime',lambda:rt)
    cmd,e,mode=wi.plan_worker_launch([rt.base_executable,'-B','-c','pass'],{'__PYVENV_LAUNCHER__':'fake','CDPD_L07_EXPECTED_VENV_PREFIX':'fake'})
    assert cmd[0]==rt.base_executable and '__PYVENV_LAUNCHER__' not in e
    assert 'CDPD_L07_EXPECTED_VENV_PREFIX' not in e and mode=='UNCHANGED_CUSTOM_EXECUTABLE'


def test_fix007_base_runtime_never_forces_venv_hint(tmp_path,monkeypatch):
    home=tmp_path/'base';home.mkdir();exe=home/'python.exe';exe.touch()
    rt=wi._Runtime('win32','cpython',(3,12,14),str(exe),str(exe),str(home),str(home))
    monkeypatch.setattr(wi,'_runtime',lambda:rt)
    cmd,e,mode=wi.plan_worker_launch([str(exe),'-B'],{'__PYVENV_LAUNCHER__':'fake'})
    assert cmd[0]==str(exe) and '__PYVENV_LAUNCHER__' not in e
    assert mode=='UNCHANGED_BASE_INTERPRETER'


def test_fix007_incomplete_or_malformed_command_or_env_rejected():
    for cmd,env in [([],{}),([1],{}),(['python'],{'K':1})]:
        with pytest.raises(wi.WorkerInterpreterError):wi.plan_worker_launch(cmd,env)


def test_fix007_production_cli_denies_unbound_venv_before_requests(monkeypatch):
    from cdpd_chatgpt_profile import worker_cli
    monkeypatch.setattr(sys,'argv',['worker_cli'])
    monkeypatch.setattr(wi,'worker_runtime_attested',lambda:False)
    def forbidden():raise AssertionError('untrusted runtime must not read request')
    monkeypatch.setattr(worker_cli,'serve_worker',forbidden)
    assert worker_cli.main()==65


def test_fix007_process_runner_uses_exact_launch_planner(monkeypatch):
    from cdpd_chatgpt_profile import process_runner
    from cdpd_chatgpt_profile.worker import WorkerRequest
    from test_capture_worker import request_data, Permit
    seen=[]
    def planner(cmd,env):
        seen.append((tuple(cmd),env.get('PYTHONDONTWRITEBYTECODE')))
        return cmd,env,'TEST_DIRECT_BASE'
    monkeypatch.setattr(process_runner,'plan_worker_launch',planner)
    def refuse(*args,**kwargs):raise OSError('simulated no process')
    monkeypatch.setattr(process_runner.subprocess,'Popen',refuse)
    runner=process_runner.ProcessWorkerRunner()
    result=runner(WorkerRequest.from_dict(request_data()),permit=Permit())
    assert len(seen)==1 and seen[0][1]=='1'
    assert runner.last_process_metadata['python_launch_strategy']=='TEST_DIRECT_BASE'
    assert runner.last_process_metadata['spawn_count']==0 and result['structure'] is None


def test_fix007_watchdog_uses_exact_launch_planner(monkeypatch):
    from cdpd_chatgpt_profile import watchdog_runner
    from cdpd_chatgpt_profile.worker import WorkerRequest
    from test_capture_worker import request_data, Permit
    seen=[]
    def planner(cmd,env):
        seen.append(tuple(cmd))
        return ['TEST_DIRECT_BASE', *cmd[1:]],env,'TEST_DIRECT_BASE'
    monkeypatch.setattr(watchdog_runner,'plan_worker_launch',planner)
    def refused(*args,**kwargs):
        seen.append(tuple(args[0]));raise RuntimeError('injected precreation failure')
    runner=watchdog_runner.WatchdogWorkerRunner(process_factory=refused)
    result=runner(WorkerRequest.from_dict(request_data()),permit=Permit())
    assert len(seen)==2 and seen[1][0]=='TEST_DIRECT_BASE'
    assert result['structure'] is None and runner.last_process_metadata['spawn_attempt_count']==1


WINDOWS_PINNED=(sys.platform=='win32' and sys.implementation.name=='cpython'
                and sys.version_info[:3]==(3,12,14)
                and sys.prefix!=sys.base_prefix)


@pytest.mark.skipif(not WINDOWS_PINNED,reason='requires pinned CPython 3.12.14 Windows native')
def test_fix007_windows_real_worker_pid_is_exact_os_created_pid():
    argv,e,strategy=wi.plan_worker_launch([sys.executable,'-B','-c',
        'import os,sys;print(os.getpid());print(sys.prefix);print(sys.executable)'],os.environ.copy())
    proc=subprocess.Popen(argv,env=e,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,close_fds=True,text=True)
    try:
        lines=proc.communicate(timeout=8)[0].splitlines()
        # Real PIDs and paths are transient and never stored in test evidence.
        assert proc.returncode==0 and len(lines)==3
        assert int(lines[0])==proc.pid
        assert wi._path_eq(lines[1],sys.prefix)
        assert wi._path_eq(lines[2],sys.executable)
        assert strategy=='WIN_CP31214_DIRECT_BASE_ATTESTED'
    finally:
        if proc.poll() is None:proc.kill();proc.wait(timeout=2)


@pytest.mark.skipif(not WINDOWS_PINNED,reason='requires pinned CPython 3.12.14 Windows native')
def test_fix007_windows_job_contains_real_python_and_confirms_termination():
    from cdpd_chatgpt_profile.owned_process import OwnedWorker
    from cdpd_chatgpt_profile._l07_win32 import Win32API
    cmd,e,_=wi.plan_worker_launch([sys.executable,'-B','-c',
        'import os,sys,time;print(os.getpid(),flush=True);time.sleep(8)'],os.environ.copy())
    worker=OwnedWorker.spawn(cmd,env=e,deadline_monotonic=time.monotonic()+5)
    try:
        # The only native process returned by CreateProcess must be Job-owned.
        assert worker.supervision_scope=='WINDOWS_PRIVATE_JOB_OBJECT'
        assert Win32API().is_in_job(worker._backend.created.process,worker._backend.job)
        child_pid=int(worker.stdout.readline().decode('ascii').strip())
        assert worker.pid==child_pid
        result=worker.terminate_and_confirm(timeout_s=2.0)
        assert result.confirmed and result.active_processes==0
    finally:
        worker.terminate_and_confirm(timeout_s=2.0)
        worker.close()


@pytest.mark.skipif(not WINDOWS_PINNED,reason='requires pinned CPython 3.12.14 Windows native')
@pytest.mark.parametrize('mode,reason',[
    ('normal','NONE'),('duplicate_result','OUTPUT_AFTER_RESULT'),('wrong_pid','RESULT_INVALID')
])
def test_fix007_windows_watchdog_authenticated_result_and_rejections(mode,reason):
    from cdpd_chatgpt_profile.watchdog_runner import WatchdogWorkerRunner
    from cdpd_chatgpt_profile.worker import WorkerRequest
    from test_capture_worker import request_data,Permit
    root=Path(__file__).resolve().parents[1]
    class NativeFixture(WatchdogWorkerRunner):
        def _command(self):return [sys.executable,'-B',str(root/'tests'/'l06_process_fixture.py'),mode]
    runner=NativeFixture()
    result=runner(WorkerRequest.from_dict(request_data(timeout=7)),permit=Permit())
    md=runner.last_process_metadata
    assert md['failure_reason']==reason
    assert md['termination_facts']['confirmed'] and md['termination_facts']['active_processes']==0
    if mode=='normal':
        assert result['capture_status']=='COMPLETE' and result['pid']==md['pid']
    else:
        assert result['structure'] is None
