"""FIX001 Execution QA: actual owned child -> shared L04 guard -> actual store.

These additive controls reproduce R1's eight before/after identity cases and
check terminal consequences on a peer and on a second explicit request. They
are internal Execution tests, not an independent Assessment or acceptance.
Only external browser responses are synthetic. Accepted L04/L05 code and all
original Candidate tests remain unmodified.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
from uuid import uuid4

import pytest

from chatgpt_web_api.target_guard import AuthoritySnapshot, BindingGuard, GuardError, ParsedBinding
from cdpd_chatgpt_profile.capture_service import (CaptureCoordinator, CaptureServiceError,
                                                L04CapturePermitAuthority)
from cdpd_chatgpt_profile.evidence_store import ProfileEvidenceStore, verify_profile_bundle
from cdpd_chatgpt_profile.process_runner import ProcessWorkerRunner
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_capture_worker import Permit, observe, request_data
from test_l06_capture_service import NOW, snapshot, spool_fixture
from test_l06_permit_authority import Authority, Clock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/l06_fix001_child_fixture.py"
IDENTITY_REASONS = {
    "pages": "TARGET_BINDING_INVALID",
    "origin": "TARGET_ORIGIN_MISMATCH",
    "epoch": "BROWSER_IDENTITY_MISMATCH",
    "target": "TARGET_BINDING_INVALID",
}


@pytest.fixture(autouse=True)
def isolated_child_environment(monkeypatch):
    paths = [str(ROOT / "code"), str(ROOT / "tests")]
    paths.extend(str(p) for p in sorted((ROOT / "packages").glob("*/src")))
    paths.extend(p for p in sys.path if p and ("python_deps" in p or "site-packages" in p))
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("CDPD_TEST_BROWSER", "0")

    def denied(*args, **kwargs):
        raise AssertionError("L06_FIX001_E2E_NETWORK_PROHIBITED")

    for name in ("connect", "connect_ex", "bind", "listen", "accept", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "create_server", denied)


class RecordingBridge(L04CapturePermitAuthority):
    """Keep all inherited failure-propagation methods on the actual adapter."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.permits = []

    def __call__(self, challenge):
        response = super().__call__(challenge)
        self.permits.append({"challenge": deepcopy(challenge), "response": deepcopy(response)})
        return response


class OwnedRunner(ProcessWorkerRunner):
    def __init__(self, area):
        super().__init__()
        self.area = area
        self.mode = "normal"
        self.runs = []

    def _command(self):
        return [sys.executable, "-B", str(FIXTURE), self.mode]

    def __call__(self, job, *, permit):
        data = job.to_dict()
        incident = self.area / "state/incidents" / data["capture_id"]
        s0 = json.loads((incident / "S0.json").read_bytes())
        history = (incident / "HISTORY.json").read_bytes()
        assert s0["job"] == data and s0["job_sha256"] == hashlib.sha256(job._raw).hexdigest()
        assert s0["history_sha256"] == hashlib.sha256(history).hexdigest()
        assert (incident / "attempt.json").is_file()
        result = super().__call__(job, permit=permit)
        self.runs.append({"mode": self.mode, "job": data, "s0_verified_before_spawn": True,
                          "result": deepcopy(result), "process": deepcopy(self.last_process_metadata)})
        assert job.to_dict() == data
        return result


class Harness:
    def __init__(self, area, *, data=None):
        self.area = area
        self.data = request_data() if data is None else deepcopy(data)
        self.record = ParsedBinding.parse(self.data["binding_record_raw_json"])
        self.clock = Clock()
        self.authority = Authority(self.record, self.clock)
        self.guard = BindingGuard(self.record, self.authority, clock=self.clock)
        self.peer = BindingGuard(self.record, self.authority, clock=self.clock)
        bridge = RecordingBridge(self.guard, supervisor_instance_id=self.data["supervisor_instance_id"],
                                 binding_record_raw_json=self.data["binding_record_raw_json"])
        self.bridges = [bridge]
        self.runner = OwnedRunner(area)
        self.store = ProfileEvidenceStore(area / "bundles")
        self.service = CaptureCoordinator(area / "state", area / "spool", self.store,
                                          runner=self.runner, permit=bridge, utcnow=lambda: NOW)
        self.observations = []
        self.jobs = []

    def capture(self, *, mode="normal", data=None, expected_error=None):
        data = deepcopy(self.data if data is None else data)
        job = WorkerRequest.from_dict(data)
        self.jobs.append(data)
        spool_fixture(self.area / "spool", data["trigger_envelope"])
        spool_before = snapshot(self.area / "spool")
        self.runner.mode = mode
        prior_runs = len(self.runner.runs)
        row = {"mode": mode, "capture_id": data["capture_id"], "binding_id": data["trigger_envelope"]["binding_id"],
               "control_generation": data["control_generation"], "expected_error": expected_error}
        try:
            outcome = self.service.capture(job)
            row.update(capture_status=outcome.capture_status, failure_code=outcome.failure_code,
                       bundle_path=outcome.bundle_path,
                       structure_retained=Path(outcome.bundle_path, "STRUCTURE.json").is_file(),
                       verification=verify_profile_bundle(outcome.bundle_path), exception_code=None,
                       original_failure_code=None)
        except CaptureServiceError as error:
            row.update(capture_status=None, failure_code=None, bundle_path=None,
                       structure_retained=False, verification=None, exception_code=error.code,
                       original_failure_code=getattr(error, "original_failure_code", None))
            if expected_error is None:
                raise
        row.update(guard_state=self.guard.state, guard_reason=self.guard.reason,
                   authority_invalidations=deepcopy(self.authority.invalidations),
                   authority_snapshot=asdict(self.authority.snapshot()),
                   worker_result=deepcopy(self.service.last_worker_result),
                   coordinator_metadata=deepcopy(self.service.last_capture_metadata),
                   worker_runs_for_request=len(self.runner.runs) - prior_runs,
                   worker_runs_total=len(self.runner.runs),
                   spool_unchanged=spool_before == snapshot(self.area / "spool"),
                   immutable_job_preserved=job.to_dict() == data)
        self.observations.append(row)
        assert row["spool_unchanged"] and row["immutable_job_preserved"]
        if expected_error is not None:
            assert row["exception_code"] == expected_error
        return row

    def peer_check(self, peer=None):
        peer = self.peer if peer is None else peer
        try:
            peer.check(mutation=True)
            return {"allowed": True, "reason": None, "state": peer.state}
        except GuardError as error:
            return {"allowed": False, "reason": error.code, "state": peer.state}


