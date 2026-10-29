"""OBS-002: reproduce a bounded terminal-save failure without weakening L06.

These additive tests retain the actual accepted L03/L04 authority, actual L06
coordinator/worker/transport and synthetic socket fixture. They establish one
sufficient failure condition, not the unknown cause of the historical run.
"""
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
from uuid import uuid4

import pytest

from chatgpt_web_api.target_guard import GuardError
from cdpd_chatgpt_profile import capture_service
from cdpd_chatgpt_profile.capture_service import CaptureCoordinator, CaptureServiceError
from cdpd_chatgpt_profile.evidence_store import ProfileEvidenceStore, verify_profile_bundle
from cdpd_chatgpt_profile.worker import WorkerRequest
from l06_fix002_wire_fixture import raw_error
from test_fix001_clock_terminal import ledger, real_owner_runtime
from test_l06_capture_service import snapshot, spool_fixture
from test_l06_fix001_e2e import next_request
from test_l06_fix001_live_bridge import live_job_data
from test_l06_fix002_transport import RecordingAuthority, SynchronousRunner, prohibit_real_network


@pytest.mark.parametrize("save_boundary", ["normal", "terminal_notice", "terminal_receipt"])
def test_terminal_save_over_bound_stays_fail_closed(
        save_boundary, real_owner_runtime, tmp_path, monkeypatch, request):
    """Late durable evidence never grants a new mutation or substitutes a cause."""
    env = real_owner_runtime
    peer = env.bridge.create_guard()
    data = live_job_data(env, supervisor=str(uuid4()))
    record_before, ledger_before = env.bridge.current_record.to_bytes(), ledger(env)
    generation_before = env.bridge.generation
    dispatch_before = len(env.transport.primitive_commands)
    authority = RecordingAuthority(env.guard, supervisor_instance_id=data["supervisor_instance_id"],
                                   binding_record_raw_json=data["binding_record_raw_json"])
    runner = SynchronousRunner(tmp_path, {"error_factory": lambda: raw_error(errno.EPIPE),
                                        "point": "recv", "method": "Target.getTargetInfo", "occurrence": 3})
    service = CaptureCoordinator(tmp_path / "state", tmp_path / "spool",
                                 ProfileEvidenceStore(tmp_path / "bundles"), runner=runner,
                                 permit=authority, utcnow=lambda: env.clock.sample().utc)
    target = {"terminal_notice": "TERMINAL_NOTICE.json", "terminal_receipt": "TERMINAL_RECEIPT.json"}.get(save_boundary)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    observed = {"calls": 0, "hold_started": None, "hold_seconds": None}
    original_write = capture_service._write_new

    def controlled_write(path, raw):
        if Path(path).name != target:
            return original_write(path, raw)
        observed["calls"] += 1
        observed["hold_started"] = time.monotonic()
        entered.set()
        try:
            assert release.wait(2.0), "TEST_RELEASE_WAS_NOT_DELIVERED"
            observed["hold_seconds"] = time.monotonic() - observed["hold_started"]
            return original_write(path, raw)
        finally:
            completed.set()

    monkeypatch.setattr(capture_service, "_write_new", controlled_write)
    spool_fixture(tmp_path / "spool", data["trigger_envelope"])
    spool_before = snapshot(tmp_path / "spool")
    error, outcome = None, None
    started = time.monotonic()
    try:
        if target is None:
            outcome = service.capture(WorkerRequest.from_dict(data))
        else:
            with pytest.raises(CaptureServiceError) as caught:
                service.capture(WorkerRequest.from_dict(data))
            error = {"code": caught.value.code, "original_failure_code": caught.value.original_failure_code}
        metadata_at_return = deepcopy(service.last_capture_metadata)
        call_seconds = time.monotonic() - started
    finally:
        if target is not None:
            assert entered.is_set(), "requested persistence boundary was not reached"
            # The save remains pending until the coordinator has rejected it;
            # retain a >=250 ms hold to prove this exceeds the fixed 200 ms bound.
            remaining = max(0.0, 0.25 - (time.monotonic() - observed["hold_started"]))
            threading.Event().wait(remaining)
            release.set()
            assert completed.wait(2.0), "late save did not finish within fixture cleanup"

    assert snapshot(tmp_path / "spool") == spool_before
    assert len(runner.runs) == 1
    assert service.last_worker_result["failure_code"] == "DISCONNECTED"
    assert service.last_worker_result["structure"] is None
    with pytest.raises(GuardError):
        peer.check(mutation=True)
    assert env.bridge.state == "INVALIDATED"
    assert service.last_capture_metadata == metadata_at_return, "late storage may not upgrade the sealed result"
    metadata_after_late_save = deepcopy(service.last_capture_metadata)
    notice = service._terminal_bindings[service._terminal_key(data)]
    assert notice["notice"]["phase"] == "TERMINAL:DISCONNECTED"
    assert notice["automatic_recovery"] is False
    terminal_calls = [row for row in authority.calls if row["challenge"]["phase"].startswith("TERMINAL:")]
    assert len(terminal_calls) == 1
    assert terminal_calls[0]["response"]["status"] == "CONFIRMED"
    assert terminal_calls[0]["response"]["shared_state"] == "REVOKED"
    assert terminal_calls[0]["response"]["reason_code"] == "DISCONNECTED"
    s0 = tmp_path / "state/incidents" / data["capture_id"] / "S0.json"
    s0_before_second = s0.read_bytes()
    first_commit = tmp_path / "bundles" / data["capture_id"] / "COMMIT.json"
    if target is None:
        assert outcome.capture_status == "PARTIAL" and outcome.failure_code == "DISCONNECTED"
        assert first_commit.is_file()
        assert verify_profile_bundle(Path(outcome.bundle_path))["profile_valid"]
        assert metadata_at_return["terminal_notification"]["durable_latch"] is True
        assert notice["shared_invalidation"] == "CONFIRMED"
    else:
        assert error == {"code": "BINDING_INVALIDATION_UNCONFIRMED", "original_failure_code": "DISCONNECTED"}
        assert observed["calls"] == 1 and observed["hold_seconds"] >= 0.25
        assert not first_commit.exists()
        assert metadata_at_return["terminal_evidence_write_complete"] is False
        assert notice["shared_invalidation"] == "UNCONFIRMED"
    assert not list((tmp_path / "bundles").rglob("STRUCTURE.json"))

    next_data = next_request(data)
    spool_fixture(tmp_path / "spool", next_data["trigger_envelope"])
    if target is None:
        second = service.capture(WorkerRequest.from_dict(next_data))
        assert second.capture_status == "PARTIAL" and second.failure_code == "DISCONNECTED"
        next_result = {"status": second.capture_status, "failure_code": second.failure_code}
    else:
        with pytest.raises(CaptureServiceError) as caught_next:
            service.capture(WorkerRequest.from_dict(next_data))
        assert caught_next.value.code == "BINDING_INVALIDATION_UNCONFIRMED"
        assert caught_next.value.original_failure_code == "DISCONNECTED"
        next_result = {"code": caught_next.value.code, "original_failure_code": caught_next.value.original_failure_code}
    assert len(runner.runs) == 1 and s0.read_bytes() == s0_before_second
    assert env.bridge.current_record.to_bytes() == record_before
    assert env.bridge.generation == generation_before and ledger(env) == ledger_before
    assert len(env.owner_calls) == len(env.probes) == 1
    assert len(env.transport.primitive_commands) == dispatch_before
    assert not list((tmp_path / "bundles").rglob("STRUCTURE.json"))

    row = {"role": "EXECUTION_INTERNAL_QA", "observation_id": "L07-OBS-002",
           "expected": {"boundary": save_boundary, "persistence_bound_seconds": 0.20,
                        "injected_hold_minimum_seconds": 0.25 if target else 0,
                        "error": "BINDING_INVALIDATION_UNCONFIRMED" if target else None,
                        "first_cause": "DISCONNECTED", "peer_mutation_allowed": False,
                        "dom_structure_committed": False, "same_binding_next_worker_calls": 0},
           "actual": {"boundary": save_boundary, "error": error, "call_seconds": call_seconds,
                      "save_hold_seconds": observed["hold_seconds"],
                      "metadata_at_return": metadata_at_return,
                      "metadata_after_late_save": metadata_after_late_save,
                      "first_cause": notice["notice"]["phase"], "peer_mutation_allowed": False,
                      "shared_bridge_state": env.bridge.state, "coordinator_marker": deepcopy(notice),
                      "first_commit_present": first_commit.exists(), "dom_structure_committed": False,
                      "next_capture": next_result, "total_worker_calls": len(runner.runs),
                      "same_binding_next_worker_calls": 0, "terminal_calls": terminal_calls,
                      "binding_bytes_generation_ledger_unchanged": True, "s0_unchanged": True},
           "limits": {"historical_failure_cause_established": False,
                      "scope": "ACTUAL_ACCEPTED_CLASSES_SYNTHETIC_SOCKET_AND_RUNTIME_SAVE_DELAY",
                      "native_windows": False, "real_browser": False}}
    request.node._l07_evidence = row
    output = os.environ.get("CDPD_L07_EVIDENCE_DIR")
    if output:
        directory = Path(output) / ("transport_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "EXPECTED_ACTUAL.json").write_text(json.dumps(row, indent=2, allow_nan=False) + "\n")
        for name in ("state", "spool", "bundles"):
            if (tmp_path / name).exists():
                shutil.copytree(tmp_path / name, directory / name, dirs_exist_ok=True)
