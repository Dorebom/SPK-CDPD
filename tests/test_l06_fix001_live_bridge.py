"""FIX001 EXECUTION integration against the exact accepted live L03 bridge.

The accepted PreflightOwner, L03BindingBridge, BindingGuard and L06 Coordinator
are real classes. The existing accepted real_owner_runtime fixture supplies
synthetic clock/probe/transport inputs; no browser, OS profile, network or live
State is inspected or changed. This is not an independent Assessment.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from chatgpt_web_api.target_guard import GuardError
from cdpd_chatgpt_adapter.l03_binding import L03BindingBridge
from cdpd_chatgpt_profile.capture_service import CaptureCoordinator, L04CapturePermitAuthority
from cdpd_chatgpt_profile.evidence_store import ProfileEvidenceStore, verify_profile_bundle
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_capture_worker import request_data
from test_fix001_clock_terminal import real_owner_runtime, ledger
from test_l06_capture_service import spool_fixture, snapshot
from test_l06_fix001_authority import FIRST_REASON, challenge, notification, registered
from test_l06_fix001_e2e import OwnedRunner, RecordingBridge, isolated_child_environment, next_request


ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMES = (
    "accepted_l03/code/cdpd_chatgpt_profile/preflight.py",
    "packages/cdpd-chatgpt-adapter/src/cdpd_chatgpt_adapter/l03_binding.py",
    "packages/chatgpt-web-api/src/chatgpt_web_api/target_guard.py",
    "code/cdpd_chatgpt_profile/capture_service.py",
    "code/cdpd_chatgpt_profile/terminal_failure.py",
    "code/cdpd_chatgpt_profile/process_runner.py",
    "code/cdpd_chatgpt_profile/worker.py",
    "tests/test_fix001_clock_terminal.py",
    "tests/test_l06_fix001_live_bridge.py",
    "tests/l06_fix001_child_fixture.py",
)


def observe_live(request, *, expected, actual, rr, area=None, runner=None, permits=None):
    row = {
        "expected": expected, "actual": actual, "ac": ["AC02"], "rr": list(rr),
        "commands": [] if runner is None else [run["process"] for run in runner.runs],
        "role": "EXECUTION_INTERNAL_QA", "scope": "REAL_ACCEPTED_CLASSES_SYNTHETIC_INPUTS_ONLY",
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_NAMES},
    }
    request.node._l06_evidence = row
    output = os.environ.get("CDPD_L06_EVIDENCE_DIR")
    if not output:
        return
    directory = Path(output) / ("case_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "EXPECTED_ACTUAL.json").write_text(json.dumps(row, indent=2, allow_nan=False) + "\n")
    if runner is not None:
        (directory / "OWNED_PROCESS_RAW.json").write_text(json.dumps(runner.runs, indent=2, allow_nan=False) + "\n")
    if permits is not None:
        (directory / "PERMIT_LOG.json").write_text(json.dumps(permits, indent=2, allow_nan=False) + "\n")
    if area is not None:
        for name in ("state", "spool", "bundles"):
            source = area / name
            if source.is_dir():
                shutil.copytree(source, directory / name, dirs_exist_ok=True)


def bridge_state(env):
    return {
        "exact_accepted_type": type(env.bridge) is L03BindingBridge,
        "state": env.bridge.state, "reason": env.bridge.reason,
        "generation": env.bridge.generation,
        "record": env.bridge.current_record.to_dict(),
        "tombstones": dict(env.bridge._tombstones),
        "owner_state": env.owner.state,
        "owner_current_sha256": hashlib.sha256(env.owner.current_record.to_bytes()).hexdigest(),
        "explicit_preflight_calls": len(env.owner_calls), "synthetic_probe_calls": len(env.probes),
    }


@pytest.mark.parametrize("reported,mapped", [
    ("TARGET_BINDING_INVALID", "TARGET_BINDING_INVALID"),
    ("TARGET_ORIGIN_MISMATCH", "TARGET_ORIGIN_MISMATCH"),
    ("BROWSER_IDENTITY_MISMATCH", "BROWSER_IDENTITY_MISMATCH"),
    ("FRAME_ORIGIN_UNVERIFIED", "TARGET_ORIGIN_MISMATCH"),
    ("DISCONNECTED", "CDP_COMMAND_FAILED"),
    ("KNOWN_CDP_LOSS", "CDP_COMMAND_FAILED"),
])
def test_real_accepted_bridge_terminal_ack_and_peer_denial(reported, mapped, real_owner_runtime, request):
    """snapshot raising after terminal state is not mistaken for failed revocation."""
    env = real_owner_runtime
    assert type(env.bridge) is L03BindingBridge
    peer = env.bridge.create_guard()
    capture_bridge = L04CapturePermitAuthority(env.guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(capture_bridge, env.guard)
    before_ledger = ledger(env)
    before_owner = env.owner.current_record.to_bytes()
    before_dispatch = len(env.transport.primitive_commands)
    receipt = capture_bridge(notification(capture_bridge, env.guard, capture_id, reason=reported))
    assert receipt["status"] == "CONFIRMED" and receipt["shared_state"] == "REVOKED"
    assert receipt["reason_code"] == reported and receipt["local_state"] == "INVALIDATED"
    assert env.guard.reason == mapped and env.bridge.reason == mapped
    assert env.bridge.state == "INVALIDATED"
    with pytest.raises(GuardError) as raw_snapshot_error:
        env.bridge.snapshot()
    assert raw_snapshot_error.value.code == mapped
    with pytest.raises(GuardError):
        peer.check(mutation=True)
    with pytest.raises(GuardError):
        env.cached.ops.evaluate("() => true")
    denied = capture_bridge(challenge(capture_bridge, env.guard))
    assert denied["allowed"] is False
    assert peer.state == "INVALIDATED"
    assert len(env.owner_calls) == len(env.probes) == 1
    assert ledger(env) == before_ledger and env.owner.current_record.to_bytes() == before_owner
    assert len(env.transport.primitive_commands) == before_dispatch
    observe_live(request,
        expected={"notification": "CONFIRMED", "shared": "REVOKED", "raw_snapshot": "THROWS_AFTER_TERMINAL",
                  "peer_mutation": "DENIED", "next_capture": "DENIED", "preflight_calls": 1,
                  "automatic_retry": 0, "accepted_ledger_preserved": True},
        actual={"reported_code": reported, "mapped_guard_code": mapped, "receipt": receipt,
                "raw_snapshot_error": raw_snapshot_error.value.code, "bridge": bridge_state(env),
                "peer_state": peer.state, "next_capture": denied,
                "owner_record_unchanged": env.owner.current_record.to_bytes() == before_owner,
                "ledger_unchanged": ledger(env) == before_ledger,
                "cached_dispatch_delta": len(env.transport.primitive_commands) - before_dispatch},
        rr=("L06-RR-001", "L06-RR-002", "L06-RR-006", "L06-RR-008"))


@pytest.mark.parametrize("notice_before_replacement", [False, True])
def test_real_explicit_preflight_separates_late_old_notice_from_new_binding(
        notice_before_replacement, real_owner_runtime, request):
    """Both arrival orders preserve the new accepted Preflight generation."""
    env = real_owner_runtime
    old_guard = env.guard
    old_capture_bridge = L04CapturePermitAuthority(old_guard, supervisor_instance_id=str(uuid4()))
    old_capture = registered(old_capture_bridge, old_guard)
    old_notice = notification(old_capture_bridge, old_guard, old_capture)
    before_ledger = ledger(env)
    first_receipt = old_capture_bridge(old_notice) if notice_before_replacement else None
    if first_receipt is not None:
        assert first_receipt["status"] == "CONFIRMED"
    env.clock.advance(1)
    explicit_preflight = env.bridge.preflight(env.config, policy_allowed=True, work_settled=True)
    assert explicit_preflight["status"] == "PASS"
    new_guard = env.bridge.create_guard()
    assert new_guard.generation == 2 and new_guard.record.binding_id != old_guard.record.binding_id
    new_capture_bridge = L04CapturePermitAuthority(new_guard, supervisor_instance_id=str(uuid4()))
    registered(new_capture_bridge, new_guard)
    new_record = env.bridge.current_record.to_bytes()
    preserved_tombstone = env.bridge._tombstones[old_guard.record.binding_id]
    late_receipt = old_capture_bridge(dict(old_notice))
    if notice_before_replacement:
        assert late_receipt == first_receipt
    else:
        assert late_receipt["status"] == "UNCONFIRMED" and late_receipt["shared_state"] == "NOT_CURRENT"
    misrouted = new_capture_bridge(old_notice)
    assert misrouted["status"] == "REJECTED"
    assert env.bridge.state == "BOUND" and env.bridge.current_record.to_bytes() == new_record
    assert env.bridge._tombstones[old_guard.record.binding_id] == preserved_tombstone
    assert old_guard.state == "INVALIDATED"
    new_guard.check(mutation=True)
    assert new_capture_bridge(challenge(new_capture_bridge, new_guard))["allowed"] is True
    assert len(env.owner_calls) == len(env.probes) == 2 and ledger(env) == before_ledger
    observe_live(request,
        expected={"new_generation": 2, "new_id_required": True, "new_state": "BOUND",
                  "old_state": "INVALIDATED", "old_misroute": "REJECTED", "explicit_preflight_calls": 2,
                  "automatic_retry_or_retarget": 0, "accepted_ledger_preserved": True},
        actual={"notice_before_replacement": notice_before_replacement, "first_receipt": first_receipt,
                "late_receipt": late_receipt, "old_misroute": misrouted, "bridge": bridge_state(env),
                "old_state": old_guard.state, "new_state": new_guard.state,
                "old_binding_id": old_guard.record.binding_id, "new_binding_id": new_guard.record.binding_id,
                "new_record_unchanged": env.bridge.current_record.to_bytes() == new_record,
                "old_tombstone_preserved": env.bridge._tombstones[old_guard.record.binding_id] == preserved_tombstone,
                "ledger_unchanged": ledger(env) == before_ledger},
        rr=("L06-RR-003", "L06-RR-005", "L06-RR-008"))


@pytest.mark.parametrize("fault", ["no_effect", "snapshot_exception"])
def test_exact_bridge_without_terminal_state_never_receives_false_confirmation(
        fault, real_owner_runtime, monkeypatch, request):
    """Exact accepted type alone cannot turn a swallowed/no-op callback into ACK."""
    env = real_owner_runtime
    peer = env.bridge.create_guard()
    capture_bridge = L04CapturePermitAuthority(env.guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(capture_bridge, env.guard)
    original_invalidate = env.bridge.invalidate
    original_snapshot = env.bridge.snapshot
    attempted = []

    def no_effect(reason, *, binding_id=None, generation=None):
        attempted.append({"reason": reason, "binding_id": binding_id, "generation": generation})

    def unreadable():
        raise RuntimeError("SYNTHETIC_ACCEPTED_BRIDGE_SNAPSHOT_UNAVAILABLE")

    monkeypatch.setattr(env.bridge, "invalidate", no_effect)
    if fault == "snapshot_exception":
        monkeypatch.setattr(env.bridge, "snapshot", unreadable)
    notice = notification(capture_bridge, env.guard, capture_id)
    receipt = capture_bridge(notice)
    assert receipt["status"] == "UNCONFIRMED" and receipt["shared_state"] == "UNKNOWN"
    assert env.guard.state == "INVALIDATED" and env.bridge.state == "BOUND"
    assert env.bridge._tombstones == {} and len(attempted) == 1
    monkeypatch.setattr(env.bridge, "snapshot", original_snapshot)
    peer.check(mutation=True)
    assert peer.state == "BOUND"
    assert capture_bridge(challenge(capture_bridge, env.guard))["allowed"] is False
    assert capture_bridge(dict(notice)) == receipt and len(attempted) == 1
    observed = bridge_state(env)
    monkeypatch.setattr(env.bridge, "invalidate", original_invalidate)
    observe_live(request,
        expected={"notification": "UNCONFIRMED", "shared": "UNKNOWN", "bridge_state": "BOUND",
                  "local_guard": "INVALIDATED", "false_peer_denial_claim": False, "automatic_retry": 0},
        actual={"fault": fault, "receipt": receipt, "bridge": observed, "attempted": attempted,
                "peer_state": peer.state, "local_guard": env.guard.state,
                "fault_injection": "INSTANCE_CALLBACK_ONLY_ACCEPTED_SOURCE_BYTES_UNCHANGED"},
        rr=("L06-RR-001", "L06-RR-004", "L06-RR-008"))


def live_job_data(env, *, supervisor):
    data = request_data()
    raw = env.guard.record.to_bytes().decode()
    raw_hash = hashlib.sha256(raw.encode()).hexdigest()
    data["capture_request"]["target"] = env.guard.record.to_dict()["target"]
    data.update(binding_record_raw_json=raw, binding_record_sha256=raw_hash,
                control_generation=env.guard.generation, supervisor_instance_id=supervisor)
    data["trigger_envelope"].update(binding_id=env.guard.record.binding_id, binding_sha256=raw_hash,
                                    control_generation=env.guard.generation,
                                    occurred_at_utc=env.guard.record.issued_at_utc)
    return data


def live_coordinator(env, area):
    supervisor = str(uuid4())
    data = live_job_data(env, supervisor=supervisor)
    capture_bridge = RecordingBridge(env.guard, supervisor_instance_id=supervisor,
                                     binding_record_raw_json=data["binding_record_raw_json"])
    runner = OwnedRunner(area)
    store = ProfileEvidenceStore(area / "bundles")
    service = CaptureCoordinator(area / "state", area / "spool", store,
                                 runner=runner, permit=capture_bridge, utcnow=lambda: env.clock.sample().utc)
    return data, capture_bridge, runner, service


def live_capture(area, runner, service, data, *, mode):
    spool_fixture(area / "spool", data["trigger_envelope"])
    spool_before = snapshot(area / "spool")
    runner.mode = mode
    previous_runs = len(runner.runs)
    job = WorkerRequest.from_dict(data)
    outcome = service.capture(job)
    assert job.to_dict() == data and snapshot(area / "spool") == spool_before
    bundle = Path(outcome.bundle_path)
    verification = verify_profile_bundle(bundle)
    assert verification["integrity_valid"]
    result = {
        "mode": mode, "capture_id": data["capture_id"], "capture_status": outcome.capture_status,
        "failure_code": outcome.failure_code, "structure_retained": (bundle / "STRUCTURE.json").is_file(),
        "verification": verification, "worker_runs": len(runner.runs) - previous_runs,
        "coordinator_metadata": deepcopy(service.last_capture_metadata),
        "worker_result": deepcopy(service.last_worker_result),
        "immutable_job_preserved": True, "spool_unchanged": True,
    }
    for run in runner.runs[previous_runs:]:
        assert run["s0_verified_before_spawn"]
        assert run["process"]["spawn_count"] == 1
        assert run["process"]["child_exit_code"] == 0 and run["process"]["termination"] == "NORMAL"
        assert run["result"]["pid"] == run["process"]["pid"] != os.getpid()
    return result


@pytest.mark.parametrize("moment", ["before", "after"])
def test_owned_terminal_capture_confirms_real_bridge_and_blocks_next_request(
        moment, real_owner_runtime, tmp_path, request):
    """Owned child plus actual L03 bridge publishes metadata after proven revocation."""
    env = real_owner_runtime
    peer = env.bridge.create_guard()
    data, capture_bridge, runner, service = live_coordinator(env, tmp_path)
    before_ledger = ledger(env)
    first = live_capture(tmp_path, runner, service, data, mode="origin_" + moment)
    assert first["capture_status"] == "PARTIAL" and first["failure_code"] == "TARGET_ORIGIN_MISMATCH"
    assert first["structure_retained"] is False and first["worker_runs"] == 1
    notification_result = first["coordinator_metadata"]["terminal_notification"]
    assert notification_result["receipt"]["status"] == "CONFIRMED"
    assert notification_result["receipt"]["shared_state"] == "REVOKED"
    assert env.bridge.state == env.guard.state == "INVALIDATED"
    with pytest.raises(GuardError):
        peer.check(mutation=True)
    second = live_capture(tmp_path, runner, service, next_request(data), mode="normal")
    assert second["capture_status"] == "PARTIAL" and second["failure_code"] == "TARGET_ORIGIN_MISMATCH"
    assert second["structure_retained"] is False and second["worker_runs"] == 0
    assert second["worker_result"]["command_audit"] == []
    assert len(env.owner_calls) == len(env.probes) == 1 and ledger(env) == before_ledger
    observe_live(request,
        expected={"first_status": "PARTIAL", "first_dom": "DISCARDED", "notification": "CONFIRMED",
                  "shared": "REVOKED", "peer_mutation": "DENIED", "next_worker_spawns": 0,
                  "next_content_dispatch": 0, "explicit_preflight_calls": 1},
        actual={"moment": moment, "captures": [first, second], "bridge": bridge_state(env),
                "peer_state": peer.state, "ledger_unchanged": ledger(env) == before_ledger},
        rr=("L06-RR-001", "L06-RR-002", "L06-RR-007", "L06-RR-008"),
        area=tmp_path, runner=runner, permits=capture_bridge.permits)


def test_two_healthy_owned_captures_keep_real_live_binding_unchanged(real_owner_runtime, tmp_path, request):
    """A valid live L03 generation admits two distinct explicit diagnostic requests."""
    env = real_owner_runtime
    data, capture_bridge, runner, service = live_coordinator(env, tmp_path)
    original_record = env.bridge.current_record.to_bytes()
    before_ledger = ledger(env)
    first = live_capture(tmp_path, runner, service, data, mode="normal")
    second = live_capture(tmp_path, runner, service, next_request(data), mode="normal")
    assert all(row["capture_status"] == "COMPLETE" and row["structure_retained"] for row in (first, second))
    assert all(row["worker_runs"] == 1 for row in (first, second))
    assert len({run["process"]["pid"] for run in runner.runs}) == 2
    assert all(run["result"]["command_audit"].count("Runtime.evaluate") == 1 for run in runner.runs)
    assert env.guard.state == env.bridge.state == "BOUND" and env.bridge.generation == 1
    assert env.bridge.current_record.to_bytes() == original_record
    assert env.bridge._tombstones == {} and ledger(env) == before_ledger
    assert len(env.owner_calls) == len(env.probes) == 1
    assert not any(row["challenge"]["phase"].startswith("TERMINAL:") for row in capture_bridge.permits)
    observe_live(request,
        expected={"complete_requests": 2, "owned_children": 2, "same_generation": 1,
                  "binding_unchanged": True, "terminal_notifications": 0, "preflight_calls": 1,
                  "accepted_ledger_preserved": True},
        actual={"captures": [first, second], "bridge": bridge_state(env),
                "binding_unchanged": env.bridge.current_record.to_bytes() == original_record,
                "ledger_unchanged": ledger(env) == before_ledger},
        rr=("L06-RR-005", "L06-RR-007", "L06-RR-008"),
        area=tmp_path, runner=runner, permits=capture_bridge.permits)
