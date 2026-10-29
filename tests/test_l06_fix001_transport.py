"""FIX001 internal EXECUTION tests of the actual read-only wire classifier.

ReadOnlyCaptureConnection.send/close are exercised with an in-memory socket.
Its constructor and HTTP discovery are deliberately not called; no browser,
network, real Profile/State, or independent Assessment is involved.
"""
from __future__ import annotations

import json
import time

import pytest
import websocket

from cdpd_chatgpt_profile.capture_transport import ReadOnlyCaptureConnection
from cdpd_chatgpt_profile.worker import WorkerRequest, run_capture_worker
from test_capture_worker import Connection, Permit, request_data
from test_l06_fix001_authority import record_observation


class InMemoryWireSocket:
    def __init__(self, target, *, fault=None, fault_method="Target.getTargetInfo"):
        self.target = target
        self.fault = fault
        self.fault_method = fault_method
        self.sent = []
        self.recv_calls = 0
        self.close_calls = []

    def settimeout(self, value):
        assert value > 0

    def send(self, raw):
        packet = json.loads(raw)
        self.sent.append(packet)
        if packet["method"] == self.fault_method and self.fault == "closed_on_send":
            raise websocket.WebSocketConnectionClosedException("SYNTHETIC_SOCKET_CLOSED")

    def recv(self):
        self.recv_calls += 1
        packet = self.sent[-1]
        active_fault = self.fault if packet["method"] == self.fault_method else None
        if active_fault == "empty_text":
            return ""
        if active_fault == "empty_bytes":
            return b""
        if active_fault == "closed_on_recv":
            raise websocket.WebSocketConnectionClosedException("SYNTHETIC_SOCKET_CLOSED")
        if active_fault == "socket_timeout":
            raise websocket.WebSocketTimeoutException("SYNTHETIC_SOCKET_TIMEOUT")
        if active_fault == "other_exception":
            raise RuntimeError("SYNTHETIC_DIAGNOSTIC_COLLECTOR_EXCEPTION")
        if active_fault == "invalid_json":
            return "{invalid-json}"
        message = {"id": packet["id"]}
        if "sessionId" in packet:
            message["sessionId"] = packet["sessionId"]
        if active_fault == "cdp_error":
            message["error"] = {"code": -32000, "message": "SYNTHETIC_COMMAND_ERROR"}
        elif active_fault == "invalid_result":
            message["result"] = []
        else:
            message["result"] = self.target.send(packet["method"], packet["params"], packet.get("sessionId"))
        return json.dumps(message)

    def close(self, *, timeout):
        self.close_calls.append(timeout)
        if self.fault == "close_exception":
            raise RuntimeError("SYNTHETIC_NORMAL_CLEANUP_CLOSE_EXCEPTION")


def actual_transport_with_fake_socket(*, fault=None, fault_method="Target.getTargetInfo"):
    target = Connection()
    socket = InMemoryWireSocket(target, fault=fault, fault_method=fault_method)
    connection = ReadOnlyCaptureConnection.__new__(ReadOnlyCaptureConnection)
    connection.endpoint = "http://127.0.0.1:9222"
    connection.deadline = time.monotonic() + 10.0
    connection.sequence = 0
    connection.audit = []
    connection.metadata_audit = []
    connection.socket = socket
    connection.browser_id = target.browser_id
    # Identity metadata is a deterministic fixture; actual send/close stay bound
    # to ReadOnlyCaptureConnection and use only the injected in-memory socket.
    connection.identity = target.identity
    if fault == "transport_deadline":
        connection.deadline = time.monotonic() - 1.0
    return connection, socket


