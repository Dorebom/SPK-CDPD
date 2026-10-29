"""Additive FIX002 EXECUTION_INTERNAL_QA of exact known-loss classifications.

Actual pinned WebSocket -> actual transport -> worker -> coordinator -> accepted
L03 bridge all execute, with a synchronous in-process runner and synthetic IO.
The unchanged fixed R2 reproducer separately tests actual owned child processes.
No independent Assessment, native-platform, live browser, or release acceptance
is claimed. Earlier sources, tests and assertions are not changed by this file.
"""
from __future__ import annotations

from copy import deepcopy
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
from uuid import uuid4

import pytest
import websocket
import websocket._socket

from chatgpt_web_api.target_guard import GuardError
from cdpd_chatgpt_adapter.l03_binding import L03BindingBridge
from cdpd_chatgpt_profile.capture_service import CaptureCoordinator, L04CapturePermitAuthority
from cdpd_chatgpt_profile.evidence_store import ProfileEvidenceStore, verify_profile_bundle
from cdpd_chatgpt_profile.worker import WorkerRequest, run_capture_worker
from l06_fix002_wire_fixture import PRIVATE_DETAIL, actual_connection, raw_error
from test_fix001_clock_terminal import ledger, real_owner_runtime
from test_l06_capture_service import snapshot, spool_fixture
from test_l06_fix001_e2e import next_request
from test_l06_fix001_live_bridge import live_job_data


ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMES = (
    "code/cdpd_chatgpt_profile/capture_transport.py",
    "code/cdpd_chatgpt_profile/worker.py",
    "code/cdpd_chatgpt_profile/capture_service.py",
    "code/cdpd_chatgpt_profile/terminal_failure.py",
    "accepted_l03/code/cdpd_chatgpt_profile/preflight.py",
    "packages/cdpd-chatgpt-adapter/src/cdpd_chatgpt_adapter/l03_binding.py",
    "packages/chatgpt-web-api/src/chatgpt_web_api/target_guard.py",
    "tests/l06_fix002_wire_fixture.py",
    "tests/test_l06_fix002_transport.py",
)


