"""Identity-bound terminal notifications for the L06 diagnostic boundary.

This module issues no Binding and performs no Web operation. An owned Worker
may report a fixed terminal cause through its existing permit pipe. The live
adapter uses the unchanged accepted L04 guard's scoped invalidation and then
reads shared state: a swallowed exception or missing acknowledgement is never
reported as successful shared revocation. Local uncertainty remains sticky.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

TERMINAL_CODES = frozenset({"TARGET_BINDING_INVALID", "TARGET_ORIGIN_MISMATCH",
    "BROWSER_IDENTITY_MISMATCH", "FRAME_ORIGIN_UNVERIFIED", "DISCONNECTED", "KNOWN_CDP_LOSS"})
CHALLENGE_KEYS = frozenset({"schema", "phase", "nonce", "capture_id", "binding_id",
    "binding_sha256", "control_generation", "supervisor_instance_id"})
RECEIPT_KEYS = CHALLENGE_KEYS | {"status", "reason_code", "local_state", "shared_state"}
NOTIFICATION_SECONDS = 0.75  # strictly within the existing one-second pipe bound


def is_terminal(value: Any) -> bool:
    return type(value) is dict and type(value.get("phase")) is str and value["phase"].startswith("TERMINAL:")


def valid_scope(value: Any, expected: dict[str, Any]) -> bool:
    return (type(value) is dict and set(value) == CHALLENGE_KEYS
        and value.get("schema") == "cdpd.capture-worker-permit-check/v1"
        and type(value.get("phase")) is str
        and type(value.get("nonce")) is str and re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) is not None
        and type(value.get("capture_id")) is str and re.fullmatch(r"C-[0-9a-f]{32}", value["capture_id"]) is not None
        and all(type(value.get(k)) is type(v) and value.get(k) == v for k, v in expected.items()))


def terminal_receipt(challenge: dict, *, status="UNCONFIRMED", reason=None,
                     local_state="UNKNOWN", shared_state="UNKNOWN") -> dict:
    # Rejected input is never copied wholesale into the protocol/evidence.
    result = {k: challenge.get(k) for k in CHALLENGE_KEYS}
    result.update(schema="cdpd.capture-terminal-receipt/v1", status=status,
                  reason_code=reason or challenge.get("phase", "").removeprefix("TERMINAL:"),
                  local_state=local_state, shared_state=shared_state)
    return result


def valid_receipt(value: Any, challenge: dict) -> bool:
    return (type(value) is dict and set(value) == RECEIPT_KEYS
        and value.get("schema") == "cdpd.capture-terminal-receipt/v1"
        and all(type(value.get(k)) is type(v) and value.get(k) == v for k, v in challenge.items() if k != "schema")
        and value.get("status") in {"CONFIRMED", "UNCONFIRMED", "REJECTED"}
        and value.get("reason_code") in TERMINAL_CODES
        and value.get("local_state") in {"BOUND", "INVALIDATED", "EXPIRED", "UNKNOWN"}
        and value.get("shared_state") in {"REVOKED", "NOT_CURRENT", "UNKNOWN"}
        and (value["status"] != "CONFIRMED" or (value["local_state"] in {"INVALIDATED", "EXPIRED"}
                                                   and value["shared_state"] == "REVOKED")))


class LiveTerminalAuthority:
    """One immutable accepted guard; never re-resolve a delayed notice to current."""
    def __init__(self, guard, raw_hash: str, supervisor_instance_id: str) -> None:
        self.guard = guard
        self.expected = {"binding_id": guard.record.binding_id, "binding_sha256": raw_hash,
                         "control_generation": guard.generation, "supervisor_instance_id": supervisor_instance_id}
        self._captures: set[str] = set()
        self._first_reason: str | None = None
        self._outcome: dict | None = None
        self._lock = threading.Lock()

    def register(self, challenge: dict) -> bool:
        if not valid_scope(challenge, self.expected):
            return False
        with self._lock:
            if self._first_reason is not None or len(self._captures) >= 4096:
                return False
            self._captures.add(challenge["capture_id"])
            return True

    def _accepted_bridge_readback(self) -> str:
        """The pinned L03/L04 bridge raises from snapshot() after revocation.

        Only that exact accepted class may use its existing locked live state
        as readback. No arbitrary duck-typed object or snapshot exception is an
        acknowledgement. This reads state; it neither admits nor patches L03.
        """
        from cdpd_chatgpt_adapter.l03_binding import L03BindingBridge
        authority = self.guard._authority
        if type(authority) is not L03BindingBridge or not authority._lock.acquire(blocking=False):
            return "UNKNOWN"
        try:
            record = authority.current_record
            if (record is not None and type(authority.generation) is int
                    and authority.generation == self.expected["control_generation"]
                    and record.binding_id == self.expected["binding_id"]
                    and record.record_hash == self.guard.record.record_hash):
                return "REVOKED" if authority.state in {"INVALIDATED", "EXPIRED"} else "UNKNOWN"
            if record is not None and (authority.generation != self.expected["control_generation"]
                                      or record.binding_id != self.expected["binding_id"]):
                return "NOT_CURRENT"
            return "UNKNOWN"
        finally:
            authority._lock.release()

    def notify(self, challenge: dict) -> dict:
        code = challenge.get("phase", "").removeprefix("TERMINAL:")
        if not valid_scope(challenge, self.expected) or code not in TERMINAL_CODES:
            return terminal_receipt(challenge, status="REJECTED", local_state=self.guard.state)
        with self._lock:
            if challenge["capture_id"] not in self._captures:
                return terminal_receipt(challenge, status="REJECTED", local_state=self.guard.state)
            if self._first_reason is not None:
                outcome = self._outcome or {"status": "UNCONFIRMED", "local_state": self.guard.state,
                                           "shared_state": "UNKNOWN"}
                return terminal_receipt(challenge, reason=self._first_reason, **outcome)
            self._first_reason = code
        # No adapter lock is held while calling the external authority. Accepted
        # BindingGuard sets its local terminal bit before the scoped callback.
        guard_reason = {"DISCONNECTED": "CDP_COMMAND_FAILED", "KNOWN_CDP_LOSS": "CDP_COMMAND_FAILED",
                        "FRAME_ORIGIN_UNVERIFIED": "TARGET_ORIGIN_MISMATCH"}.get(code, code)
        shared = "UNKNOWN"
        try:
            self.guard.invalidate(guard_reason)
            snapshot = self.guard._read_snapshot()
            if (type(snapshot.generation) is int and snapshot.generation == self.expected["control_generation"]
                    and snapshot.binding_id == self.expected["binding_id"]
                    and snapshot.record_hash == self.guard.record.record_hash):
                if snapshot.revoked is True or snapshot.state in {"INVALIDATED", "EXPIRED"}:
                    shared = "REVOKED"
            elif type(snapshot.generation) is int and (snapshot.generation != self.expected["control_generation"]
                                                       or snapshot.binding_id != self.expected["binding_id"]):
                shared = "NOT_CURRENT"
        except Exception:
            try:
                shared = self._accepted_bridge_readback()
            except Exception:
                shared = "UNKNOWN"
        outcome = {"status": "CONFIRMED" if shared == "REVOKED" and self.guard.state in {"INVALIDATED", "EXPIRED"}
                   else "UNCONFIRMED", "local_state": self.guard.state, "shared_state": shared}
        with self._lock:
            self._outcome = outcome
        return terminal_receipt(challenge, reason=code, **outcome)


class CapturePermitSession:
    """Per-job parent latch, bounded callback, and immutable first observation.

