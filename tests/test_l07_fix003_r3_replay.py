"""Independent assessment-only publication ordering probes.
Outside the FIX002 Candidate and its manifest; uses fixed 25 externally verified originals.
"""
import os
import threading
from pathlib import Path
from time import monotonic

import pytest
import cdpd_chatgpt_profile.evidence_store as storemod
from cdpd_chatgpt_profile.capture_service import CaptureServiceError
from cdpd_chatgpt_profile.evidence_store import verify_profile_bundle
from cdpd_chatgpt_profile.owned_process import RegisteredProcess,process_creation_identity
from test_l06_capture_service import snapshot
from test_l07_fix001_supervisor import INVALIDATE, subject, event, begin, durable_receive

TIMEOUT=5

def launch_capture(coordinator, job):
    result={}
    def task():
        try: result['output']=coordinator.capture(job)
        except Exception as ex: result['error']=ex
    thread=threading.Thread(target=task, daemon=True)
    thread.start()
    return thread,result

def live(tmp_path, *, process=None):
    sup, guard, authority, now=subject(tmp_path,process=process)
    initial, operation=begin(sup)
    job=sup._job(initial)
    coord=sup._coordinator(sup._scoped_permit(operation.context))
    return sup, guard, authority, operation, job, coord

@pytest.mark.parametrize('row', INVALIDATE, ids=lambda row: row['case_id'])
def test_assessment_independent_post_permit_pre_rename_six_rows(tmp_path, monkeypatch, row):
    sup, guard, authority, op, job, coord=live(tmp_path)
    reached, release=threading.Event(),threading.Event()
    gate=op.context.publication_gate
    original=gate.publish
    def publish_paused(action):
        reached.set()  # after coordinator final permit + staged COMMIT fsync
        assert release.wait(TIMEOUT)
        return original(action)
    monkeypatch.setattr(gate,'publish',publish_paused)
    thread,box=launch_capture(coord,job)
    assert reached.wait(TIMEOUT)
    t0=monotonic()
    durable_receive(sup,event(row=row,seq=2))
    sup.tick()
    latch_seconds=monotonic()-t0
    assert latch_seconds<1.0, 'watchdog local invalidation waited on parked writer'
    assert gate.invalidated and not gate.published
    release.set()
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    assert isinstance(box.get('error'), CaptureServiceError)
    assert box['error'].code=='PUBLISH_GUARD_DENIED'
    bundle=sup.root/'bundles'/job.to_dict()['capture_id']
    assert not (bundle/'COMMIT.json').exists() and not (bundle/'STRUCTURE.json').exists()
    assert not (bundle/'.COMMIT.staged').exists()
    assert verify_profile_bundle(bundle)['publication_state']=='INCOMPLETE'


def test_assessment_independent_keeper_observation_lost_pre_rename(tmp_path, monkeypatch):
    proc=RegisteredProcess.open(os.getpid(),process_creation_identity(os.getpid()))
    try:
        sup,guard,auth,op,job,coord=live(tmp_path,process=proc)
        reached,release=threading.Event(),threading.Event()
        gate=op.context.publication_gate
        original=gate.publish
        def publish_paused(action):
            reached.set()
            assert release.wait(TIMEOUT)
            return original(action)
        monkeypatch.setattr(gate,'publish',publish_paused)
        thread,box=launch_capture(coord,job)
        assert reached.wait(TIMEOUT)
        proc.close() # observation-unavailable, not process death
        t0=monotonic()
        sup.tick()
        assert monotonic()-t0<1.0
        assert gate.invalidated
        release.set()
        thread.join(TIMEOUT)
        assert isinstance(box.get('error'),CaptureServiceError)
        assert box['error'].code=='PUBLISH_GUARD_DENIED'
        bundle=sup.root/'bundles'/job.to_dict()['capture_id']
        assert not (bundle/'COMMIT.json').exists() and not (bundle/'STRUCTURE.json').exists()
    finally:
        proc.close()