@pytest.mark.parametrize("fault,expected,terminal", [
    ("empty_text", "DISCONNECTED", True),
    ("empty_bytes", "DISCONNECTED", True),
    ("closed_on_recv", "DISCONNECTED", True),
    ("closed_on_send", "DISCONNECTED", True),
    ("invalid_json", "CDP_COMMAND_FAILED", False),
    ("invalid_result", "CDP_COMMAND_FAILED", False),
    ("cdp_error", "CDP_COMMAND_FAILED", False),
    ("socket_timeout", "CDP_COMMAND_FAILED", False),
    ("other_exception", "CDP_COMMAND_FAILED", False),
    ("transport_deadline", "CAPTURE_DEADLINE", False),
])
def test_actual_wire_failure_classification_and_worker_terminal_decision(fault, expected, terminal, request):
    """Only known established diagnostic loss becomes a terminal notification."""
    connection, socket = actual_transport_with_fake_socket(fault=fault)
    permit = Permit()
    factory_calls = []

    def factory(endpoint, timeout):
        factory_calls.append({"endpoint": endpoint, "timeout": timeout})
        return connection

    result = run_capture_worker(WorkerRequest.from_dict(request_data()), permit=permit,
                                connection_factory=factory)
    terminal_notices = [row for row in permit.challenges if row["phase"].startswith("TERMINAL:")]
    assert result["failure_code"] == expected and result["capture_status"] == "PARTIAL"
    assert result["structure"] is None and result["command_audit"].count("Runtime.evaluate") == 0
    assert len(terminal_notices) == int(terminal)
    if terminal:
        assert terminal_notices[0]["phase"] == "TERMINAL:DISCONNECTED"
    assert len(factory_calls) == 1 and len(socket.sent) <= 1
    assert socket.close_calls == [0.2]
    assert connection.metadata_audit == []
    record_observation(request,
        expected={"fault": fault, "failure_code": expected, "terminal_notifications": int(terminal),
                  "content_dispatch": 0, "factory_calls": 1, "reconnect": 0, "resend": 0,
                  "real_network_calls": 0},
        actual={"fault": fault, "failure_code": result["failure_code"],
                "capture_status": result["capture_status"], "structure_discarded": result["structure"] is None,
                "terminal_notifications": terminal_notices, "permit_challenges": permit.challenges,
                "wire_packets": socket.sent, "wire_recv_calls": socket.recv_calls,
                "transport_audit": connection.audit, "worker_command_audit": result["command_audit"],
                "factory_calls": factory_calls, "socket_cleanup": socket.close_calls,
                "network_constructor_called": False, "identity_source": "IN_MEMORY_FIXED_TARGET",
                "send_and_close_source": "ReadOnlyCaptureConnection"},
        rr=("L06-RR-001", "L06-RR-005", "L06-RR-006", "L06-RR-007"))


@pytest.mark.parametrize("cleanup_fault", [None, "empty_text", "closed_on_recv", "cdp_error", "close_exception"])
def test_normal_self_detach_and_cleanup_errors_do_not_create_terminal_failure(cleanup_fault, request):
    """An explicit final self-detach is cleanup of this diagnostic session."""
    connection, socket = actual_transport_with_fake_socket(
        fault=cleanup_fault, fault_method="Target.detachFromTarget")
    permit = Permit()
    result = run_capture_worker(WorkerRequest.from_dict(request_data()), permit=permit,
                                connection_factory=lambda *_: connection)
    terminal_notices = [row for row in permit.challenges if row["phase"].startswith("TERMINAL:")]
    assert result["capture_status"] == "COMPLETE" and result["failure_code"] is None
    assert result["structure"] is not None and terminal_notices == []
    assert result["command_audit"].count("Runtime.evaluate") == 1
    detach = [row for row in socket.sent if row["method"] == "Target.detachFromTarget"]
    assert len(detach) == 1 and detach[0]["params"] == {"sessionId": "SYNTHETIC-SESSION-A"}
    assert "sessionId" not in detach[0]
    assert socket.close_calls == [0.2]
    assert connection.audit[-1] == "Target.detachFromTarget"
    record_observation(request,
        expected={"channel": "DIAGNOSTIC_SELF_DETACH_CLEANUP", "capture_status": "COMPLETE",
                  "terminal_notifications": 0, "fixed_session_detach": 1, "content_dispatch": 1,
                  "reconnect": 0, "resend": 0, "real_network_calls": 0},
        actual={"channel": "DIAGNOSTIC_SELF_DETACH_CLEANUP", "cleanup_fault": cleanup_fault,
                "capture_status": result["capture_status"], "failure_code": result["failure_code"],
                "terminal_notifications": terminal_notices, "wire_packets": socket.sent,
                "transport_audit": connection.audit, "worker_command_audit": result["command_audit"],
                "detach_packets": detach, "socket_cleanup": socket.close_calls,
                "network_constructor_called": False, "identity_source": "IN_MEMORY_FIXED_TARGET",
                "send_and_close_source": "ReadOnlyCaptureConnection"},
        rr=("L06-RR-005", "L06-RR-006", "L06-RR-007"))