prepare() is called by the owned-pipe runner before starting a bounded external
callback. This preserves an authenticated observed cause even when that callback
hangs or the child result never arrives. finish() seals the outcome; a late
callback cannot restore authority or upgrade the recorded notification.
"""
    def __init__(self, data: dict, permit: Callable, *, on_detect: Callable[[dict], None],
                 timeout_seconds: float = NOTIFICATION_SECONDS) -> None:
        self.expected = {"capture_id": data["capture_id"], "binding_id": data["trigger_envelope"]["binding_id"],
                         "binding_sha256": data["binding_record_sha256"], "control_generation": data["control_generation"],
                         "supervisor_instance_id": data["supervisor_instance_id"]}
        self.permit, self.on_detect = permit, on_detect
        self.timeout_seconds = min(NOTIFICATION_SECONDS, max(0.001, timeout_seconds))
        self.deadline = time.monotonic() + data["capture_request"]["timeout_seconds"]
        self.first_notice: dict | None = None
        self.receipt: dict | None = None
        self.durable = True
        self._invoked = False
        self._sealed = False
        self._lock = threading.Lock()

    @property
    def failure_code(self) -> str | None:
        return None if self.first_notice is None else self.first_notice["phase"].removeprefix("TERMINAL:")

    @property
    def confirmed(self) -> bool:
        return self.durable and self.receipt is not None and self.receipt["status"] == "CONFIRMED"

    def prepare(self, challenge: dict) -> bool:
        if (not is_terminal(challenge) or not valid_scope(challenge, self.expected)
                or challenge["phase"].removeprefix("TERMINAL:") not in TERMINAL_CODES):
            return False
        with self._lock:
            if self._sealed:
                return False
            if self.first_notice is not None:
                return True
            self.first_notice = dict(challenge)
            self.receipt = terminal_receipt(challenge)
            self.durable = False
        try:
            from .worker_cli import _bounded_call
            deadline = min(self.deadline, time.monotonic() + 0.20)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            _bounded_call(lambda: self.on_detect(dict(challenge)), deadline)
            with self._lock:
                if not self._sealed:
                    self.durable = True
        except Exception:
            self.durable = False
        return True

    def __call__(self, challenge: dict) -> dict:
        if not is_terminal(challenge):
            if self.failure_code is not None or self._sealed or not valid_scope(challenge, self.expected):
                response = dict(challenge)
                response.update(schema="cdpd.capture-worker-permit/v1", allowed=False,
                                remaining_lease_seconds=0.0, reason_code=self.failure_code or "CLOCK_OR_KEEPER_LOST")
                return response
            return self.permit(dict(challenge))
        if not self.prepare(challenge):
            return terminal_receipt(challenge, status="REJECTED")
        with self._lock:
            if self._invoked:
                return terminal_receipt(challenge, reason=self.failure_code,
                    **{k: self.receipt[k] for k in ("status", "local_state", "shared_state")})
            self._invoked = True
        # Import only at call time to avoid the worker/CLI import cycle.
        from .worker_cli import _bounded_call
        try:
            deadline = min(self.deadline, time.monotonic() + self.timeout_seconds)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            receipt = _bounded_call(lambda: self.permit(dict(challenge)), deadline)
            if not valid_receipt(receipt, challenge) or receipt["reason_code"] != self.failure_code:
                raise ValueError()
        except Exception:
            receipt = terminal_receipt(challenge, reason=self.failure_code)
        with self._lock:
            if not self._sealed:
                self.receipt = receipt
            return dict(self.receipt)

    def finish(self) -> dict | None:
        with self._lock:
            self._sealed = True
            return None if self.receipt is None else dict(self.receipt)
