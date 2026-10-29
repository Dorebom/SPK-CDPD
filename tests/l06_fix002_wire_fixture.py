"""FIX002 EXECUTION_INTERNAL_QA socket fixture; no OS socket is allocated.

Framing follows the fixed R2 reproduction's memory-socket technique. This new
fixture is additive: the original Assessment harness and all earlier tests stay
unchanged. Actual websocket-client 1.8.0 and the actual product transport run
over this low-level fixture. Windows-shaped errors are synthetic on the host;
they do not establish native Windows behavior or acceptance.
"""
from __future__ import annotations

import json
import time

import websocket

from cdpd_chatgpt_profile.capture_transport import ReadOnlyCaptureConnection
from test_capture_worker import Connection


PRIVATE_DETAIL = "SYNTHETIC_FIX002_PRIVATE_SOCKET_DETAIL"


class RawSocketError(OSError):
    """Avoid OSError's automatic conversion to typed built-in subclasses."""


def raw_error(code=None, *, winerror=None, message=PRIVATE_DETAIL):
    error = RawSocketError(message) if code is None else RawSocketError(code, message)
    if winerror is not None:
        error.winerror = winerror
    return error


class MemorySocket:
    """A framed socket shape only, without a descriptor or external endpoint."""

    def __init__(self, target, *, error_factory=None, point="recv",
                 method="Target.getTargetInfo", occurrence=3):
        self.target = target
        self.error_factory = error_factory
        self.point, self.method, self.occurrence = point, method, occurrence
        self.timeout = 10.0
        self.pending = b""
        self.pending_fault = False
        self.fault_consumed = False
        self.packets = []
        self.failures = []
        self.counts = {}
        self.native_send_calls = 0
        self.native_recv_calls = 0
        self.close_count = 0
        self.shutdown_count = 0

    def settimeout(self, timeout):
        assert timeout is None or timeout >= 0
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    @staticmethod
    def decode_frame(raw):
        data = bytes(raw)
        opcode, masked, size = data[0] & 15, bool(data[1] & 128), data[1] & 127
        offset = 2
        if size in (126, 127):
            count = 2 if size == 126 else 8
            size = int.from_bytes(data[offset:offset + count], "big")
            offset += count
        key = data[offset:offset + 4] if masked else b""
        offset += 4 if masked else 0
        payload = data[offset:offset + size]
        assert len(payload) == size
        if masked:
            payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
        return opcode, payload

    @staticmethod
    def frame(payload, opcode=1):
        frame = websocket.ABNF.create_frame(payload, opcode)
        frame.mask_value = 0
        return frame.format()

    def _inject(self, point):
        self.fault_consumed = True
        error = self.error_factory()
        self.failures.append({"point": point, "type": type(error).__name__,
                              "errno": getattr(error, "errno", None),
                              "winerror": getattr(error, "winerror", None)})
        raise error

    def send(self, raw):
        self.native_send_calls += 1
        opcode, payload = self.decode_frame(raw)
        if opcode == 8:
            self.pending_fault = False
            self.pending = self.frame(payload, 8)
            return len(raw)
        assert opcode == 1
        packet = json.loads(payload)
        method = packet["method"]
        occurrence = self.counts.get(method, 0) + 1
        fault_here = (self.error_factory is not None and not self.fault_consumed
                      and method == self.method and occurrence == self.occurrence)
        # A failed low-level send did not deliver an application command. The
        # pinned library can retry EAGAIN on this same socket without a second
        # product-level dispatch or an additional CDP packet.
        if fault_here and self.point == "send":
            self._inject("send")
        self.counts[method] = occurrence
        self.packets.append(packet)
        self.pending_fault = fault_here and self.point == "recv"
        reply = {"id": packet["id"]}
        if "sessionId" in packet:
            reply["sessionId"] = packet["sessionId"]
        reply["result"] = self.target.send(method, packet["params"], packet.get("sessionId"))
        self.pending = self.frame(json.dumps(reply))
        return len(raw)

    def recv(self, count):
        self.native_recv_calls += 1
        if self.pending_fault:
            self.pending_fault = False
            self._inject("recv")
        result, self.pending = self.pending[:count], self.pending[count:]
        return result

    def close(self):
        self.close_count += 1

    def shutdown(self, how):
        self.shutdown_count += 1

    def observation(self):
        return {"native_exceptions": self.failures, "wire_packets": self.packets,
                "native_send_calls": self.native_send_calls,
                "native_recv_calls": self.native_recv_calls,
                "socket_close_calls": self.close_count,
                "shutdown_calls": self.shutdown_count,
                "real_socket_allocations": 0}


def actual_connection(**fault):
    """Retain actual WebSocket/send/recv/close; replace only the low-level IO."""
    assert websocket.__version__ == "1.8.0"
    target = Connection()
    wire = MemorySocket(target, **fault)
    wrapped = websocket.WebSocket(enable_multithread=True)
    wrapped.sock, wrapped.connected = wire, True
    connection = ReadOnlyCaptureConnection.__new__(ReadOnlyCaptureConnection)
    connection.endpoint = "http://127.0.0.1:9222"
    connection.deadline = time.monotonic() + 10.0
    connection.sequence = 0
    connection.audit, connection.metadata_audit = [], []
    connection.socket = wrapped
    connection.browser_id = target.browser_id
    connection.identity = target.identity
    return connection, wire, wrapped