def test_assessment_independent_commit_linearized_first(tmp_path,monkeypatch):
    sup,guard,auth,op,job,coord=live(tmp_path)
    reached,release=threading.Event(),threading.Event()
    gate=op.context.publication_gate
    original=gate.publish
    def publish_paused(action):
        def action_paused():
            reached.set()
            assert release.wait(TIMEOUT)
            return action()
        return original(action_paused)
    monkeypatch.setattr(gate,'publish',publish_paused)
    thread,box=launch_capture(coord,job)
    assert reached.wait(TIMEOUT)
    accepted=threading.Event()
    def invalidate():
        durable_receive(sup,event(row=INVALIDATE[0],seq=2))
        sup.tick()
        accepted.set()
    inv=threading.Thread(target=invalidate,daemon=True)
    inv.start()
    assert not accepted.wait(.05)
    release.set()
    thread.join(TIMEOUT); inv.join(TIMEOUT)
    assert 'error' not in box and accepted.is_set()
    bundle=Path(box['output'].bundle_path)
    assert (bundle/'COMMIT.json').exists() and gate.published and gate.invalidated
    after=snapshot(bundle)
    sup.tick()
    assert after==snapshot(bundle)


def test_assessment_nonblocking_payload_io(tmp_path,monkeypatch):
    sup,guard,auth,op,job,coord=live(tmp_path)
    reached,release=threading.Event(),threading.Event()
    original=storemod._write_new
    def block_payload(path,raw):
        original(path,raw)
        if Path(path).name=='TRIGGER.json':
            reached.set()
            assert release.wait(TIMEOUT)
    monkeypatch.setattr(storemod,'_write_new',block_payload)
    thread,box=launch_capture(coord,job)
    assert reached.wait(TIMEOUT)
    t0=monotonic()
    durable_receive(sup,event(row=INVALIDATE[0],seq=2));sup.tick()
    assert monotonic()-t0<1.0
    assert op.context.publication_gate.invalidated
    release.set()
    thread.join(TIMEOUT)
    assert isinstance(box.get('error'),CaptureServiceError)
    assert box['error'].code=='PUBLISH_GUARD_DENIED'


def test_assessment_nonblocking_post_commit_directory_fsync(tmp_path,monkeypatch):
    sup,guard,auth,op,job,coord=live(tmp_path)
    reached,release=threading.Event(),threading.Event()
    original=storemod._sync_directory
    def block_post_commit_sync(path):
        if Path(path).name==job.to_dict()['capture_id'] and (Path(path)/'COMMIT.json').exists() and not reached.is_set():
            reached.set()
            assert release.wait(TIMEOUT)
        return original(path)
    monkeypatch.setattr(storemod,'_sync_directory',block_post_commit_sync)
    thread,box=launch_capture(coord,job)
    assert reached.wait(TIMEOUT)
    bundle=sup.root/'bundles'/job.to_dict()['capture_id']
    before=snapshot(bundle)
    t0=monotonic()
    durable_receive(sup,event(row=INVALIDATE[0],seq=2));sup.tick()
    assert monotonic()-t0<1.0
    assert op.context.publication_gate.published and op.context.publication_gate.invalidated
    assert before==snapshot(bundle)
    release.set()
    thread.join(TIMEOUT)
    assert 'error' not in box and before==snapshot(bundle)


def test_assessment_commit_immutability_after_post_commit_sync_failure(tmp_path,monkeypatch):
    """Fault injection at a site outside the publication lock after atomic rename.

    The positive ordering contract says once actual COMMIT publication wins,
    later invalidation must not rewrite a committed bundle.  Force that path's
    post-rename fsync to fail after the watchdog has accepted invalidation.
    """
    sup,guard,auth,op,job,coord=live(tmp_path)
    reached,release=threading.Event(),threading.Event()
    original=storemod._sync_directory
    def fail_post_commit_sync(path):
        if Path(path).name==job.to_dict()['capture_id'] and (Path(path)/'COMMIT.json').exists() and not reached.is_set():
            reached.set()
            assert release.wait(TIMEOUT)
            raise OSError('SYNTHETIC_INDEPENDENT_FSYNC_FAILURE')
        return original(path)
    monkeypatch.setattr(storemod,'_sync_directory',fail_post_commit_sync)
    thread,box=launch_capture(coord,job)
    assert reached.wait(TIMEOUT)
    bundle=sup.root/'bundles'/job.to_dict()['capture_id']
    assert (bundle/'COMMIT.json').exists()
    before=snapshot(bundle)
    assert op.context.publication_gate.published
    durable_receive(sup,event(row=INVALIDATE[0],seq=2));sup.tick()
    assert op.context.publication_gate.invalidated
    release.set()
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    # A bundle whose COMMIT already linearized and whose hashes were captured
    # before invalidation must not change after that accepted invalidation.
    assert before==snapshot(bundle)


