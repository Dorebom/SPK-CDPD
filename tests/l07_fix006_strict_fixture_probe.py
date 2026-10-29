#!/usr/bin/env python3
"""FIX006 local synthetic protocol probe. NEVER Chrome, SHIRABE, or network.

Uses the same strict parent validators without replacing their decisions.
Reports ONLY enumerated reason codes/counts; never stdout payload, DOM, raw
stderr, PIDs, input/secret hashes, paths, or arbitrary exception messages.
No product modules import this diagnostic file.
"""
from __future__ import annotations
import argparse, json, os, pathlib, sys, time
from collections import Counter

ALLOWED = (
    ('process', 'l06_process_fixture.py', 'normal'),
    ('process', 'l06_process_fixture.py', 'capture_exception'),
    ('process', 'l06_fix001_child_fixture.py', 'pages_before'),
    ('process', 'l06_process_fixture.py', 'duplicate_result'),
    ('watchdog', 'l06_process_fixture.py', 'normal'),
    ('watchdog', 'l06_fix001_child_fixture.py', 'epoch_after'),
    ('watchdog', 'l06_process_fixture.py', 'duplicate_result'),
)
PROCESS_REASONS = frozenset({'NONE','DEADLINE','CANCELLED','RESULT_INVALID','MISSING_RESULT',
 'OUTPUT_AFTER_RESULT','PERMIT_INVALID','PERMIT_TIMEOUT','PERMIT_CALLBACK_FAILED','PROTOCOL_INVALID',
 'CHILD_EXITED_ERROR','TRUNCATED_LINE','OUTPUT_TOO_LARGE','TOO_MANY_MESSAGES',
 'WORKER_TERMINATION_UNCONFIRMED','WORKER_RESOURCE_CLEANUP_UNCONFIRMED','SPAWN_FAILED',
 'DESCENDANTS_AFTER_RESULT','PIPE_CLOSED','INPUT_INVALID','OUTPUT_TOO_LARGE','WORKER_ALREADY_ACTIVE'})

def probe_one(root, runner_type, fixture, mode, timeout):
    from cdpd_chatgpt_profile import process_runner as pr, watchdog_runner as wd
    from cdpd_chatgpt_profile.worker import WorkerRequest
    from test_capture_worker import request_data, Permit
    from l07_fix006_protocol_diagnostic import result_reason, RESULT_CODES
    module = pr if runner_type=='process' else wd
    orig_result = module._valid_result
    orig_challenge = module._valid_challenge
    orig_response = module._valid_response
    reasons=[]
    challenges=Counter()
    responses=Counter()
    # Observe strict decisions. The original validators remain the sole authority.
    def result_wrapper(value, data, pid):
        ok=orig_result(value,data,pid)
        reasons.append(result_reason(value,data,pid,validator=orig_result))
        return ok
    def challenge_wrapper(value,data,used):
        ok=orig_challenge(value,data,used)
        challenges['ACCEPTED' if ok else 'DENIED']+=1
        return ok
    def response_wrapper(response, challenge):
        ok=orig_response(response,challenge)
        responses['ACCEPTED' if ok else 'DENIED']+=1
        return ok
    module._valid_result=result_wrapper
    module._valid_challenge=challenge_wrapper
    module._valid_response=response_wrapper
    try:
        class FixedRunner(pr.ProcessWorkerRunner if runner_type=='process' else wd.WatchdogWorkerRunner):
            def _command(self):
                return [sys.executable,'-B',str(root/'tests'/fixture),mode]
        runner=FixedRunner()
        job=WorkerRequest.from_dict(request_data(timeout=timeout))
        result=runner(job,permit=Permit())
        meta=runner.last_process_metadata
        failure=meta.get('failure_reason')
        if failure not in PROCESS_REASONS:failure='OTHER'
        reason_counts={x:reasons.count(x) for x in sorted(set(reasons)) if x in RESULT_CODES}
        if not reasons:reason_counts={'RESULT_NOT_OBSERVED':1}
        code=meta.get('child_exit_code')
        if type(code) is int:
            exit_class='ZERO' if code==0 else 'NONZERO'
        else:exit_class='UNAVAILABLE'
        termination=meta.get('termination')
        if termination not in {'NORMAL','NOT_STARTED','KILLED','EXITED_ERROR','UNCONFIRMED'}:termination='OTHER'
        facts=meta.get('termination_facts') or {}
        result_status=result.get('capture_status')
        if result_status not in {'COMPLETE','PARTIAL','REJECTED'}:result_status='OTHER'
        return {
            'runner':runner_type,'fixture':fixture,'mode':mode,
            'result_status':result_status,
            'accepted_structure':result.get('structure') is not None,
            'result_validation_reason_counts':reason_counts,
            'challenge_counts':dict(challenges),'permit_response_counts':dict(responses),
            'parent_failure_reason':failure,'termination':termination,
            'child_exit_class':exit_class,
            'owned_job_backend':meta.get('supervision_scope')=='WINDOWS_PRIVATE_JOB_OBJECT',
            'termination_confirmed':facts.get('confirmed') if type(facts.get('confirmed')) is bool else None,
            'active_processes_zero':facts.get('active_processes')==0 if type(facts.get('active_processes')) is int else None,
            'termination_unconfirmed':bool(meta.get('termination_unconfirmed',False)),
            'deadline_exceeded':bool(meta.get('timed_out',False)),
            'permit_checks':meta.get('permit_checks') if type(meta.get('permit_checks')) is int else None,
            'stdout_bytes':meta.get('stdout_bytes') if type(meta.get('stdout_bytes')) is int else None,
            'no_payload_or_stderr_persisted':True,
        }
    finally:
        module._valid_result=orig_result
        module._valid_challenge=orig_challenge
        module._valid_response=orig_response

