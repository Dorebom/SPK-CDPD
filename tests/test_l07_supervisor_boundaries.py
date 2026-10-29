"""Execution regressions for exact process registration and restart admission.

Local typed fixtures only: no OS child, network, browser or native acceptance.
"""
from copy import deepcopy
import hashlib
import queue
from types import SimpleNamespace

import pytest

from cdpd_chatgpt_profile.spool import SpoolError, canonical_json
from cdpd_chatgpt_profile.supervisor import Supervisor, SupervisorConfig, SupervisorError, _pin
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_binding_guard import fixture_guard
from test_capture_worker import request_data


def supervisor_fixture(tmp_path):
    root = tmp_path/'state'
    for name in ('binding_metadata','operations'):
        (root/name).mkdir(parents=True)
    return Supervisor(SupervisorConfig(root,tmp_path,'boundary-run'),monotonic=lambda:1000.0)


def claim_fixture(supervisor):
    job = WorkerRequest.from_dict(request_data())
    data = job.to_dict()
    operation = data['trigger_envelope']['event']['operation_id']
    directory = supervisor.root/'operations'/_pin(['boundary-run',operation])
    directory.mkdir()
    claim = {'schema':'cdpd.supervisor-operation-claim/v1','run_id':'boundary-run',
             'job':data,'job_sha256':hashlib.sha256(job._raw).hexdigest(),
             'scope':'ONCE_PER_RUN_OPERATION_ACROSS_PRODUCER_BINDING_GENERATION',
             'spawn_may_have_occurred':True,'created_utc':'2026-10-25T00:00:00Z'}
    (directory/'CLAIM.json').write_bytes(canonical_json(claim))
    outcome = {'schema':'cdpd.supervisor-outcome/v1','status':'PARTIAL','bundle_path':None,
               'worker_blocked':False,'supervisor_instance_id':data['supervisor_instance_id'],
               'operation_id':operation,'process_metadata':{
                   'spawn_attempt_count':0,'spawn_count':0,'pid':None,'termination':'NOT_STARTED'}}
    return directory,outcome


def process_uncertainty():
    return {'schema':'cdpd.l07-capture-process-metadata/v1','spawn_attempt_count':1,
            'spawn_count':1,'spawn_count_known':True,'pid':12345,'termination':'UNCONFIRMED',
            'new_worker_prohibited':True,'termination_unconfirmed':True,
            'termination_facts':{'requested':True,'confirmed':False,
                'reason':'WORKER_TERMINATION_UNCONFIRMED','exit_code':None,
                'active_processes':None,'native_error':None}}


def evidence(request,expected,actual,*,wd=()):
    request.node._l07_evidence={'expected':expected,'actual':actual,'ac':[],
        'wd':list(wd),'scope':'LOCAL_SYNTHETIC_ADMISSION_NO_PROCESS_NO_NATIVE_WINDOWS'}


@pytest.mark.parametrize('malformation',['missing_process','empty_process','false_confirmation','broken_json'])
def test_malformed_outcome_never_restores_worker_admission(tmp_path,request,malformation):
    supervisor=supervisor_fixture(tmp_path)
    directory,outcome=claim_fixture(supervisor)
    if malformation=='missing_process':
        outcome.pop('process_metadata')
    elif malformation=='empty_process':
        outcome['process_metadata']={}
    elif malformation=='false_confirmation':
        outcome['process_metadata']=process_uncertainty()
        outcome['process_metadata'].update(termination='NORMAL',new_worker_prohibited=False,
                                          termination_unconfirmed=False)
    raw=b'{"schema":' if malformation=='broken_json' else canonical_json(outcome)
    (directory/'OUTCOME.json').write_bytes(raw)
    rejected=False
    try:
        supervisor._load_records()
    except (SupervisorError,SpoolError):
        rejected=True
    actual={'admission_denied':rejected or supervisor.worker_blocked}
    evidence(request,{'admission_denied':True},actual,wd=('WD-05',))
    assert actual=={'admission_denied':True}


def test_outcome_restores_sticky_block_without_separate_marker(tmp_path,request):
    supervisor=supervisor_fixture(tmp_path)
    directory,outcome=claim_fixture(supervisor)
    outcome.update(worker_blocked=True,process_metadata=process_uncertainty())
    (directory/'OUTCOME.json').write_bytes(canonical_json(outcome))
    assert not (supervisor.root/'WORKER_BLOCKED.json').exists()
    supervisor._load_records()
    actual={'worker_blocked':supervisor.worker_blocked,'replay_queued':bool(supervisor._pending)}
    expected={'worker_blocked':True,'replay_queued':False}
    evidence(request,expected,actual,wd=('WD-05',))
    assert actual==expected


@pytest.mark.parametrize('role',['consumer','chrome'])
def test_registration_rejects_duck_typed_pid_only_process(tmp_path,request,role):
    supervisor=supervisor_fixture(tmp_path)
    supervisor._started=True  # Isolate the trusted registration boundary.
    guard,_,_=fixture_guard()
    fake=SimpleNamespace(pid=12345,identity='pid-only-unverified',
                         is_alive=lambda:False,close=lambda:None)
    rejected=False
    try:
        supervisor.register_binding(guard,producer_id='boundary-producer',
            producer_instance_id='boundary-instance',**{role:fake})
    except SupervisorError:
        rejected=True
    actual={'registration_rejected':rejected,'contexts':len(supervisor._contexts),
            'saved_binding_records':len(list((supervisor.root/'binding_metadata').glob('*.json')))}
    expected={'registration_rejected':True,'contexts':0,'saved_binding_records':0}
    evidence(request,expected,actual)
    assert actual==expected


def test_finished_unclaimed_operation_does_not_capture_later_process_exit(tmp_path,request):
    supervisor=supervisor_fixture(tmp_path)
    supervisor._started=True
    supervisor._pump=SimpleNamespace(error=None,events=queue.Queue())
    started=deepcopy(request_data()['trigger_envelope'])
    started['event'].update(kind='ACTION_STARTED',failure_code='NONE',correlation={'run_id':'boundary-run'})
    started['profile'].update(reason='OPERATION_ARMED',dispatch_state='NOT_DISPATCHED')
    supervisor._receive(started,1000.0)
    finished=deepcopy(started)
    finished['event'].update(kind='ACTION_FINISHED')
    finished['profile'].update(reason='OPERATION_FINISHED',dispatch_state='CONFIRMED')
    supervisor._receive(finished,1000.0)
    operation=next(iter(supervisor._operations.values()))
    reads=[]
    process=SimpleNamespace(is_alive=lambda:reads.append('unexpected-observation') or False)
    operation.context=SimpleNamespace(consumer=process,chrome=process)
    supervisor.tick()
    actual={'finished':operation.finished,'claimed':operation.claimed,
            'process_queries':len(reads),'capture_queued':bool(supervisor._pending)}
    expected={'finished':True,'claimed':False,'process_queries':0,'capture_queued':False}
    evidence(request,expected,actual,wd=('WD-04',))
    assert actual==expected
