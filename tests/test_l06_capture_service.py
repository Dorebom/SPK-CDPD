"""L06 coordinator integration with actual accepted durable-spool reader.

The runner is synchronous and in-memory here; separate-process validation is
provided by test_l06_process_worker.py. No live keeper/browser is granted.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from cdpd.collector import CaptureError
from cdpd_chatgpt_profile.capture_service import CaptureCoordinator, CaptureServiceError
from cdpd_chatgpt_profile.evidence_store import ProfileEvidenceStore, verify_profile_bundle
from cdpd_chatgpt_profile.spool import FileMailbox, JournalSpool, SpoolError, _FileLock, canonical_json, read_history, stream_key
from cdpd_chatgpt_profile.worker import WorkerRequest, run_capture_worker
from test_capture_worker import Connection, Permit, observe, request_data


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def make_checkpoint(envelope, *, clean=True):
    return {"schema": "cdpd.producer-checkpoint/v1",
            "producer_id": envelope["event"]["producer_id"],
            "producer_instance_id": envelope["event"]["producer_instance_id"],
            "binding_id": envelope["binding_id"], "binding_sha256": envelope["binding_sha256"],
            "control_generation": envelope["control_generation"],
            "diagnostic_high_water": envelope["producer_seq"], "liveness_high_water": 0,
            "loss_lower_bound": envelope["profile"]["loss_lower_bound"],
            "loss_count_exact": envelope["profile"]["loss_count_exact"], "clean_shutdown": clean}


def spool_fixture(path, envelope, *, clean=True, earlier=()):
    with JournalSpool(path) as writer:
        for row in (*earlier,envelope):
            assert writer.append(row)["stage"] == "ACK_DURABLE"
    FileMailbox(path).publish_checkpoint(make_checkpoint(envelope, clean=clean))


def snapshot(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def snapshot_child_invariance(root):
    """Fixture-only: compare all child-observable files except parent-owned OS lock.

    This excludes precisely state/capture-writer.lock relative to the pytest
    temporary root. The production lock is NOT released or modified, and all
    spool/evidence files remain in the comparison. Other lock files are included.
    """
    lock_relative = 'state/capture-writer.lock'
    lock = root / 'state' / 'capture-writer.lock'
    assert lock.is_file(), 'CAPTURE_PARENT_LOCK_MISSING'
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()
            and p.relative_to(root).as_posix() != lock_relative}


def assert_parent_capture_lock_held(root):
    """A second OS lock holder must be rejected while Coordinator owns it."""
    lock = root / 'state' / 'capture-writer.lock'
    assert lock.is_file()
    try:
        with _FileLock(lock):
            raise AssertionError('PARENT_CAPTURE_LOCK_NOT_HELD')
    except SpoolError as error:
        assert error.code == 'CURRENT_NOT_DURABLE'


def contents(path):
    root = Path(path)
    return {name:json.loads((root/name).read_bytes()) for name in ("TRIGGER.json","EVENTS.json","RESULT.json")}


class Runner:
    def __init__(self, *, factory=None, after=None):
        self.calls = []
        self.factory = factory or (lambda endpoint,timeout:Connection())
        self.after = after

    def __call__(self, job, *, permit):
        self.calls.append(job.to_dict()["capture_id"])
        result = run_capture_worker(job, permit=permit, connection_factory=self.factory)
        if self.after:
            self.after(result)
        return result


def coordinator(tmp_path, data=None, *, clean=True, earlier=(), runner=None, permit=None, now=NOW):
    data = request_data() if data is None else data
    spool_root = tmp_path / "spool"
    spool_fixture(spool_root,data["trigger_envelope"],clean=clean,earlier=earlier)
    runner = runner or Runner()
    store = ProfileEvidenceStore(tmp_path / "bundles")
    service = CaptureCoordinator(tmp_path / "state",spool_root,store,runner=runner,
                                 permit=permit or Permit(),utcnow=lambda:now)
    return service,WorkerRequest.from_dict(data),runner,spool_root,store


def test_complete_spool_to_profile_bundle_preserves_caller_ids_and_spool_bytes(tmp_path,request):
    service,job,runner,spool_root,store = coordinator(tmp_path)
    before = snapshot(spool_root)
    result = service.capture(job)
    payload = contents(result.bundle_path)
    verified = verify_profile_bundle(result.bundle_path)
    assert result.capture_status == "COMPLETE" and verified["integrity_valid"] and verified["profile_valid"]
    assert payload["TRIGGER.json"] == job.to_dict()["trigger_envelope"]["event"]
    assert payload["RESULT.json"]["profile"]["history_coverage"]["complete"] is True
    assert payload["RESULT.json"]["history_dropped_count"] == 0
    assert Path(result.bundle_path,"STRUCTURE.json").is_file()
    assert len(runner.calls) == 1 and before == snapshot(spool_root)
    observe(request,expected={"status":"COMPLETE","profile_valid":True,"caller_event_unchanged":True,"spool_unchanged":True,"runner_calls":1},
            actual={"status":result.capture_status,"profile_valid":verified["profile_valid"],
                    "caller_event_unchanged":payload["TRIGGER.json"] == job.to_dict()["trigger_envelope"]["event"],
                    "spool_unchanged":before == snapshot(spool_root),"runner_calls":len(runner.calls)},
            ac=("AC01","AC02","AC03","AC04"),cap=("CAP-01",))


@pytest.mark.parametrize("clean,loss,exact",[(False,0,True),(True,1,True),(True,0,False)])
def test_history_incomplete_is_partial_with_verified_structure(tmp_path,request,clean,loss,exact):
    service,job,runner,spool_root,store = coordinator(tmp_path,request_data(loss=loss,exact=exact),clean=clean)
    expected_history = read_history(spool_root,stream=stream_key(job.to_dict()["trigger_envelope"]),now_utc=NOW.timestamp())
    assert expected_history["history_coverage"]["complete"] is False
    result = service.capture(job)
    payload = contents(result.bundle_path)
    profile = payload["RESULT.json"]["profile"]
    assert result.capture_status == "PARTIAL" and result.failure_code == "HISTORY_INCOMPLETE"
    assert Path(result.bundle_path,"STRUCTURE.json").is_file()
    inherited_coverage={key:profile["history_coverage"][key] for key in expected_history["history_coverage"]}
    assert inherited_coverage == expected_history["history_coverage"]
    assert profile["history_coverage"]["retained_count"] == len(payload["EVENTS.json"]["events"])
    assert profile["history_coverage"]["window_seconds"] == 120
    assert all(profile["stages"][name]["status"] == "COMPLETE" and profile["stages"][name]["reason_code"] is None for name in ("S1","S2"))
    assert verify_profile_bundle(result.bundle_path)["profile_valid"] is True
    observe(request,expected={"status":"PARTIAL","reason":"HISTORY_INCOMPLETE","structure_retained":True,"source_history_preserved":True},
            actual={"status":result.capture_status,"reason":result.failure_code,"structure_retained":Path(result.bundle_path,"STRUCTURE.json").is_file(),
                    "source_history_preserved":inherited_coverage == expected_history["history_coverage"],"history":profile["history_coverage"]},
            ac=("AC03",),cap=("CAP-01",))


def test_adr001_history_window_shift_does_not_clear_unknown(tmp_path,request):
    from uuid import uuid4
    data = request_data(exact=False)
    old = deepcopy(data["trigger_envelope"])
    current = data["trigger_envelope"]
    current.update(event_id=str(uuid4()),producer_seq=2,occurred_at_utc="2026-10-24T00:03:00Z")
    service,job,runner,spool_root,store = coordinator(tmp_path,data,earlier=(old,),now=NOW+timedelta(seconds=180))
    observed = read_history(spool_root,stream=stream_key(current),now_utc=(NOW+timedelta(seconds=180)).timestamp())
    assert old["event_id"] not in {e["event_id"] for e in observed["events"]}
    assert observed["history_coverage"]["loss_count_exact"] is False
    result=service.capture(job)
    profile=contents(result.bundle_path)["RESULT.json"]["profile"]
    assert result.capture_status == "PARTIAL" and result.failure_code == "HISTORY_INCOMPLETE"
    assert profile["history_coverage"]["loss_count_exact"] is False
    assert profile["history_coverage"]["complete"] is False
    observe(request,expected={"old_event_outside_window":True,"history_complete":False,"loss_exact":False,"status":"PARTIAL"},
            actual={"old_event_outside_window":old["event_id"] not in {e["event_id"] for e in observed["events"]},
                    "history_complete":profile["history_coverage"]["complete"],"loss_exact":profile["history_coverage"]["loss_count_exact"],"status":result.capture_status},
            ac=("AC03",),cap=("CAP-01",))


def test_unresponsive_chrome_commits_s0_s3_only_and_no_second_attempt(tmp_path,request):
    def unavailable(endpoint,timeout):
        raise CaptureError("CDP_CONNECT_FAILED")
    service,job,runner,spool_root,store=coordinator(tmp_path,runner=Runner(factory=unavailable))
    result=service.capture(job)
    profile=contents(result.bundle_path)["RESULT.json"]["profile"]
    assert result.capture_status == "PARTIAL" and result.failure_code == "CDP_CONNECT_FAILED"
    assert not Path(result.bundle_path,"STRUCTURE.json").exists()
    assert profile["stages"]["S0"]["status"] == profile["stages"]["S3"]["status"] == "COMPLETE"
    assert profile["stages"]["S1"]["status"] == profile["stages"]["S2"]["status"] == "NOT_RUN"
    assert verify_profile_bundle(result.bundle_path)["profile_valid"] is True and len(runner.calls)==1
    observe(request,expected={"status":"PARTIAL","reason":"CDP_CONNECT_FAILED","structure_retained":False,"runner_calls":1},
            actual={"status":result.capture_status,"reason":result.failure_code,"structure_retained":Path(result.bundle_path,"STRUCTURE.json").exists(),"runner_calls":len(runner.calls)},
            ac=("AC02","AC03","AC04"),cap=("CAP-04",))


def test_final_coordinator_permit_loss_discards_normal_worker_structure(tmp_path,request):
    finished=[False]
    def transform(response,ordinal):
        if finished[0]: response.update(allowed=False,reason_code="BINDING_REVOKED")
        return response
    runner=Runner(after=lambda result:finished.__setitem__(0,True))
    service,job,runner,spool_root,store=coordinator(tmp_path,runner=runner,permit=Permit(transform))
    result=service.capture(job)
    assert finished[0] and result.capture_status == "PARTIAL"
    assert not Path(result.bundle_path,"STRUCTURE.json").exists()
    assert verify_profile_bundle(result.bundle_path)["profile_valid"] is True
    observe(request,expected={"normal_worker_finished":True,"status":"PARTIAL","structure_retained":False},
            actual={"normal_worker_finished":finished[0],"status":result.capture_status,"structure_retained":Path(result.bundle_path,"STRUCTURE.json").exists(),"reason":result.failure_code},
            ac=("AC02","AC03"),cap=("CAP-02",))


def test_active_incident_exposes_protection_ids_then_releases_after_commit(tmp_path,request):
    seen=[]
    service=None
    def during(job,*,permit):
        assert service is not None
        seen.append(service.protected_event_ids())
        return run_capture_worker(job,permit=permit,connection_factory=lambda endpoint,timeout:Connection())
    service,job,runner,spool_root,store=coordinator(tmp_path,runner=during)
    assert not service.protected_event_ids()
    result=service.capture(job)
    event_id=job.to_dict()["trigger_envelope"]["event_id"]
    assert seen and event_id in seen[0]
    assert not service.protected_event_ids()
    observe(request,expected={"active_trigger_protected":True,"after_commit_active_count":0,"status":"COMPLETE"},
            actual={"active_trigger_protected":event_id in seen[0],"after_commit_active_count":len(service.protected_event_ids()),"status":result.capture_status},
            ac=("AC01","AC03"),cap=("CAP-01",))


def test_same_capture_returns_verified_cached_bundle_without_repeat(tmp_path,request):
    service,job,runner,spool_root,store=coordinator(tmp_path)
    first=service.capture(job)
    before=snapshot(Path(first.bundle_path))
    second=service.capture(job)
    assert second == first and len(runner.calls)==1
    assert before==snapshot(Path(first.bundle_path))
    changed=job.to_dict()
    changed["capture_request"]["max_nodes"] -= 1
    with pytest.raises(CaptureServiceError): service.capture(WorkerRequest.from_dict(changed))
    assert len(runner.calls)==1 and before==snapshot(Path(first.bundle_path))
    observe(request,expected={"cached_path_same":True,"bundle_unchanged":True,"runner_calls":1,"changed_job_rejected":True},
            actual={"cached_path_same":first.bundle_path==second.bundle_path,"bundle_unchanged":before==snapshot(Path(first.bundle_path)),"runner_calls":len(runner.calls),"changed_job_rejected":True},
            ac=("AC01","AC03","AC04"),cap=("CAP-01",))


def test_storage_failure_never_invents_bundle_and_keeps_active_s0(tmp_path,request,monkeypatch):
    service,job,runner,spool_root,store=coordinator(tmp_path)
    def refused(*args,**kwargs):
        raise OSError("SYNTHETIC_PRIVATE_STORAGE_PATH")
    monkeypatch.setattr(store,"commit",refused)
    with pytest.raises(CaptureServiceError) as error:
        service.capture(job)
    assert "SYNTHETIC_PRIVATE_STORAGE_PATH" not in str(error.value)
    assert not list((tmp_path/"bundles").glob("*/COMMIT.json"))
    assert job.to_dict()["trigger_envelope"]["event_id"] in service.protected_event_ids()
    assert (tmp_path/"state/incidents"/job.to_dict()["capture_id"]/"S0.json").is_file()
    observe(request,expected={"claimed_bundle":False,"active_trigger_protected":True,"s0_preserved":True},
            actual={"claimed_bundle":False,"active_trigger_protected":job.to_dict()["trigger_envelope"]["event_id"] in service.protected_event_ids(),
                    "s0_preserved":(tmp_path/"state/incidents"/job.to_dict()["capture_id"]/"S0.json").is_file(),"reason":error.value.code},
            ac=("AC03","AC04"),cap=("CAP-04","CAP-05"))


def test_guard_lost_at_last_publish_barrier_removes_provisional_structure(tmp_path,request):
    phases=[]
    def transform(response,ordinal):
        phases.append(response["phase"])
        if response["phase"]=="COORDINATOR_COMMIT":
            response.update(allowed=False,reason_code="BINDING_REVOKED")
        return response
    service,job,runner,spool_root,store=coordinator(tmp_path,permit=Permit(transform))
    with pytest.raises(CaptureServiceError) as error:
        service.capture(job)
    destination=store.root/job.to_dict()["capture_id"]
    assert error.value.code=="PUBLISH_GUARD_DENIED"
    assert "COORDINATOR_FINAL" in phases and phases[-1]=="COORDINATOR_COMMIT"
    assert not (destination/"COMMIT.json").exists() and not (destination/"STRUCTURE.json").exists()
    checked=verify_profile_bundle(destination)
    assert checked["publication_state"]=="INCOMPLETE" and checked["integrity_valid"] is False
    assert job.to_dict()["trigger_envelope"]["event_id"] in service.protected_event_ids()
    observe(request,expected={"published":False,"provisional_structure_retained":False,"active_trigger_protected":True,"runner_calls":1},
            actual={"published":(destination/"COMMIT.json").exists(),"provisional_structure_retained":(destination/"STRUCTURE.json").exists(),
                    "active_trigger_protected":job.to_dict()["trigger_envelope"]["event_id"] in service.protected_event_ids(),"runner_calls":len(runner.calls),"reason":error.value.code},
            ac=("AC02","AC03","AC04"),cap=("CAP-02","CAP-05"))


def test_recovered_s0_attempt_is_metadata_only_and_never_respawns(tmp_path,request,monkeypatch):
    service,job,runner,spool_root,store=coordinator(tmp_path)
    original_commit=store.commit
    def stopped_before_store(*args,**kwargs):
        raise OSError("SYNTHETIC_STORE_STOP")
    monkeypatch.setattr(store,"commit",stopped_before_store)
    with pytest.raises(CaptureServiceError): service.capture(job)
    assert len(runner.calls)==1
    monkeypatch.setattr(store,"commit",original_commit)
    second=CaptureCoordinator(tmp_path/"state",spool_root,store,runner=runner,permit=Permit(),utcnow=lambda:NOW)
    result=second.capture(job)
    assert result.capture_status=="PARTIAL" and result.failure_code=="SUPERVISOR_LOST"
    assert not Path(result.bundle_path,"STRUCTURE.json").exists() and len(runner.calls)==1
    assert verify_profile_bundle(result.bundle_path)["profile_valid"] is True
    assert second.last_capture_metadata["recovery_metadata_only"] is True
    assert second.last_capture_metadata["worker_attempts"]==0
    observe(request,expected={"status":"PARTIAL","reason":"SUPERVISOR_LOST","runner_calls_total":1,"recovery_worker_attempts":0,"structure_retained":False},
            actual={"status":result.capture_status,"reason":result.failure_code,"runner_calls_total":len(runner.calls),
                    "recovery_worker_attempts":second.last_capture_metadata["worker_attempts"],"structure_retained":Path(result.bundle_path,"STRUCTURE.json").exists()},
            ac=("AC01","AC03","AC04"),cap=("CAP-04",))


def test_cached_bundle_missing_commit_is_never_repaired_or_reexecuted(tmp_path,request):
    service,job,runner,spool_root,store=coordinator(tmp_path)
    result=service.capture(job)
    destination=Path(result.bundle_path)
    (destination/"COMMIT.json").unlink()
    before=snapshot(destination)
    with pytest.raises(CaptureServiceError) as error: service.capture(job)
    assert error.value.code=="INCOMPLETE_BUNDLE" and len(runner.calls)==1
    assert before==snapshot(destination)
    assert not verify_profile_bundle(destination)["integrity_valid"]
    observe(request,expected={"reason":"INCOMPLETE_BUNDLE","runner_calls":1,"incomplete_bytes_unchanged":True},
            actual={"reason":error.value.code,"runner_calls":len(runner.calls),"incomplete_bytes_unchanged":before==snapshot(destination)},
            ac=("AC03","AC04"),cap=("CAP-05",))


def test_accepted_l04_guard_supplies_diagnostic_permits_without_web_admission(tmp_path,request):
    from cdpd_chatgpt_profile.capture_service import L04CapturePermitAuthority
    from test_binding_guard import fixture_guard
    guard,authority,clock=fixture_guard()
    data=request_data()
    permit=L04CapturePermitAuthority(guard,supervisor_instance_id=data["supervisor_instance_id"])
    service,job,runner,spool_root,store=coordinator(tmp_path,data,permit=permit)
    result=service.capture(job)
    assert result.capture_status=="COMPLETE" and len(runner.calls)==1
    assert authority.calls==[] and guard.state=="BOUND"
    assert guard.record.to_bytes().decode()==job.to_dict()["binding_record_raw_json"]
    observe(request,expected={"status":"COMPLETE","guard_state":"BOUND","web_authority_changes":0,"binding_bytes_unchanged":True},
            actual={"status":result.capture_status,"guard_state":guard.state,"web_authority_changes":len(authority.calls),
                    "binding_bytes_unchanged":guard.record.to_bytes().decode()==job.to_dict()["binding_record_raw_json"]},
            ac=("AC01","AC02"),cap=("CAP-01",))


def test_accepted_l04_terminal_guard_is_not_revived_by_capture(tmp_path,request):
    from cdpd_chatgpt_profile.capture_service import L04CapturePermitAuthority
    from test_binding_guard import fixture_guard
    guard,authority,clock=fixture_guard()
    data=request_data()
    guard.invalidate("BINDING_REVOKED")
    before=guard.record.to_bytes()
    permit=L04CapturePermitAuthority(guard,supervisor_instance_id=data["supervisor_instance_id"])
    service,job,runner,spool_root,store=coordinator(tmp_path,data,permit=permit)
    result=service.capture(job)
    assert result.capture_status=="PARTIAL" and len(runner.calls)==0
    assert guard.state=="INVALIDATED" and guard.record.to_bytes()==before
    assert not Path(result.bundle_path,"STRUCTURE.json").exists()
    observe(request,expected={"status":"PARTIAL","runner_calls":0,"terminal_guard_preserved":True,"structure_retained":False},
            actual={"status":result.capture_status,"runner_calls":len(runner.calls),"terminal_guard_preserved":guard.state=="INVALIDATED",
                    "structure_retained":Path(result.bundle_path,"STRUCTURE.json").exists()},
            ac=("AC02","AC03"),cap=("CAP-02",))


def test_serialized_binding_cannot_instantiate_l04_live_permit_authority(request):
    from cdpd_chatgpt_profile.capture_service import L04CapturePermitAuthority
    from cdpd_chatgpt_profile.worker import WorkerInputError
    data=request_data()
    with pytest.raises(WorkerInputError):
        L04CapturePermitAuthority(json.loads(data["binding_record_raw_json"]),supervisor_instance_id=data["supervisor_instance_id"])
    observe(request,expected={"serialized_record_accepted_as_live_guard":False,"browser_operations":0},
            actual={"serialized_record_accepted_as_live_guard":False,"browser_operations":0},
            ac=("AC02",),cap=("CAP-02",))


def test_two_history_byte_cutoffs_count_proven_disjoint_omissions(tmp_path,request):
    """Actual accepted spool crop and later EVENTS wrapper crop omit different IDs."""
    from uuid import uuid4
    data=request_data()
    envelope=data["trigger_envelope"]
    correlation={"opaque_id_"+str(index):"x"*1024 for index in range(16)}
    envelope["event"]["correlation"]=correlation
    data["capture_request"]["event"]=deepcopy(envelope["event"])
    total=80
    earlier=[]
    for sequence in range(1,total):
        row=deepcopy(envelope)
        row.update(event_id=str(uuid4()),producer_seq=sequence)
        earlier.append(row)
    envelope.update(event_id=str(uuid4()),producer_seq=total)
    service,job,runner,spool_root,store=coordinator(tmp_path,data,earlier=tuple(earlier))
    inherited=read_history(spool_root,stream=stream_key(envelope),now_utc=NOW.timestamp())
    assert inherited["omitted_by_limit"]>0
    assert len(inherited["events"])+inherited["omitted_by_limit"]==total
    result=service.capture(job)
    payload=contents(result.bundle_path)
    profile=payload["RESULT.json"]["profile"]
    retained=len(payload["EVENTS.json"]["events"])
    extra_omitted=service.last_capture_metadata["wrapper_limit_omitted"]
    assert extra_omitted>0 and len(inherited["events"])-retained==extra_omitted
    omitted=inherited["omitted_by_limit"]+extra_omitted
    assert omitted==total-retained
    assert profile["history_coverage"]["loss_lower_bound"]==omitted
    assert payload["RESULT.json"]["history_dropped_count"]==omitted
    assert profile["history_coverage"]["complete"] is False and profile["history_coverage"]["sampled"] is True
    assert result.capture_status=="PARTIAL" and result.failure_code=="HISTORY_INCOMPLETE"
    assert Path(result.bundle_path,"EVENTS.json").stat().st_size<=1048576
    assert verify_profile_bundle(result.bundle_path)["profile_valid"] is True
    observe(request,expected={"both_cutoffs_occurred":True,"distinct_omissions_accounted":True,"history_complete":False,"status":"PARTIAL"},
            actual={"both_cutoffs_occurred":inherited["omitted_by_limit"]>0 and extra_omitted>0,
                    "distinct_omissions_accounted":profile["history_coverage"]["loss_lower_bound"]==total-retained,
                    "history_complete":profile["history_coverage"]["complete"],"status":result.capture_status,
                    "initial_events":total,"accepted_reader_omitted":inherited["omitted_by_limit"],
                    "wrapper_omitted":extra_omitted,"retained_events":retained,"loss_lower_bound":profile["history_coverage"]["loss_lower_bound"]},
            ac=("AC03",),cap=("CAP-01",))


@pytest.mark.parametrize("mode,status,failure",[("normal","COMPLETE",None),("capture_exception","PARTIAL","COLLECTOR_EXCEPTION")])
def test_actual_spool_child_worker_and_coordinator_commit_pipeline(tmp_path,request,monkeypatch,mode,status,failure):
    """Accepted spool -> real owned child -> parent store; child interval writes zero files."""
    from test_l06_process_worker import FixtureRunner
    root=Path(__file__).resolve().parents[1]
    paths=[str(root/"code"),str(root/"tests")]
    paths.extend(str(p) for p in (root/"packages").glob("*/src"))
    paths.extend(p for p in sys.path if p and ("python_deps" in p or "site-packages" in p))
    if os.environ.get("PYTHONPATH"): paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH",os.pathsep.join(paths))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE","1")
    child_cwd=tmp_path/"child_cwd"
    child_cwd.mkdir()
    monkeypatch.chdir(child_cwd)

    class ObservedFixtureRunner(FixtureRunner):
        def __init__(self):
            super().__init__(mode)
            self.child_interval_unchanged=False
        def __call__(self,job,*,permit):
            assert_parent_capture_lock_held(tmp_path)
            before=snapshot_child_invariance(tmp_path)
            value=super().__call__(job,permit=permit)
            assert_parent_capture_lock_held(tmp_path)
            self.child_interval_unchanged=before==snapshot_child_invariance(tmp_path)
            return value

    process=ObservedFixtureRunner()
    service,job,runner,spool_root,store=coordinator(tmp_path,runner=process)
    spool_before=snapshot(spool_root)
    outcome=service.capture(job)
    metadata=process.last_process_metadata
    child_result=service.last_worker_result
    verification=verify_profile_bundle(outcome.bundle_path)
    bundle=Path(outcome.bundle_path)
    assert outcome.capture_status==status and outcome.failure_code==failure
    assert verification["integrity_valid"] is True and verification["profile_valid"] is True
    assert (bundle/"COMMIT.json").is_file()
    assert child_result["pid"]==metadata["pid"] and child_result["pid"]!=os.getpid()
    assert metadata["child_exit_code"]==0 and metadata["spawn_count"]==1
    assert metadata["termination"]=="NORMAL" and metadata["failure_reason"]=="NONE"
    assert process.child_interval_unchanged and not list(child_cwd.iterdir())
    # Parent's lock file remains in place after the capture. Only the fixture
    # side-effect comparison omits it; production lock acquisition is intact.
    assert (tmp_path / "state/capture-writer.lock").read_bytes() == (b"0" if os.name == "nt" else b"")
    assert spool_before==snapshot(spool_root)
    assert (bundle/"STRUCTURE.json").exists()==(mode=="normal")
    expected={"status":status,"failure_code":failure,"real_child_distinct_pid":True,"child_interval_files_unchanged":True,
              "committed_profile_valid":True,"spool_unchanged":True,"spawn_count":1}
    actual={"status":outcome.capture_status,"failure_code":outcome.failure_code,
            "real_child_distinct_pid":child_result["pid"]==metadata["pid"] and child_result["pid"]!=os.getpid(),
            "child_interval_files_unchanged":process.child_interval_unchanged,
            "committed_profile_valid":verification["integrity_valid"] and verification["profile_valid"],
            "spool_unchanged":spool_before==snapshot(spool_root),"spawn_count":metadata["spawn_count"],
            "coordinator_pid":os.getpid(),"child_pid":metadata["pid"],"process":metadata,
            "structure_retained":(bundle/"STRUCTURE.json").exists()}
    observe(request,expected=expected,actual=actual,ac=("AC01","AC02","AC03","AC04"),
            cap=("CAP-01",) if mode=="normal" else ("CAP-04",),result=child_result)
    request.node._l06_evidence["commands"]=[metadata]
    request.node._l04_evidence["commands"]=[metadata]
    target=os.environ.get("CDPD_L06_EVIDENCE_DIR")
    if target:
        output=Path(target)/("actual_process_pipeline_"+mode)
        output.mkdir(parents=True,exist_ok=True)
        (output/"PROCESS_METADATA.json").write_text(json.dumps(metadata,indent=2)+"\n")
        (output/"BUNDLE_VERIFICATION.json").write_text(json.dumps(verification,indent=2)+"\n")
        shutil.copytree(bundle,output/bundle.name)
        shutil.copytree(tmp_path/"state/incidents"/job.to_dict()["capture_id"],output/"incident")