def run(root, timeout):
    for path in [root/'code',root/'tests',*sorted(root.glob('packages/*/src'))]:
        sys.path.insert(0,str(path))
    os.environ['PYTHONPATH']=os.pathsep.join([str(root/'code'),str(root/'tests'),
        *(str(p) for p in sorted(root.glob('packages/*/src')))])
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.environ['PYTHONNOUSERSITE']='1'
    os.environ['CDPD_TEST_BROWSER']='0'
    cases=[]
    for runner,fixture,mode in ALLOWED:
        try:cases.append(probe_one(root,runner,fixture,mode,timeout))
        except BaseException as exc:
            # Only a fixed Python exception class, no string, path or traceback.
            name=type(exc).__name__
            if name not in {'AssertionError','TypeError','ValueError','OSError','ImportError',
                            'RuntimeError','TimeoutError','FileNotFoundError','PermissionError'}:
                name='OtherException'
            cases.append({'runner':runner,'fixture':fixture,'mode':mode,
                          'diagnostic_exception_class':name,'no_payload_or_stderr_persisted':True})
    return {'schema':'cdpd.l07.fix006.strict-owned-fixture-probe/v1',
        'scope':'SYNTHETIC_FIXED_FIXTURES_ONLY',
        'platform':'WINDOWS' if os.name=='nt' else 'NONWINDOWS',
        'python_version':sys.version.split()[0],
        'test_count':len(cases),'cases':cases,
        'strict_production_validators_unchanged':True,
        'browser_network_shirabe':'NOT_RUN', 'native_product_acceptance':'NOT_RUN',
        'raw_child_stderr_stored':False,'raw_ipc_or_dom_stored':False,
        'fixture_probe_pass_is_not_suite_acceptance':True}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',required=True)
    ap.add_argument('--output',required=True)
    ap.add_argument('--timeout',type=float,default=7.0)
    a=ap.parse_args()
    root=pathlib.Path(a.root).resolve()
    if not (root/'tests/test_capture_worker.py').is_file() or not (root/'code/cdpd_chatgpt_profile/watchdog_runner.py').is_file():
        raise SystemExit('invalid candidate root')
    if not 2<=a.timeout<=20:raise SystemExit('invalid bounded timeout')
    evidence=run(root,a.timeout)
    output=pathlib.Path(a.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(json.dumps({'scope':evidence['scope'],'cases':evidence['test_count'],
                      'platform':evidence['platform'],'product_acceptance':'NOT_RUN'}))
if __name__=='__main__':raise SystemExit(main())
