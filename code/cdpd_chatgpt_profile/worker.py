"""One-shot independent Capture Worker: immutable input -> temporary result.

This module neither writes S0/current/journal nor publishes a Bundle COMMIT.
It is not a supervisor or Web-operation admission authority. Fresh permits and
read-only connection factories are injected by the trusted coordinator.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import math
import os
import re
import time
from typing import Any, Callable
from uuid import uuid4

from cdpd import CaptureRequest
from cdpd.collector import CaptureError
from chatgpt_web_api.target_guard import ParsedBinding, GuardError, single_page, same_target, frame_snapshot
from .spool import canonical_json, strict_json, validate_envelope
from .structure_script import capture_script, script_sha256, CATALOG_SHA256, FEATURE_IDS, SCRIPT_FAILURE_SCHEMA
from .terminal_failure import TERMINAL_CODES, valid_receipt
from .worker_contracts import (capture_rule as _worker_capture_rule,
                               validate_structure_payload as _worker_validate_structure_payload)

_JOB_KEYS = {"schema", "capture_request", "trigger_envelope", "binding_record_raw_json",
             "binding_record_sha256", "control_generation", "capture_id", "supervisor_instance_id", "policy_allowed"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CAPTURE = re.compile(r"^C-[0-9a-f]{32}$")
_RAW_STRUCTURE_KEYS = {"schema", "nodes", "truncated", "iframeCount", "openShadowCount", "coverage", "observation", "profile"}
_FAILURES = frozenset({"BINDING_REVOKED", "BROWSER_IDENTITY_MISMATCH", "CAPTURE_DEADLINE", "CAPTURE_FAILED",
    "CDP_COMMAND_FAILED", "CDP_CONNECT_FAILED", "CDP_DISCOVERY_FAILED", "CDP_DISCOVERY_TOO_LARGE", "CDP_RESPONSE_TOO_LARGE",
    "CLOCK_OR_KEEPER_LOST", "COLLECTOR_EXCEPTION", "COMMAND_NOT_ALLOWED", "DISCONNECTED", "ENDPOINT_NOT_ALLOWED",
    "ENDPOINT_REDIRECT_REJECTED", "FRAME_ORIGIN_UNVERIFIED", "KNOWN_CDP_LOSS", "LEASE_EXPIRED",
    "NAVIGATION_DURING_CAPTURE", "NO_ACTIVE_BINDING", "POLICY_DENIED", "PRIVACY_OR_IDENTITY_FAILURE",
    "SCRIPT_NOT_ALLOWED", "STRUCTURE_EVALUATION_FAILED", "STRUCTURE_INVALID", "SUPERVISOR_LOST",
    "TARGET_BINDING_INVALID", "TARGET_ORIGIN_MISMATCH", "WORKER_FAILED_OR_DEADLINE"})


class WorkerInputError(ValueError):
    def __init__(self, code: str = "INPUT_INVALID") -> None:
        self.code = "INPUT_INVALID"
        super().__init__(self.code)


@dataclass(frozen=True)
class WorkerRequest:
    """Only canonical serialized bytes are retained; nested caller edits cannot alter a job."""
    _raw: bytes

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkerRequest":
        try:
            raw = canonical_json(value)
            data = strict_json(raw, max_bytes=131072)
            if type(data) is not dict or set(data) != _JOB_KEYS or data["schema"] != "cdpd.capture-worker-request/v1":
                raise ValueError()
            if type(data["policy_allowed"]) is not bool or type(data["capture_id"]) is not str or not _CAPTURE.fullmatch(data["capture_id"]):
                raise ValueError()
            if type(data["supervisor_instance_id"]) is not str or not _UUID.fullmatch(data["supervisor_instance_id"]):
                raise ValueError()
            if type(data["control_generation"]) is not int or not 1 <= data["control_generation"] <= 9007199254740991:
                raise ValueError()
            if type(data["binding_record_raw_json"]) is not str:
                raise ValueError()
            binding_bytes = data["binding_record_raw_json"].encode("utf-8")
            if len(binding_bytes) > 8192 or hashlib.sha256(binding_bytes).hexdigest() != data["binding_record_sha256"]:
                raise ValueError()
            binding = ParsedBinding.parse(binding_bytes)
            request = CaptureRequest.from_dict(data["capture_request"])
            envelope = validate_envelope(data["trigger_envelope"])
            if (data["capture_request"]["target"] != binding.to_dict()["target"]
                    or request.event.to_dict() != envelope["event"]
                    or envelope["binding_id"] != binding.binding_id
                    or envelope["binding_sha256"] != data["binding_record_sha256"]
                    or envelope["control_generation"] != data["control_generation"]):
                raise ValueError()
            return cls(raw)
        except Exception:
            raise WorkerInputError() from None

    def to_dict(self) -> dict[str, Any]:
        return strict_json(self._raw, max_bytes=131072)


def _failure(error: Exception) -> str:
    code = getattr(error, "code", None)
    if code in _FAILURES:
        return code
    if isinstance(error, GuardError):
        if code in {"TARGET_MISSING", "TARGET_AMBIGUOUS", "SINGLE_PAGE_VIOLATION", "TARGET_UNVERIFIED"}:
            return "TARGET_BINDING_INVALID"
        if code in {"CLOCK_DISCONTINUITY", "CLOCK_UNVERIFIED"}:
            return "CLOCK_OR_KEEPER_LOST"
        if code == "BINDING_EXPIRED":
            return "LEASE_EXPIRED"
    return "COLLECTOR_EXCEPTION"


class _PermitGate:
    def __init__(self, data: dict[str, Any], permit: Callable, clock: Callable[[], float], timeout: float) -> None:
        self.data, self.permit, self.clock = data, permit, clock
        self.start = clock()
        self.deadline = self.start + timeout
        self.lease_deadline = self.deadline
        self.failed = False

    def remaining(self) -> float:
        left = min(self.deadline, self.lease_deadline) - self.clock()
        if self.failed or not math.isfinite(left) or left <= 0:
            raise CaptureError("CAPTURE_DEADLINE")
        return left

    def check(self, phase: str) -> None:
        self.remaining()
        sent = self.clock()
        data = self.data
        challenge = {"schema": "cdpd.capture-worker-permit-check/v1", "phase": phase, "nonce": uuid4().hex,
                     "capture_id": data["capture_id"], "binding_id": data["trigger_envelope"]["binding_id"],
                     "binding_sha256": data["binding_record_sha256"], "control_generation": data["control_generation"],
                     "supervisor_instance_id": data["supervisor_instance_id"]}
        try:
            response = self.permit(dict(challenge))
            now = self.clock()
            if type(response) is not dict or set(response) != set(challenge) | {"allowed", "remaining_lease_seconds", "reason_code"}:
                raise CaptureError("CLOCK_OR_KEEPER_LOST")
            if response["schema"] != "cdpd.capture-worker-permit/v1" or any(response[k] != v for k, v in challenge.items() if k != "schema"):
                raise CaptureError("CLOCK_OR_KEEPER_LOST")
            if not 0 <= now - sent <= 1.0:
                raise CaptureError("CLOCK_OR_KEEPER_LOST")
            lease = response["remaining_lease_seconds"]
            if (type(response["allowed"]) is not bool or type(lease) not in (int, float)
                    or not math.isfinite(lease) or not 0 <= lease <= 14400):
                raise CaptureError("CLOCK_OR_KEEPER_LOST")
            if response["allowed"] is not True:
                reason = response["reason_code"]
                raise CaptureError(reason if reason in _FAILURES else "CLOCK_OR_KEEPER_LOST")
            if response["reason_code"] != "NONE" or lease <= 0:
                raise CaptureError("LEASE_EXPIRED")
            # The send instant anchors the lease; IPC latency never extends it.
            self.lease_deadline = min(self.lease_deadline, sent + lease)
            self.remaining()
        except Exception as error:
            self.failed = True
            raise CaptureError(_failure(error) if isinstance(error, (CaptureError, GuardError)) else "CLOCK_OR_KEEPER_LOST") from None

    def notify_terminal(self, code: str) -> None:
        """Report only an observed terminal cause, never request a new permit.

        The existing child pipe enforces its original deadline. Notification
        failure cannot erase the first cause or turn discarded DOM into success.
        The parent independently latches and records confirmation uncertainty.
        """
        data = self.data
        challenge = {"schema": "cdpd.capture-worker-permit-check/v1", "phase": "TERMINAL:" + code,
                     "nonce": uuid4().hex, "capture_id": data["capture_id"],
                     "binding_id": data["trigger_envelope"]["binding_id"], "binding_sha256": data["binding_record_sha256"],
                     "control_generation": data["control_generation"], "supervisor_instance_id": data["supervisor_instance_id"]}
        try:
            # Do not use check(): a terminal receipt never grants lease/content.
            from .worker_cli import _bounded_call
            deadline = min(time.monotonic() + 1.0, time.monotonic() + max(0.0, self.deadline - self.clock()))
            response = _bounded_call(lambda: self.permit(challenge), deadline)
            if not valid_receipt(response, challenge):
                raise ValueError()
        except Exception:
            pass


def _validate_structure(structure: Any, *, maximum: int) -> None:
    from .bundle_validation import validate_payload_file
    validate_payload_file("STRUCTURE.json", structure)
    _validate_structure_semantics(structure, maximum=maximum)


def _validate_worker_structure(structure: Any, *, maximum: int) -> None:
    _worker_validate_structure_payload(structure)
    _validate_structure_semantics(structure, maximum=maximum)


def _validate_structure_semantics(structure: Any, *, maximum: int) -> None:
    nodes = structure["nodes"]
    shapes = structure["profile"]["feature_shapes"]
    if len(nodes) + sum(len(v) for v in shapes.values()) > maximum or sum(len(v) for v in shapes.values()) > min(200, maximum):
        raise CaptureError("STRUCTURE_INVALID")
    for index, node in enumerate(nodes):
        if node["index"] != index or node["parent"] >= index or node["parent"] < -1:
            raise CaptureError("STRUCTURE_INVALID")
        if index > 0 and node["parent"] == -1:
            raise CaptureError("STRUCTURE_INVALID")
    probes = structure["profile"]["probes"]
    if len(probes) != len(FEATURE_IDS) or {p["feature_id"] for p in probes} != set(FEATURE_IDS):
        raise CaptureError("STRUCTURE_INVALID")
    for probe in probes:
        if probe["visible_count"] > probe["match_count"] or probe["enabled_count"] > probe["match_count"]:
            raise CaptureError("STRUCTURE_INVALID")
        if len(shapes.get(probe["feature_id"], [])) > probe["match_count"]:
            raise CaptureError("STRUCTURE_INVALID")
        if not probe["probe_complete"] and not structure["truncated"]:
            raise CaptureError("STRUCTURE_INVALID")


def metadata_worker_result(job: WorkerRequest, code: str, *, termination: str = "NOT_STARTED") -> dict[str, Any]:
    """A safe no-DOM result for a coordinator/process failure; no success inference."""
    data = WorkerRequest.from_dict(job.to_dict()).to_dict()
    request = CaptureRequest.from_dict(data["capture_request"])
    if code not in _FAILURES | {"NOT_RUN", "SPOOL_BACKPRESSURE"}:
        code = "WORKER_FAILED_OR_DEADLINE"
    if termination not in {"NORMAL", "KILLED", "EXITED_ERROR", "UNCONFIRMED", "NOT_STARTED"}:
        termination = "UNCONFIRMED"
    rejected = not data["policy_allowed"] and code == "POLICY_DENIED"
    return {"schema": "cdpd.capture-worker-result/v1", "capture_id": data["capture_id"],
            "capture_status": "REJECTED" if rejected else "PARTIAL", "failure_code": code, "structure": None,
            "identity_checks": {"before": False, "after": False, "current": False, "keeper": False, "lease": False},
            "before": None, "after": None,
            "stages": {name: {"status": "NOT_RUN", "reason_code": code, "duration_ms": 0} for name in ("S1", "S2")},
            "sanitization_passed": False, "worker_termination": termination, "command_audit": [],
            "script_sha256": script_sha256(request.max_nodes), "probe_catalog_sha256": CATALOG_SHA256, "pid": os.getpid()}


def run_capture_worker(job: WorkerRequest, *, permit: Callable[[dict], dict],
                       connection_factory: Callable, monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read once, validate before/after, return safe temporary data; never COMMIT."""
    if not isinstance(job, WorkerRequest):
        raise WorkerInputError()
    # Validate even a direct dataclass construction; serialized input is not authority.
    data = WorkerRequest.from_dict(job.to_dict()).to_dict()
    request = CaptureRequest.from_dict(data["capture_request"])
    binding = ParsedBinding.parse(data["binding_record_raw_json"])
    stage = lambda status, reason=None, duration=0: {"status": status, "reason_code": reason, "duration_ms": duration}
    result = {"schema": "cdpd.capture-worker-result/v1", "capture_id": data["capture_id"],
              "capture_status": "PARTIAL", "failure_code": "NOT_RUN", "structure": None,
              "identity_checks": {"before": False, "after": False, "current": False, "keeper": False, "lease": False},
              "before": None, "after": None, "stages": {"S1": stage("NOT_RUN", "NOT_RUN"), "S2": stage("NOT_RUN", "NOT_RUN")},
              "sanitization_passed": False, "worker_termination": "NOT_STARTED", "command_audit": [],
              "script_sha256": script_sha256(request.max_nodes), "probe_catalog_sha256": CATALOG_SHA256, "pid": os.getpid()}
    if not data["policy_allowed"]:
        result.update(capture_status="REJECTED", failure_code="POLICY_DENIED")
        result["stages"] = {name: stage("NOT_RUN", "POLICY_DENIED") for name in ("S1", "S2")}
        return result
    rule = _worker_capture_rule(data["trigger_envelope"])
    if rule["capture_mode"] in {"NONE", "METADATA_ONLY", "GUARDED_STRUCTURE_IF_IDLE"}:
        # Scheduled idle proof is a later L08/L07 integration, never inferred
        # from caller UI context. The one-shot entry fails closed without it.
        reason = data["trigger_envelope"]["profile"]["reason"]
        codes = {"CDP_CHANNEL_LOST": "KNOWN_CDP_LOSS", "PROVIDER_DISCONNECTED": "KNOWN_CDP_LOSS",
                 "CHROME_EXITED": "KNOWN_CDP_LOSS", "CLOCK_OR_KEEPER_LOST": "CLOCK_OR_KEEPER_LOST",
                 "TARGET_IDENTITY_MISMATCH": "TARGET_BINDING_INVALID", "PAGE_SET_VIOLATION": "TARGET_BINDING_INVALID",
                 "NAVIGATION_DURING_CAPTURE": "NAVIGATION_DURING_CAPTURE", "WORKER_FAILED": "WORKER_FAILED_OR_DEADLINE",
                 "WORKER_DEADLINE": "WORKER_FAILED_OR_DEADLINE", "SPOOL_BACKPRESSURE": "SPOOL_BACKPRESSURE"}
        return metadata_worker_result(job, codes.get(reason, "NOT_RUN"))
    gate = _PermitGate(data, permit, monotonic, request.timeout_seconds)
    connection = None
    session = None
    evaluated = False
    started = monotonic()
    result["worker_termination"] = "NORMAL"
    try:
        gate.check("START")
        result["identity_checks"].update(current=True, keeper=True, lease=True)
        connection = connection_factory(request.target.endpoint, gate.remaining())
        if connection.browser_id != binding.expected_browser_id:
            raise CaptureError("BROWSER_IDENTITY_MISMATCH")

        def send(method: str, params: dict[str, Any], selected_session: str | None = None):
            gate.check("COMMAND:" + method)
            result["command_audit"].append(method)
            return connection.send(method, params, selected_session)

        def identity(phase: str):
            gate.check(phase)
            value = connection.identity()
            if type(value) is not dict or set(value) != {"browser_id", "targets"}:
                raise CaptureError("TARGET_BINDING_INVALID")
            if value["browser_id"] != binding.expected_browser_id:
                raise CaptureError("BROWSER_IDENTITY_MISMATCH")
            selected = single_page(value["targets"])
            if selected["targetId"] != binding.target_id:
                raise CaptureError("TARGET_BINDING_INVALID")
            actual = send("Target.getTargetInfo", {"targetId": binding.target_id})
            same_target(actual.get("targetInfo"), selected)
            return selected

        before_target = identity("BEFORE")
        attached = send("Target.attachToTarget", {"targetId": binding.target_id, "flatten": True})
        session = attached.get("sessionId")
        if type(session) is not str or not session or len(session) > 256:
            raise CaptureError("TARGET_BINDING_INVALID")
        before_frame = frame_snapshot(send("Page.getFrameTree", {}, session), before_target)
        result["before"] = {"frame_id": before_frame[0], "loader_id": before_frame[1]}
        result["identity_checks"]["before"] = True
        world = send("Page.createIsolatedWorld", {"frameId": before_frame[0], "worldName": "cdpd-structure-v1"}, session)
        context = world.get("executionContextId")
        if type(context) is not int or context < 1:
            raise CaptureError("STRUCTURE_EVALUATION_FAILED")
        # Refresh metadata after attach/world creation, before the sole DOM read.
        current_target = identity("PRE_CONTENT")
        current_frame = frame_snapshot(send("Page.getFrameTree", {}, session), current_target)
        if current_frame != before_frame:
            raise CaptureError("NAVIGATION_DURING_CAPTURE")
        gate.check("CONTENT")
        evaluated = True
        raw = send("Runtime.evaluate", {"expression": capture_script(request.max_nodes), "contextId": context,
                   "returnByValue": True, "awaitPromise": False, "timeout": max(1, int(gate.remaining() * 1000))}, session)
        if raw.get("exceptionDetails"):
            raise CaptureError("STRUCTURE_EVALUATION_FAILED")
        value = raw.get("result", {}).get("value")
        # The versioned fixed script can reject before touching the DOM. Accept
        # only its exact safe sentinel; arbitrary errors remain invalid payloads.
        if (type(value) is dict and set(value) == {"schema", "failure_code"}
                and type(value["schema"]) is str and value["schema"] == SCRIPT_FAILURE_SCHEMA
                and type(value["failure_code"]) is str and value["failure_code"] == "TARGET_ORIGIN_MISMATCH"):
            raise CaptureError("TARGET_ORIGIN_MISMATCH")
        if type(value) is not dict or set(value) != _RAW_STRUCTURE_KEYS:
            raise CaptureError("STRUCTURE_INVALID")
        # Strict detached copy rejects non-JSON values, NaN and excess data.
        structure = strict_json(canonical_json(value), max_bytes=2097152)
        structure["binding"] = {"target_id": binding.target_id, "browser_id": binding.expected_browser_id,
                                "origin": request.target.expected_origin, "loader_id": before_frame[1], "binding_strength": "PINNED_BROWSER"}
        structure["command_audit"] = result["command_audit"].copy()
        try:
            _validate_worker_structure(structure, maximum=request.max_nodes)
        except Exception:
            raise CaptureError("STRUCTURE_INVALID") from None
        result["sanitization_passed"] = True
        after_target = identity("AFTER")
        after_frame = frame_snapshot(send("Page.getFrameTree", {}, session), after_target)
        result["after"] = {"frame_id": after_frame[0], "loader_id": after_frame[1]}
        if before_frame != after_frame or before_target["url"] != after_target["url"]:
            raise CaptureError("NAVIGATION_DURING_CAPTURE")
        gate.check("FINAL")
        result["identity_checks"]["after"] = True
        structure["command_audit"] = result["command_audit"].copy()
        elapsed = min(300000, max(0, int((monotonic() - started) * 1000)))
        truncated = structure["truncated"]
        result["stages"] = {name: stage("PARTIAL" if truncated else "COMPLETE", "TRUNCATED" if truncated else None, elapsed if name == "S2" else 0) for name in ("S1", "S2")}
        result.update(capture_status="PARTIAL" if truncated else "COMPLETE", failure_code="TRUNCATED" if truncated else None, structure=structure)
    except Exception as error:
        code = _failure(error)
        result.update(capture_status="PARTIAL", failure_code=code, structure=None)
        if code in TERMINAL_CODES and connection is not None:
            gate.notify_terminal(code)
        if gate.failed:
            result["identity_checks"].update(current=False, keeper=False, lease=False)
        elapsed = min(300000, max(0, int((monotonic() - started) * 1000)))
        result["stages"] = {name: stage("FAILED" if evaluated else "NOT_RUN", code, elapsed if evaluated and name == "S2" else 0) for name in ("S1", "S2")}
    finally:
        if connection is not None:
            # Cleanup of this session is not an operational control disconnect.
            try:
                connection.close(session)
            except Exception:
                pass
    return result