def test_assessment_keeper_immutability_after_post_commit_sync_failure(tmp_path,monkeypatch):
    proc=RegisteredProcess.open(os.getpid(),process_creation_identity(os.getpid()))
    try:
        sup,guard,auth,op,job,coord=live(tmp_path,process=proc)
        reached,release=threading.Event(),threading.Event()
        original=storemod._sync_directory
        def fail_after_commit(path):
            if Path(path).name==job.to_dict()['capture_id'] and (Path(path)/'COMMIT.json').exists() and not reached.is_set():
                reached.set()
                assert release.wait(TIMEOUT)
                raise OSError('SYNTHETIC_POST_PUBLICATION_FSYNC_ERROR')
            return original(path)
        monkeypatch.setattr(storemod,'_sync_directory',fail_after_commit)
        thread,box=launch_capture(coord,job)
        assert reached.wait(TIMEOUT)
        bundle=sup.root/'bundles'/job.to_dict()['capture_id']
        before=snapshot(bundle)
        assert op.context.publication_gate.published and (bundle/'COMMIT.json').exists()
        proc.close()
        sup.tick()  # authenticated keeper observation unavailable
        assert op.context.publication_gate.invalidated
        release.set()
        thread.join(TIMEOUT)
        assert not thread.is_alive()
        assert before==snapshot(bundle)
    finally:
        proc.close()


def test_assessment_nonblocking_staged_commit_fsync(tmp_path,monkeypatch):
    sup,guard,auth,op,job,coord=live(tmp_path)
    reached,release=threading.Event(),threading.Event()
    # FIX005: identify the exact staged write via a lexical, per-thread scope.
    # /proc/self/fd is Linux-only; inspecting fd numbers is also vulnerable to
    # reuse.  This injection never pauses payload, manifest or directory fsync.
    original_write = storemod._write_new
    original_fsync = storemod.os.fsync
    stage_scope = threading.local()
    def instrumented_new_write(path, raw):
        if Path(path).name != '.COMMIT.staged':
            return original_write(path, raw)
        assert not getattr(stage_scope, 'active', False)
        stage_scope.active = True
        try:
            return original_write(path, raw)
        finally:
            stage_scope.active = False
    def block_only_staged_fsync(fd):
        if getattr(stage_scope, 'active', False) and not reached.is_set():
            reached.set()
            assert release.wait(TIMEOUT)
        return original_fsync(fd)
    monkeypatch.setattr(storemod, '_write_new', instrumented_new_write)
    monkeypatch.setattr(storemod.os, 'fsync', block_only_staged_fsync)
    thread,box=launch_capture(coord,job)
    assert reached.wait(TIMEOUT)
    t0=monotonic()
    durable_receive(sup,event(row=INVALIDATE[0],seq=2));sup.tick()
    assert monotonic()-t0<1.0
    assert op.context.publication_gate.invalidated
    release.set();thread.join(TIMEOUT)
    assert isinstance(box.get('error'),CaptureServiceError)
    assert not (sup.root/'bundles'/job.to_dict()['capture_id']/'COMMIT.json').exists()


def test_assessment_nonblocking_shared_authority_notice(tmp_path,monkeypatch):
    sup,guard,auth,op,job,coord=live(tmp_path)
    callback_entered,release=threading.Event(),threading.Event()
    original=auth.invalidate
    def slow_authority_notice(*args,**kwargs):
        callback_entered.set()
        assert release.wait(TIMEOUT)
        return original(*args,**kwargs)
    monkeypatch.setattr(auth,'invalidate',slow_authority_notice)
    t0=monotonic()
    durable_receive(sup,event(row=INVALIDATE[0],seq=2));sup.tick()
    assert monotonic()-t0<1.0
    assert callback_entered.wait(TIMEOUT)
    assert op.context.publication_gate.invalidated
    release.set()