def next_request(data, *, policy=None):
    value = deepcopy(data)
    value["capture_id"] = "C-" + uuid4().hex
    value["trigger_envelope"]["event_id"] = str(uuid4())
    value["trigger_envelope"]["producer_seq"] += 1
    if policy is not None:
        value["policy_allowed"] = policy
    return value


def record(request, harness, *, expected, actual):
    observations = {
        **actual, "captures": harness.observations,
        "commands": [run["process"] for run in harness.runner.runs],
        "role": "EXECUTION_INTERNAL_QA", "authority": "CANDIDATE_ONLY",
        "browser_network_operations": 0, "web_mutation_resend_operations": len(harness.authority.operation_calls),
        "real_browser_acceptance": "NOT_RUN",
    }
    observe(request, expected=expected, actual=observations,
            ac=("AC01", "AC02", "AC03", "AC04"), cap=("CAP-01", "CAP-02", "CAP-03", "CAP-04", "CAP-05"))
    commands = [run["process"] for run in harness.runner.runs]
    for key in ("_l06_evidence", "_l04_evidence"):
        getattr(request.node, key)["commands"] = commands
    destination = os.environ.get("CDPD_L06_EVIDENCE_DIR")
    if destination:
        area = Path(destination) / ("case_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
        area.mkdir(parents=True, exist_ok=True)
        source_paths = [Path(__file__), FIXTURE,
                        ROOT / "code/cdpd_chatgpt_profile/worker.py",
                        ROOT / "code/cdpd_chatgpt_profile/worker_cli.py",
                        ROOT / "code/cdpd_chatgpt_profile/process_runner.py",
                        ROOT / "code/cdpd_chatgpt_profile/capture_service.py",
                        ROOT / "code/cdpd_chatgpt_profile/terminal_failure.py",
                        ROOT / "code/cdpd_chatgpt_profile/evidence_store.py",
                        ROOT / "packages/chatgpt-web-api/src/chatgpt_web_api/target_guard.py"]
        values = {
            "EXPECTED_ACTUAL.json": {"nodeid": request.node.nodeid, **request.node._l06_evidence},
            "JOBS.json": harness.jobs,
            "OWNED_PROCESS_RAW.json": harness.runner.runs,
            "PERMIT_LOG.json": [bridge.permits for bridge in harness.bridges],
            "AUTHORITY_LOG.json": {"invalidations": harness.authority.invalidations,
                                   "operation_calls": harness.authority.operation_calls,
                                   "snapshot": asdict(harness.authority.snapshot())},
            "SOURCE_PINS.json": {path.relative_to(ROOT).as_posix(): {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                for path in source_paths},
        }
        for name, value in values.items():
            (area / name).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        for name in ("state", "spool", "bundles"):
            if (harness.area / name).is_dir():
                shutil.copytree(harness.area / name, area / name, dirs_exist_ok=True)


def assert_owned_one_shot(harness):
    pids = []
    for run in harness.runner.runs:
        meta = run["process"]
        pids.append(meta["pid"])
        assert meta["spawn_count"] == 1
        assert meta["pid"] == run["result"]["pid"] and meta["pid"] != os.getpid()
        assert meta["child_exit_code"] == 0 and meta["termination"] == "NORMAL", meta
        assert run["s0_verified_before_spawn"]
        assert run["result"]["command_audit"].count("Runtime.evaluate") <= 1
    assert len(set(pids)) == len(pids)
    assert not harness.authority.operation_calls


@pytest.mark.parametrize("kind", ["pages", "origin", "epoch", "target"])
@pytest.mark.parametrize("moment", ["before", "after"])
def test_r1_identity_violation_latches_shared_binding_before_next_explicit_capture(tmp_path, request, kind, moment):
    harness = Harness(tmp_path)
    first = harness.capture(mode=kind + "_" + moment)
    peer = harness.peer_check()
    second_data = next_request(harness.data)
    second = harness.capture(data=second_data)
    record(request, harness,
           expected={"first_reason": IDENTITY_REASONS[kind], "first_guard_state": "INVALIDATED",
                     "peer_allowed": False, "same_binding_next_dom": False, "total_worker_spawns": 1},
           actual={"peer_after_first": peer, "same_binding_id": harness.data["trigger_envelope"]["binding_id"] == second_data["trigger_envelope"]["binding_id"],
                   "same_generation": harness.data["control_generation"] == second_data["control_generation"],
                   "separate_explicit_capture": harness.data["capture_id"] != second_data["capture_id"],
                   "separate_explicit_event": harness.data["trigger_envelope"]["event_id"] != second_data["trigger_envelope"]["event_id"]})
    assert first["capture_status"] == "PARTIAL" and first["failure_code"] == IDENTITY_REASONS[kind]
    assert not first["structure_retained"] and first["worker_result"]["structure"] is None
    assert first["worker_result"]["command_audit"].count("Runtime.evaluate") == (moment == "after")
    assert first["guard_state"] == "INVALIDATED" and first["authority_snapshot"]["revoked"]
    assert first["authority_invalidations"] and not peer["allowed"]
    assert second["capture_status"] == "PARTIAL" and not second["structure_retained"]
    assert second["worker_result"]["structure"] is None and not second["worker_result"]["command_audit"]
    assert second["worker_runs_for_request"] == 0 and len(harness.runner.runs) == 1
    assert harness.guard.state == "INVALIDATED"
    assert all(row["verification"]["integrity_valid"] and row["verification"]["profile_valid"]
               for row in (first, second))
    assert_owned_one_shot(harness)


@pytest.mark.parametrize("fault,process_reason", [
    ("result_wrong_capture", "RESULT_INVALID"),
    ("terminal_duplicate_notice", "PERMIT_INVALID"),
    ("result_then_nonzero", "CHILD_EXITED_ERROR"),
    ("result_then_hang", "DEADLINE"),
])
def test_authenticated_terminal_notice_survives_later_owned_child_failure(tmp_path, request, fault, process_reason):
    harness = Harness(tmp_path, data=request_data(timeout=2.0 if fault == "result_then_hang" else 10.0))
    first = harness.capture(mode="pages_after+" + fault)
    peer = harness.peer_check()
    second = harness.capture(data=next_request(harness.data))
    process = harness.runner.runs[0]["process"]
    record(request, harness,
           expected={"first_reason": "TARGET_BINDING_INVALID", "process_reason": process_reason,
                     "guard_state": "INVALIDATED", "peer_allowed": False,
                     "second_dom": False, "total_worker_spawns": 1},
           actual={"peer_after_first": peer, "fault_after_authenticated_notice": fault})
    assert first["capture_status"] == "PARTIAL" and first["failure_code"] == "TARGET_BINDING_INVALID"
    assert not first["structure_retained"] and first["worker_result"]["structure"] is None
    assert first["guard_state"] == "INVALIDATED" and not peer["allowed"]
    assert process["failure_reason"] == process_reason
    assert process["terminal_notice"]["phase"] == "TERMINAL:TARGET_BINDING_INVALID"
    assert process["terminal_receipt"]["status"] == "CONFIRMED"
    assert process["pid"] == first["worker_result"]["pid"] and process["pid"] != os.getpid()
    assert process["spawn_count"] == 1 and process["child_exit_code"] is not None
    assert process["termination"] in {"KILLED", "EXITED_ERROR"}
    if fault == "result_then_hang":
        assert process["timed_out"] and process["termination"] == "KILLED"
        assert process["elapsed_ms"] < 3500
    if fault == "result_then_nonzero":
        assert process["child_exit_code"] == 73
    assert not second["structure_retained"] and second["worker_runs_for_request"] == 0
    assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls
    assert first["verification"]["profile_valid"] and first["verification"]["integrity_valid"]
    assert "SYNTHETIC_PRIVATE_CHILD_ERROR" not in json.dumps(harness.observations)


@pytest.mark.parametrize("fault", [
    "terminal_wrong_capture", "terminal_wrong_binding", "terminal_wrong_hash",
    "terminal_wrong_generation", "terminal_generation_bool", "terminal_wrong_supervisor",
    "terminal_duplicate_nonce",
])
def test_child_notice_with_wrong_scope_or_replayed_nonce_cannot_revoke_authority(tmp_path, request, fault):
    harness = Harness(tmp_path)
    first = harness.capture(mode="pages_after+" + fault)
    peer = harness.peer_check()
    second = harness.capture(data=next_request(harness.data), expected_error="BINDING_INVALIDATION_UNCONFIRMED")
    process = harness.runner.runs[0]["process"]
    terminal_callbacks = [row for bridge in harness.bridges for row in bridge.permits
                          if row["challenge"]["phase"].startswith("TERMINAL:")]
    record(request, harness,
           expected={"invalid_notice_reaches_authority": False, "process_reason": "PERMIT_INVALID",
                     "first_dom": False, "false_shared_invalidation": False,
                     "next_explicit_same_binding_error": "BINDING_INVALIDATION_UNCONFIRMED",
                     "next_child_spawns": 0},
           actual={"fault": fault, "terminal_callback_count": len(terminal_callbacks),
                   "peer_after_rejected_untrusted_notice": peer})
    assert first["capture_status"] == "PARTIAL" and not first["structure_retained"]
    assert first["failure_code"] == "WORKER_FAILED_OR_DEADLINE"
    assert process["failure_reason"] == "PERMIT_INVALID" and "terminal_notice" not in process
    assert process["child_exit_code"] is not None and process["termination"] in {"KILLED", "EXITED_ERROR"}
    assert not terminal_callbacks and not harness.authority.invalidations
    assert first["guard_state"] == "BOUND" and peer["allowed"]
    assert second["exception_code"] == "BINDING_INVALIDATION_UNCONFIRMED"
    assert second["original_failure_code"] is None and not second["structure_retained"]
    assert second["worker_runs_for_request"] == 0
    assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls


def restarted_coordinator(harness, *, permit=None):
    """Fresh coordinator object reads durable evidence, not its old Python map."""
    harness.service = CaptureCoordinator(harness.area / "state", harness.area / "spool", harness.store,
                                          runner=harness.runner,
                                          permit=harness.bridges[0] if permit is None else permit,
                                          utcnow=lambda: NOW)


def test_valid_terminal_result_without_notice_keeps_original_cause_and_unknown_latch(tmp_path, request):
    harness = Harness(tmp_path)
    first = harness.capture(mode="pages_after+no_notice", expected_error="BINDING_INVALIDATION_UNCONFIRMED")
    incident = tmp_path / "state/incidents" / harness.data["capture_id"]
    terminal_callbacks = [row for bridge in harness.bridges for row in bridge.permits
                          if row["challenge"]["phase"].startswith("TERMINAL:")]
    # Deliberately permissive test callback proves restart refusal comes from
    # durable local evidence and does not rely on a retained guard instance.
    after_restart_permit = Permit()
    restarted_coordinator(harness, permit=after_restart_permit)
    second = harness.capture(data=next_request(harness.data), expected_error="BINDING_INVALIDATION_UNCONFIRMED")
    process = harness.runner.runs[0]["process"]
    record(request, harness,
           expected={"caller_error": "BINDING_INVALIDATION_UNCONFIRMED", "original_failure": "TARGET_BINDING_INVALID",
                     "shared_notification_retries": 0, "false_shared_confirmation": False,
                     "restart_child_spawns": 0, "commits": 0},
           actual={"terminal_callback_count": len(terminal_callbacks),
                   "restart_permit_calls": len(after_restart_permit.challenges),
                   "original_s0_preserved": (incident / "S0.json").is_file(),
                   "guard_is_still_bound_but_not_assumed_safe": harness.guard.state == "BOUND"})
    assert first["original_failure_code"] == second["original_failure_code"] == "TARGET_BINDING_INVALID"
    assert first["worker_result"]["failure_code"] == "TARGET_BINDING_INVALID"
    assert process["child_exit_code"] == 0 and process["termination"] == "NORMAL"
    assert "terminal_notice" not in process and not terminal_callbacks
    assert not harness.authority.invalidations and harness.guard.state == "BOUND"
    assert (incident / "S0.json").is_file() and (incident / "attempt.json").is_file()
    assert not list((tmp_path / "bundles").glob("*/COMMIT.json"))
    assert second["worker_runs_for_request"] == 0 and not after_restart_permit.challenges
    assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls


@pytest.mark.parametrize("fault", ["no_notice_abrupt_death", "no_notice_hang"])
def test_lost_child_without_authenticated_cause_remains_unknown_after_restart(tmp_path, request, fault):
    harness = Harness(tmp_path, data=request_data(timeout=2.0 if fault == "no_notice_hang" else 10.0))
    first = harness.capture(mode="pages_after+" + fault)
    process = harness.runner.runs[0]["process"]
    terminal_callbacks = [row for bridge in harness.bridges for row in bridge.permits
                          if row["challenge"]["phase"].startswith("TERMINAL:")]
    first_bundle = Path(first["bundle_path"])
    before_cached = snapshot(first_bundle)
    cached = harness.service.capture(WorkerRequest.from_dict(harness.data))
    cache_preserved = before_cached == snapshot(first_bundle)
    cached_runs = len(harness.runner.runs)
    after_restart_permit = Permit()
    restarted_coordinator(harness, permit=after_restart_permit)
    second = harness.capture(data=next_request(harness.data), expected_error="BINDING_INVALIDATION_UNCONFIRMED")
    record(request, harness,
           expected={"first_status": "PARTIAL", "first_reason": "WORKER_FAILED_OR_DEADLINE",
                     "invented_terminal_cause": False, "claimed_shared_revocation": False,
                     "cached_result_is_historical_only": True,
                     "next_same_binding_error": "BINDING_INVALIDATION_UNCONFIRMED",
                     "next_child_spawns": 0},
           actual={"fault": fault, "terminal_callback_count": len(terminal_callbacks),
                   "cached_bundle_path_same": cached.bundle_path == first["bundle_path"],
                   "cached_bundle_unchanged": cache_preserved,
                   "worker_runs_after_cached_read": cached_runs,
                   "restart_permit_calls": len(after_restart_permit.challenges),
                   "first_exact_identity_cause_available_to_parent": False})
    assert first["capture_status"] == "PARTIAL" and first["failure_code"] == "WORKER_FAILED_OR_DEADLINE"
    assert first["worker_result"]["failure_code"] == "WORKER_FAILED_OR_DEADLINE"
    assert not first["structure_retained"] and first["worker_result"]["structure"] is None
    assert first["verification"]["profile_valid"] and first["verification"]["integrity_valid"]
    assert "terminal_notice" not in process and "terminal_receipt" not in process
    assert not terminal_callbacks and not harness.authority.invalidations
    assert harness.guard.state == "BOUND" and not harness.authority.snapshot().revoked
    assert process["spawn_count"] == 1 and process["child_exit_code"] is not None
    assert process["pid"] == first["worker_result"]["pid"] and process["pid"] != os.getpid()
    assert process["termination"] in {"KILLED", "EXITED_ERROR"}
    if fault == "no_notice_hang":
        assert process["failure_reason"] == "DEADLINE" and process["timed_out"]
        assert process["termination"] == "KILLED" and process["elapsed_ms"] < 3500
    else:
        assert process["failure_reason"] == "MISSING_RESULT" and process["child_exit_code"] == 74
    assert cached.bundle_path == first["bundle_path"] and cached.failure_code == "WORKER_FAILED_OR_DEADLINE"
    assert cache_preserved and cached_runs == 1
    assert second["exception_code"] == "BINDING_INVALIDATION_UNCONFIRMED"
    assert second["original_failure_code"] is None and second["worker_runs_for_request"] == 0
    assert not second["structure_retained"] and not after_restart_permit.challenges
    assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls


@pytest.mark.parametrize("fault", ["raise_before_guard", "invalid_receipt", "slow_before_guard",
                                  "shared_invalidate_raise", "shared_invalidate_noop"])
def test_unconfirmed_notification_is_visible_bounded_and_sticky_after_restart(tmp_path, request, monkeypatch, fault):
    harness = Harness(tmp_path)
    bridge = harness.bridges[0]
    callback_trace = []
    release = threading.Event()
    callback_done = threading.Event()

    if fault.startswith("shared_invalidate_"):
        def refused_shared(reason, *, binding_id=None, generation=None):
            harness.authority.invalidations.append((reason, binding_id, generation))
            callback_trace.append({"fault": fault, "scoped_binding_id": binding_id,
                                   "scoped_generation": generation})
            if fault == "shared_invalidate_raise":
                raise RuntimeError("SYNTHETIC_PRIVATE_SHARED_NOTIFICATION_FAILURE")
        monkeypatch.setattr(harness.authority, "invalidate", refused_shared)
    else:
        def injected(challenge):
            if not challenge["phase"].startswith("TERMINAL:"):
                return bridge(challenge)
            callback_trace.append({"fault": fault, "phase": challenge["phase"]})
            if fault == "raise_before_guard":
                raise RuntimeError("SYNTHETIC_PRIVATE_NOTIFICATION_FAILURE")
            if fault == "slow_before_guard":
                release.wait(5)
                receipt = bridge(challenge)
                callback_done.set()
                return receipt
            receipt = bridge(challenge)
            corrupted = dict(receipt, nonce="0" * 32)
            callback_trace.append({"delivered_receipt": corrupted})
            return corrupted
        harness.service.permit = injected

    started = time.monotonic()
    try:
        first = harness.capture(mode="pages_after", expected_error="BINDING_INVALIDATION_UNCONFIRMED")
        elapsed = time.monotonic() - started
        incident = tmp_path / "state/incidents" / harness.data["capture_id"]
        receipt_before = (incident / "TERMINAL_RECEIPT.json").read_bytes()
        saved = json.loads(receipt_before)
        if fault == "slow_before_guard":
            release.set()
            assert callback_done.wait(3), "bounded caller returned but synthetic late callback never finished"
        receipt_after = (incident / "TERMINAL_RECEIPT.json").read_bytes()
        peer = harness.peer_check()
        after_restart_permit = Permit()
        restarted_coordinator(harness, permit=after_restart_permit)
        second = harness.capture(data=next_request(harness.data), expected_error="BINDING_INVALIDATION_UNCONFIRMED")
        record(request, harness,
               expected={"caller_error": "BINDING_INVALIDATION_UNCONFIRMED", "original_failure": "TARGET_BINDING_INVALID",
                         "saved_receipt_status": "UNCONFIRMED", "automatic_recovery": False,
                         "late_callback_cannot_change_saved_receipt": True, "commits": 0,
                         "restart_child_spawns": 0},
               actual={"fault": fault, "elapsed_seconds": elapsed, "callback_trace": callback_trace,
                       "saved_notification": saved, "receipt_unchanged_after_late_callback": receipt_before == receipt_after,
                       "observed_peer_after_notification_failure": peer,
                       "restart_permit_calls": len(after_restart_permit.challenges)})
        assert first["original_failure_code"] == second["original_failure_code"] == "TARGET_BINDING_INVALID"
        assert first["worker_result"]["failure_code"] == "TARGET_BINDING_INVALID"
        assert elapsed < 3.5
        assert saved["receipt"]["status"] == "UNCONFIRMED" and saved["automatic_recovery"] is False
        assert receipt_before == receipt_after
        assert (incident / "S0.json").is_file() and (incident / "TERMINAL_WORKER_RESULT.json").is_file()
        assert not list((tmp_path / "bundles").glob("*/COMMIT.json"))
        assert second["worker_runs_for_request"] == 0 and not after_restart_permit.challenges
        assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls
        assert "SYNTHETIC_PRIVATE" not in json.dumps(harness.observations)
    finally:
        release.set()


@pytest.mark.parametrize("fault", ["marker", "receipt", "all_terminal_evidence"])
def test_terminal_evidence_write_fault_cannot_reenable_capture_after_coordinator_restart(tmp_path, request, monkeypatch, fault):
    import cdpd_chatgpt_profile.capture_service as service_module

    harness = Harness(tmp_path)
    original_write = service_module._write_new
    failed_paths = []

    def denied_terminal(path, content):
        path = Path(path)
        marker = path.parent.name == "terminal-bindings"
        terminal_artifact = path.name.startswith("TERMINAL_")
        should_fail = ((fault == "marker" and marker)
                       or (fault == "receipt" and path.name == "TERMINAL_RECEIPT.json")
                       or (fault == "all_terminal_evidence" and (marker or terminal_artifact)))
        if should_fail:
            failed_paths.append(path.relative_to(tmp_path).as_posix())
            raise OSError("SYNTHETIC_PRIVATE_TERMINAL_STORAGE_FAILURE")
        return original_write(path, content)

    monkeypatch.setattr(service_module, "_write_new", denied_terminal)
    first = harness.capture(mode="pages_after", expected_error="BINDING_INVALIDATION_UNCONFIRMED")
    first_peer = harness.peer_check()
    monkeypatch.setattr(service_module, "_write_new", original_write)
    after_restart_permit = Permit()
    restarted_coordinator(harness, permit=after_restart_permit)
    second = harness.capture(data=next_request(harness.data), expected_error="BINDING_INVALIDATION_UNCONFIRMED")
    incident = tmp_path / "state/incidents" / harness.data["capture_id"]
    record(request, harness,
           expected={"original_caller_error": "BINDING_INVALIDATION_UNCONFIRMED",
                     "original_failure": "TARGET_BINDING_INVALID", "restart_caller_error": "BINDING_INVALIDATION_UNCONFIRMED",
                     "s0_and_attempt_preserved": True, "restart_permit_calls": 0,
                     "restart_child_spawns": 0, "commits": 0},
           actual={"fault": fault, "failed_paths": failed_paths, "peer_after_first": first_peer,
                   "restart_permit_calls": len(after_restart_permit.challenges),
                   "s0_and_attempt_preserved": (incident / "S0.json").is_file() and (incident / "attempt.json").is_file(),
                   "first_cause_survives_in_caller_observation": first["original_failure_code"]})
    assert first["original_failure_code"] == "TARGET_BINDING_INVALID"
    assert first["worker_result"]["failure_code"] == "TARGET_BINDING_INVALID"
    assert failed_paths and first["guard_state"] == "INVALIDATED" and not first_peer["allowed"]
    assert (incident / "S0.json").is_file() and (incident / "attempt.json").is_file()
    assert not list((tmp_path / "bundles").glob("*/COMMIT.json"))
    assert second["worker_runs_for_request"] == 0 and not after_restart_permit.challenges
    assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls


def test_terminal_marker_write_hang_is_bounded_and_late_completion_cannot_heal_unknown(tmp_path, request, monkeypatch):
    import cdpd_chatgpt_profile.capture_service as service_module

    harness = Harness(tmp_path)
    original_write = service_module._write_new
    release = threading.Event()
    entered = threading.Event()
    late_notice_written = threading.Event()
    blocked_paths = []

    def blocked_write(path, content):
        path = Path(path)
        if path.parent.name == "terminal-bindings":
            blocked_paths.append(path.relative_to(tmp_path).as_posix())
            entered.set()
            release.wait(5)
        result = original_write(path, content)
        if path.name == "TERMINAL_NOTICE.json":
            late_notice_written.set()
        return result

    monkeypatch.setattr(service_module, "_write_new", blocked_write)
    started = time.monotonic()
    try:
        first = harness.capture(mode="pages_after", expected_error="BINDING_INVALIDATION_UNCONFIRMED")
        elapsed = time.monotonic() - started
        incident = tmp_path / "state/incidents" / harness.data["capture_id"]
        receipt_before = (incident / "TERMINAL_RECEIPT.json").read_bytes()
        saved = json.loads(receipt_before)
        assert entered.is_set() and not late_notice_written.is_set()
        release.set()
        assert late_notice_written.wait(3)
        receipt_after = (incident / "TERMINAL_RECEIPT.json").read_bytes()
        monkeypatch.setattr(service_module, "_write_new", original_write)
        after_restart_permit = Permit()
        restarted_coordinator(harness, permit=after_restart_permit)
        second = harness.capture(data=next_request(harness.data), expected_error="BINDING_INVALIDATION_UNCONFIRMED")
        record(request, harness,
               expected={"caller_error": "BINDING_INVALIDATION_UNCONFIRMED",
                         "original_failure": "TARGET_BINDING_INVALID", "durable_latch": False,
                         "late_write_cannot_change_saved_receipt": True,
                         "automatic_recovery": False, "next_child_spawns": 0, "commits": 0},
               actual={"blocked_paths": blocked_paths, "elapsed_seconds": elapsed,
                       "saved_notification": saved, "late_notice_written": late_notice_written.is_set(),
                       "receipt_unchanged_after_late_write": receipt_before == receipt_after,
                       "restart_permit_calls": len(after_restart_permit.challenges)})
        assert first["original_failure_code"] == second["original_failure_code"] == "TARGET_BINDING_INVALID"
        assert first["worker_result"]["failure_code"] == "TARGET_BINDING_INVALID"
        assert elapsed < 3.5 and blocked_paths
        assert saved["durable_latch"] is False and saved["automatic_recovery"] is False
        assert receipt_before == receipt_after
        assert (incident / "S0.json").is_file() and (incident / "TERMINAL_WORKER_RESULT.json").is_file()
        assert not list((tmp_path / "bundles").glob("*/COMMIT.json"))
        assert second["worker_runs_for_request"] == 0 and not after_restart_permit.challenges
        assert len(harness.runner.runs) == 1 and not harness.authority.operation_calls
    finally:
        release.set()


@pytest.mark.parametrize("mode,status,reason", [
    ("normal", "COMPLETE", None),
    ("normal_detach", "COMPLETE", None),
    ("navigation_after", "PARTIAL", "NAVIGATION_DURING_CAPTURE"),
    ("collector_exception", "PARTIAL", "COLLECTOR_EXCEPTION"),
    ("preconnect_failure", "PARTIAL", "CDP_CONNECT_FAILED"),
])
def test_nonterminal_failure_or_normal_detach_preserves_next_explicit_capture(tmp_path, request, mode, status, reason):
    harness = Harness(tmp_path)
    first = harness.capture(mode=mode)
    peer = harness.peer_check()
    second = harness.capture(data=next_request(harness.data))
    record(request, harness,
           expected={"first_status": status, "first_reason": reason, "guard_state": "BOUND",
                     "peer_allowed": True, "second_status": "COMPLETE", "total_worker_spawns": 2},
           actual={"peer_after_first": peer, "normal_cleanup_is_control_loss": False,
                   "preconnect_failure_is_established_control_loss": False})
    assert first["capture_status"] == status and first["failure_code"] in ({None, "NONE"} if reason is None else {reason})
    assert first["structure_retained"] == (status == "COMPLETE")
    assert first["guard_state"] == "BOUND" and peer["allowed"]
    assert harness.guard.state == "BOUND" and not harness.authority.invalidations
    assert second["capture_status"] == "COMPLETE" and second["structure_retained"]
    assert second["worker_result"]["command_audit"].count("Runtime.evaluate") == 1
    assert len(harness.runner.runs) == 2
    assert all(row["verification"]["integrity_valid"] and row["verification"]["profile_valid"]
               for row in (first, second))
    assert_owned_one_shot(harness)


def test_policy_denied_starts_no_child_and_does_not_revoke_live_binding(tmp_path, request):
    harness = Harness(tmp_path, data=request_data(policy=False))
    first = harness.capture()
    peer = harness.peer_check()
    second = harness.capture(data=next_request(harness.data, policy=True))
    record(request, harness,
           expected={"first_status": "REJECTED", "first_worker_spawns": 0,
                     "peer_allowed": True, "second_status": "COMPLETE", "total_worker_spawns": 1},
           actual={"peer_after_first": peer})
    assert first["capture_status"] == "REJECTED" and first["failure_code"] == "POLICY_DENIED"
    assert not first["structure_retained"] and first["worker_runs_for_request"] == 0
    assert peer["allowed"] and harness.guard.state == "BOUND" and not harness.authority.invalidations
    assert second["capture_status"] == "COMPLETE" and second["structure_retained"]
    assert len(harness.runner.runs) == 1
    assert_owned_one_shot(harness)


def test_established_worker_channel_disconnect_is_terminal_and_has_no_followup_dom(tmp_path, request):
    harness = Harness(tmp_path)
    first = harness.capture(mode="disconnect_after")
    peer = harness.peer_check()
    second = harness.capture(data=next_request(harness.data))
    record(request, harness,
           expected={"first_reason": "DISCONNECTED", "guard_state": "INVALIDATED",
                     "peer_allowed": False, "second_dom": False, "total_worker_spawns": 1},
           actual={"peer_after_first": peer,
                   "connection_scope": "ESTABLISHED_SYNTHETIC_WORKER_DIAGNOSTIC_CHANNEL",
                   "operation_control_channel_simulated": False})
    assert first["capture_status"] == "PARTIAL" and first["failure_code"] == "DISCONNECTED"
    assert not first["structure_retained"] and first["guard_state"] == "INVALIDATED"
    assert not peer["allowed"] and first["authority_snapshot"]["revoked"]
    assert second["capture_status"] == "PARTIAL" and not second["structure_retained"]
    assert second["worker_runs_for_request"] == 0 and len(harness.runner.runs) == 1
    assert_owned_one_shot(harness)


def test_new_explicitly_issued_binding_recovers_only_new_generation(tmp_path, request):
    harness = Harness(tmp_path)
    first = harness.capture(mode="pages_after")
    old_peer = harness.peer_check()
    new_record = replace(harness.record, binding_id=str(uuid4()))
    harness.authority.current = AuthoritySnapshot(new_record.binding_id, new_record.record_hash, 2)
    new_guard = BindingGuard(new_record, harness.authority, clock=harness.clock)
    new_peer = BindingGuard(new_record, harness.authority, clock=harness.clock)
    new_data = next_request(harness.data)
    raw = new_record.to_bytes().decode()
    raw_sha = hashlib.sha256(raw.encode()).hexdigest()
    new_data.update(binding_record_raw_json=raw, binding_record_sha256=raw_sha, control_generation=2)
    # Accepted L05 producer-instance sequence remains monotonic across Binding
    # changes; explicit issuance does not restart it.
    new_data["trigger_envelope"].update(binding_id=new_record.binding_id, binding_sha256=raw_sha,
                                        control_generation=2)
    bridge = RecordingBridge(new_guard, supervisor_instance_id=new_data["supervisor_instance_id"],
                             binding_record_raw_json=raw)
    harness.bridges.append(bridge)
    harness.service.permit = bridge
    second = harness.capture(data=new_data)
    new_peer_result = harness.peer_check(new_peer)
    old_after_new = harness.peer_check(harness.guard)
    record(request, harness,
           expected={"old_terminal": True, "old_peer_allowed": False,
                     "new_guard_state": "BOUND", "new_peer_allowed": True, "new_status": "COMPLETE"},
           actual={"old_peer_after_failure": old_peer, "old_guard_after_new_issuance": old_after_new,
                   "new_peer": new_peer_result, "new_guard_state": new_guard.state,
                   "new_binding_id": new_record.binding_id, "new_generation": new_guard.generation,
                   "explicit_authority_snapshot_issued": True})
    assert first["guard_state"] == "INVALIDATED" and not old_peer["allowed"]
    assert not old_after_new["allowed"] and harness.guard.state == "INVALIDATED"
    assert new_record.binding_id != harness.record.binding_id and new_guard.generation == 2
    assert new_guard.state == "BOUND" and new_peer_result["allowed"]
    assert second["capture_status"] == "COMPLETE" and second["structure_retained"]
    assert not harness.authority.snapshot().revoked
    assert len(harness.runner.runs) == 2
    assert_owned_one_shot(harness)


def test_store_refusal_after_identity_violation_cannot_erase_guard_latch(tmp_path, request, monkeypatch):
    harness = Harness(tmp_path)
    original_commit = harness.store.commit
    commit_observations = []

    def refuse(*args, **kwargs):
        commit_observations.append({"guard_state": harness.guard.state,
                                    "revoked": harness.authority.snapshot().revoked})
        raise OSError("SYNTHETIC_PRIVATE_STORAGE_EXCEPTION")

    monkeypatch.setattr(harness.store, "commit", refuse)
    first = harness.capture(mode="pages_after", expected_error="STORAGE_FAILED")
    peer = harness.peer_check()
    first_incident = tmp_path / "state/incidents" / harness.data["capture_id"]
    first_event = harness.data["trigger_envelope"]["event_id"]
    protected = first_event in harness.service.protected_event_ids()
    monkeypatch.setattr(harness.store, "commit", original_commit)
    second = harness.capture(data=next_request(harness.data))
    record(request, harness,
           expected={"storage_error": "STORAGE_FAILED", "invalidated_before_commit": True,
                     "peer_allowed": False, "s0_preserved": True, "second_dom": False,
                     "total_worker_spawns": 1},
           actual={"commit_observations": commit_observations, "peer_after_refusal": peer,
                   "s0_preserved": (first_incident / "S0.json").is_file(),
                   "active_trigger_protected": protected,
                   "failed_capture_claims_commit": (tmp_path / "bundles" / harness.data["capture_id"] / "COMMIT.json").exists()})
    assert first["exception_code"] == "STORAGE_FAILED"
    assert first["worker_result"]["failure_code"] == "TARGET_BINDING_INVALID"
    assert commit_observations == [{"guard_state": "INVALIDATED", "revoked": True}]
    assert first["guard_state"] == "INVALIDATED" and not peer["allowed"]
    assert (first_incident / "S0.json").is_file() and protected
    assert not (tmp_path / "bundles" / harness.data["capture_id"] / "COMMIT.json").exists()
    assert second["capture_status"] == "PARTIAL" and not second["structure_retained"]
    assert second["worker_runs_for_request"] == 0 and len(harness.runner.runs) == 1
    assert_owned_one_shot(harness)