@pytest.fixture(autouse=True)
def prohibit_real_network(monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("FIX002_EXECUTION_REAL_NETWORK_PROHIBITED")
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket.socket, "connect_ex", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", prohibited)
    monkeypatch.setattr(websocket, "create_connection", prohibited)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def observe(request, *, expected, actual, area):
    dependencies = Path(websocket.__file__).parent
    row = {"role": "EXECUTION_INTERNAL_QA", "ac": ["AC02"],
           "rr": ["L06-RR-001", "L06-RR-005", "L06-RR-006", "L06-RR-007",
                  "L06-R2-RR-001", "L06-R2-RR-002", "L06-R2-RR-003"],
           "expected": expected, "actual": actual,
           "scope": "ACTUAL_CLASSES_SYNTHETIC_SOCKET_SYNCHRONOUS_RUNNER",
           "commands": [],
           "source_sha256": {name: sha(ROOT / name) for name in SOURCE_NAMES},
           "dependency": {"name": "websocket-client", "version": websocket.__version__,
                          "sha256": {name: sha(dependencies / name)
                                     for name in ("__init__.py", "_socket.py", "_core.py")}},
           "limits": {"native_windows": False, "live_browser": False,
                      "real_profile_state": False, "owned_process_claim": False,
                      "independent_assessment": False, "finding_closed": False}}
    request.node._l06_evidence = row
    output = os.environ.get("CDPD_L06_EVIDENCE_DIR")
    if output:
        directory = Path(output) / ("case_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "EXPECTED_ACTUAL.json").write_text(json.dumps(row, indent=2, allow_nan=False) + "\n")
        for name in ("state", "spool", "bundles"):
            shutil.copytree(area / name, directory / name, dirs_exist_ok=True)


class RecordingAuthority(L04CapturePermitAuthority):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def __call__(self, challenge):
        response = super().__call__(challenge)
        self.calls.append({"challenge": deepcopy(challenge), "response": deepcopy(response)})
        return response


class SynchronousRunner:
    """No child claim: the fixed R2 process controls provide that evidence."""

    def __init__(self, area, fault):
        self.area, self.fault = area, fault
        self.runs = []

    def __call__(self, job, *, permit):
        immutable_before = job._raw
        s0 = self.area / "state" / "incidents" / job.to_dict()["capture_id"] / "S0.json"
        assert s0.is_file()
        s0_before = sha(s0)
        connections = []

        def factory(endpoint, timeout):
            assert endpoint == "http://127.0.0.1:9222" and timeout > 0
            connection, wire, wrapped = actual_connection(**(self.fault if not self.runs else {}))
            connections.append((connection, wire, wrapped))
            return connection

        result = run_capture_worker(job, permit=permit, connection_factory=factory)
        assert job._raw == immutable_before and sha(s0) == s0_before
        self.runs.append({"result": deepcopy(result), "s0_sha256": s0_before,
                          "factory_calls": len(connections),
                          "connections": [{"wire": wire.observation(),
                                           "transport_audit": connection.audit,
                                           "metadata_audit": connection.metadata_audit,
                                           "actual_websocket_type": type(wrapped).__module__ + "." + type(wrapped).__name__}
                                          for connection, wire, wrapped in connections]})
        return result


def exercise(error_factory, point, env, area, request, *, terminal, first_complete=False,
             fault_method="Target.getTargetInfo", extra=None):
    assert type(env.bridge) is L03BindingBridge
    peer = env.bridge.create_guard()
    data = live_job_data(env, supervisor=str(uuid4()))
    record_before, ledger_before = env.bridge.current_record.to_bytes(), ledger(env)
    generation_before = env.bridge.generation
    dispatch_before = len(env.transport.primitive_commands)
    authority = RecordingAuthority(env.guard, supervisor_instance_id=data["supervisor_instance_id"],
                                   binding_record_raw_json=data["binding_record_raw_json"])
    runner = SynchronousRunner(area, {"error_factory": error_factory, "point": point,
                                     "method": fault_method,
                                     "occurrence": 1 if fault_method == "Target.detachFromTarget" else 3})
    service = CaptureCoordinator(area / "state", area / "spool", ProfileEvidenceStore(area / "bundles"),
                                 runner=runner, permit=authority, utcnow=lambda: env.clock.sample().utc)
    captures = []
    for index, job_data in enumerate((data, next_request(data))):
        spool_fixture(area / "spool", job_data["trigger_envelope"])
        spool_before, calls_before = snapshot(area / "spool"), len(runner.runs)
        outcome = service.capture(WorkerRequest.from_dict(job_data))
        bundle = Path(outcome.bundle_path)
        verification = verify_profile_bundle(bundle)
        assert verification["integrity_valid"] and verification["profile_valid"]
        assert snapshot(area / "spool") == spool_before
        captures.append({"status": outcome.capture_status, "failure_code": outcome.failure_code,
                         "structure_present": (bundle / "STRUCTURE.json").is_file(),
                         "verification": verification, "runner_calls": len(runner.runs) - calls_before,
                         "evaluate_calls": sum(run["result"]["command_audit"].count("Runtime.evaluate")
                                               for run in runner.runs[calls_before:]),
                         "worker_result": deepcopy(service.last_worker_result),
                         "metadata": deepcopy(service.last_capture_metadata)})
        if index == 0:
            shared_after_first = env.bridge.state
            if terminal:
                with pytest.raises(GuardError):
                    peer.check(mutation=True)
                peer_allowed = False
            else:
                peer.check(mutation=True)
                peer_allowed = True

    first, second = captures
    expected_first_code = None if first_complete else ("DISCONNECTED" if terminal else "CDP_COMMAND_FAILED")
    assert first["status"] == ("COMPLETE" if first_complete else "PARTIAL")
    assert first["failure_code"] == expected_first_code
    assert first["structure_present"] is first_complete
    assert first["evaluate_calls"] == 1 and first["runner_calls"] == 1
    assert shared_after_first == ("INVALIDATED" if terminal else "BOUND")
    assert peer_allowed is (not terminal)
    assert second["runner_calls"] == int(not terminal)
    assert second["evaluate_calls"] == int(not terminal)
    assert second["structure_present"] is (not terminal)
    assert second["status"] == ("PARTIAL" if terminal else "COMPLETE")
    assert second["failure_code"] == ("DISCONNECTED" if terminal else None)
    notices = [call for call in authority.calls if call["challenge"]["phase"].startswith("TERMINAL:")]
    assert len(notices) == int(terminal)
    if terminal:
        notice, receipt = notices[0]["challenge"], notices[0]["response"]
        assert notice["phase"] == "TERMINAL:DISCONNECTED"
        for key, expected in {"binding_id": data["trigger_envelope"]["binding_id"],
                              "binding_sha256": data["binding_record_sha256"],
                              "control_generation": data["control_generation"],
                              "capture_id": data["capture_id"],
                              "supervisor_instance_id": data["supervisor_instance_id"]}.items():
            assert notice[key] == expected
        assert isinstance(notice["nonce"], str) and len(notice["nonce"]) == 32
        assert receipt["status"] == "CONFIRMED" and receipt["shared_state"] == "REVOKED"
        assert receipt["reason_code"] == "DISCONNECTED"
    assert env.bridge.current_record.to_bytes() == record_before
    assert env.bridge.generation == generation_before and ledger(env) == ledger_before
    assert len(env.owner_calls) == len(env.probes) == 1
    assert len(env.transport.primitive_commands) == dispatch_before
    assert all(run["factory_calls"] == 1 for run in runner.runs)
    for run in runner.runs:
        connection = run["connections"][0]
        assert connection["actual_websocket_type"] == "websocket._core.WebSocket"
        assert connection["metadata_audit"] == []
        packets = connection["wire"]["wire_packets"]
        assert len({packet["id"] for packet in packets}) == len(packets)
        assert connection["wire"]["socket_close_calls"] == 1
    assert len(runner.runs[0]["connections"][0]["wire"]["native_exceptions"]) == 1
    assert PRIVATE_DETAIL not in json.dumps({"captures": captures, "permits": authority.calls})
    observe(request,
            expected={"terminal": terminal, "first_failure_code": expected_first_code,
                      "shared": "INVALIDATED" if terminal else "BOUND",
                      "same_generation_next_runner_calls": int(not terminal),
                      "same_generation_next_evaluate_calls": int(not terminal),
                      "explicit_preflight_calls": 1, "product_reconnect": 0,
                      "application_resend": 0, "native_windows_acceptance": False},
            actual={"point": point, "captures": captures, "shared_after_first": shared_after_first,
                    "peer_mutation_allowed": peer_allowed, "terminal_notices": notices,
                    "permit_log": authority.calls, "runs": runner.runs,
                    "binding_record_unchanged": True, "ledger_unchanged": True,
                    "generation_unchanged": True, "preflight_calls": len(env.owner_calls),
                    "additional": extra or {}}, area=area)


PORTABLE_FATAL_ERRNOS = [
    ("ECONNRESET", errno.ECONNRESET), ("ECONNABORTED", errno.ECONNABORTED),
    ("EPIPE", errno.EPIPE), ("ENOTCONN", errno.ENOTCONN),
    *[(name, getattr(errno, name)) for name in ("ENETRESET", "ESHUTDOWN") if hasattr(errno, name)],
]


FATAL_REPRESENTATIONS = [
    pytest.param(lambda: ConnectionResetError(PRIVATE_DETAIL), id="typed_reset_no_errno"),
    pytest.param(lambda: ConnectionAbortedError(PRIVATE_DETAIL), id="typed_aborted_no_errno"),
    pytest.param(lambda: BrokenPipeError(PRIVATE_DETAIL), id="typed_broken_pipe_no_errno"),
    *[pytest.param(lambda value=value: raw_error(value), id="raw_errno_" + name)
      for name, value in PORTABLE_FATAL_ERRNOS],
    *[pytest.param(lambda value=value: raw_error(value), id="windows_numeric_errno_" + str(value))
      for value in (10053, 10054, 10057, 10052, 10058)],
    *[pytest.param(lambda value=value: raw_error(errno.EIO, winerror=value), id="winerror_attribute_" + str(value))
      for value in (10053, 10054, 10057, 10052, 10058)],
]


@pytest.mark.parametrize("error_factory", FATAL_REPRESENTATIONS)
@pytest.mark.parametrize("point", ["send", "recv"])
def test_known_loss_shapes_discard_dom_and_revoke_actual_shared_bridge(
        error_factory, point, real_owner_runtime, tmp_path, request):
    """Raw codes, typed errors and Windows-shaped attributes reach real IO."""
    exercise(error_factory, point, real_owner_runtime, tmp_path, request, terminal=True)


def generic_with_fatal_attributes():
    error = RuntimeError(PRIVATE_DETAIL)
    error.errno, error.winerror = errno.ECONNRESET, 10054
    return error


def generic_with_fatal_cause():
    error = RuntimeError(PRIVATE_DETAIL)
    error.__cause__ = ConnectionResetError(errno.ECONNRESET, PRIVATE_DETAIL)
    return error


NONTERMINAL_REPRESENTATIONS = [
    pytest.param(lambda: raw_error(errno.EIO), id="unknown_portable_errno"),
    pytest.param(lambda: raw_error(errno.EINTR), id="interrupted_errno"),
    pytest.param(lambda: raw_error(errno.EBADF), id="invalid_descriptor_not_stream_loss"),
    pytest.param(lambda: raw_error(errno.ENOTSOCK), id="not_socket_not_stream_loss"),
    pytest.param(lambda: raw_error(errno.ETIMEDOUT), id="native_timeout_errno"),
    pytest.param(lambda: TimeoutError(PRIVATE_DETAIL), id="typed_timeout_no_errno"),
    pytest.param(lambda: raw_error(errno.EIO, winerror=12345), id="unknown_winerror"),
    pytest.param(lambda: raw_error(errno.EIO, winerror="10054"), id="text_winerror_not_numeric"),
    pytest.param(lambda: raw_error(message="ConnectionResetError ECONNRESET 10054"), id="message_only"),
    pytest.param(generic_with_fatal_attributes, id="non_os_exception_fatal_attributes"),
    pytest.param(generic_with_fatal_cause, id="generic_exception_chained_reset"),
    pytest.param(lambda: ConnectionRefusedError(errno.ECONNREFUSED, PRIVATE_DETAIL), id="refused_not_established_loss"),
]


@pytest.mark.parametrize("error_factory", NONTERMINAL_REPRESENTATIONS)
@pytest.mark.parametrize("point", ["send", "recv"])
def test_other_error_shapes_cannot_invent_terminal_loss(
        error_factory, point, real_owner_runtime, tmp_path, request):
    """Neither message text, chained cause nor arbitrary attributes prove loss."""
    exercise(error_factory, point, real_owner_runtime, tmp_path, request, terminal=False)


@pytest.mark.parametrize("point", ["send", "recv"])
def test_would_block_then_ready_keeps_binding_and_sends_one_application_packet(
        point, real_owner_runtime, tmp_path, request, monkeypatch):
    """Pinned dependency's same-socket readiness handling is not fatal loss."""
    readiness = []

    class MemoryReadySelector:
        def register(self, wire, events):
            self.wire, self.events = wire, events
            readiness.append({"operation": "register", "events": events})

        def select(self, timeout):
            assert timeout > 0
            readiness.append({"operation": "ready", "timeout": timeout})
            return [(self.wire, self.events)]

        def close(self):
            readiness.append({"operation": "close"})

    monkeypatch.setattr(websocket._socket.selectors, "DefaultSelector", MemoryReadySelector)
    exercise(lambda: BlockingIOError(errno.EAGAIN, PRIVATE_DETAIL), point,
             real_owner_runtime, tmp_path, request, terminal=False, first_complete=True,
             extra={"readiness": readiness, "readiness_is_synthetic": True})
    assert [item["operation"] for item in readiness] == ["register", "ready", "close"]


@pytest.mark.parametrize("point,winerror", [
    pytest.param("send", 10057, id="send"),
    pytest.param("recv", 10057, id="recv"),
    pytest.param("send", 10058, id="shutdown-send"),
    pytest.param("recv", 10058, id="shutdown-recv"),
])
def test_winerror_shaped_loss_during_normal_final_detach_is_cleanup_only(
        point, winerror, real_owner_runtime, tmp_path, request):
    """The expanded classifier does not turn final diagnostic detach into loss."""
    exercise(lambda: raw_error(errno.EIO, winerror=winerror), point,
             real_owner_runtime, tmp_path, request, terminal=False, first_complete=True,
             fault_method="Target.detachFromTarget")
