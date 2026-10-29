"""Owned one-shot Capture Worker process with a bounded dedicated pipe protocol.

The capture deadline begins before Popen and is never refreshed by a permit or
result. Only the exact owned child's PID and zero exit can authenticate a
temporary result. This is not L07 process-tree/Windows Job Object supervision.
On deadline/protocol failure, only this child is killed; bounded confirmation
may extend cleanup by 0.5s. A stuck OS spawn call cannot be preempted by Python.
A timed-out local permit callback may finish later in a daemon thread, but its
result is discarded and never authorizes capture or publication.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

from .spool import canonical_json, strict_json
from .structure_script import CATALOG_SHA256, script_sha256
from .worker import WorkerRequest, WorkerInputError, _FAILURES, _validate_structure
from .terminal_failure import TERMINAL_CODES, is_terminal, valid_receipt, terminal_receipt
from .worker_interpreter import plan_worker_launch
from .worker_cli import (MAX_REQUEST_BYTES, MAX_PERMIT_BYTES, MAX_RESULT_BYTES,
                         MAX_PERMIT_MESSAGES, PERMIT_SECONDS, PipeProtocolError,
                         _LineReader, _bounded_call, _write_line)

_METHODS = frozenset({"Target.getTargetInfo", "Target.attachToTarget", "Target.detachFromTarget",
                      "Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"})
_PHASES = ({"START", "BEFORE", "PRE_CONTENT", "CONTENT", "AFTER", "FINAL"}
           | {"COMMAND:" + x for x in _METHODS} | {"TERMINAL:" + x for x in TERMINAL_CODES})
_PERMIT_KEYS = {"schema", "phase", "nonce", "capture_id", "binding_id", "binding_sha256",
                "control_generation", "supervisor_instance_id"}
_RESULT_KEYS = {"schema", "capture_id", "capture_status", "failure_code", "structure", "identity_checks",
                "before", "after", "stages", "sanitization_passed", "worker_termination", "command_audit",
                "script_sha256", "probe_catalog_sha256", "pid"}
_STAGE_CODES = set(_FAILURES) | {None, "NOT_RUN", "TRUNCATED"}


def _valid_challenge(value: dict, data: dict, used: set[str]) -> bool:
    if type(value) is not dict or set(value) != _PERMIT_KEYS or value["schema"] != "cdpd.capture-worker-permit-check/v1":
        return False
    if value["phase"] not in _PHASES or type(value["nonce"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) or value["nonce"] in used:
        return False
    expected = {"capture_id": data["capture_id"], "binding_id": data["trigger_envelope"]["binding_id"],
                "binding_sha256": data["binding_record_sha256"], "control_generation": data["control_generation"],
                "supervisor_instance_id": data["supervisor_instance_id"]}
    return all(type(value[k]) is type(v) and value[k] == v for k, v in expected.items())


def _valid_response(response: dict, challenge: dict) -> bool:
    if is_terminal(challenge):
        return valid_receipt(response, challenge)
    if type(response) is not dict or len(response) != len(_PERMIT_KEYS) + 3 or set(response) != _PERMIT_KEYS | {"allowed", "remaining_lease_seconds", "reason_code"}:
        return False
    if response["schema"] != "cdpd.capture-worker-permit/v1" or any(response[k] != v or type(response[k]) is not type(v) for k, v in challenge.items() if k != "schema"):
        return False
    lease = response["remaining_lease_seconds"]
    return (type(response["allowed"]) is bool and type(lease) in (int, float) and math.isfinite(lease)
            and 0 <= lease <= 14400 and type(response["reason_code"]) is str and len(response["reason_code"]) <= 64
            and response["reason_code"] in (set(_FAILURES) | {"NONE"})
            and (not response["allowed"] or response["reason_code"] == "NONE"))


def _marker(value: Any) -> bool:
    return value is None or (type(value) is dict and set(value) == {"frame_id", "loader_id"}
        and all(type(v) is str and 1 <= len(v) <= 256 and not re.search(r"[\x00-\x1f\x7f]", v) for v in value.values()))


def _valid_result(value: dict, data: dict, pid: int) -> bool:
    """Closed envelope plus approved structure schema; invalid data is discarded."""
    try:
        if (type(value) is not dict or set(value) != _RESULT_KEYS or value["schema"] != "cdpd.capture-worker-result/v1"
                or value["capture_id"] != data["capture_id"] or type(value["pid"]) is not int or value["pid"] != pid
                or value["script_sha256"] != script_sha256(data["capture_request"]["max_nodes"])
                or value["probe_catalog_sha256"] != CATALOG_SHA256):
            return False
        identity = value["identity_checks"]
        if type(identity) is not dict or set(identity) != {"before", "after", "current", "keeper", "lease"} or any(type(x) is not bool for x in identity.values()):
            return False
        if (value["capture_status"] not in {"COMPLETE", "PARTIAL", "REJECTED"}
                or value["failure_code"] not in _STAGE_CODES or type(value["sanitization_passed"]) is not bool
                or value["worker_termination"] not in {"NORMAL", "NOT_STARTED"}
                or not _marker(value["before"]) or not _marker(value["after"])):
            return False
        audit = value["command_audit"]
        if type(audit) is not list or len(audit) > MAX_PERMIT_MESSAGES or any(type(x) is not str or x not in _METHODS for x in audit) or audit.count("Runtime.evaluate") > 1:
            return False
        stages = value["stages"]
        if type(stages) is not dict or set(stages) != {"S1", "S2"}:
            return False
        for stage in stages.values():
            if (type(stage) is not dict or set(stage) != {"status", "reason_code", "duration_ms"}
                    or stage["status"] not in {"NOT_RUN", "COMPLETE", "PARTIAL", "FAILED"}
                    or stage["reason_code"] not in _STAGE_CODES or type(stage["duration_ms"]) is not int
                    or not 0 <= stage["duration_ms"] <= 300000):
                return False
        structure = value["structure"]
        if structure is not None:
            _validate_structure(structure, maximum=data["capture_request"]["max_nodes"])
            from chatgpt_web_api.target_guard import ParsedBinding
            binding = ParsedBinding.parse(data["binding_record_raw_json"])
            if (not all(identity.values()) or not value["sanitization_passed"] or value["worker_termination"] != "NORMAL"
                    or value["before"] is None or value["after"] != value["before"]
                    or structure["binding"] != {"target_id": binding.target_id, "browser_id": binding.expected_browser_id,
                         "origin": data["capture_request"]["target"]["expected_origin"],
                         "loader_id": value["before"]["loader_id"], "binding_strength": "PINNED_BROWSER"}
                    or structure["command_audit"] != audit or audit.count("Runtime.evaluate") != 1):
                return False
            expected = "PARTIAL" if structure["truncated"] else "COMPLETE"
            reason = "TRUNCATED" if structure["truncated"] else None
            if value["capture_status"] != expected or value["failure_code"] != reason or any(s["status"] != expected or s["reason_code"] != reason for s in stages.values()):
                return False
        elif value["capture_status"] == "COMPLETE" or value["failure_code"] is None or any(s["status"] not in {"NOT_RUN", "FAILED"} for s in stages.values()):
            return False
        if value["capture_status"] == "REJECTED":
            return (not data["policy_allowed"] and structure is None and value["failure_code"] == "POLICY_DENIED"
                    and value["worker_termination"] == "NOT_STARTED" and not any(identity.values())
                    and not audit and value["before"] is None and value["after"] is None
                    and value["sanitization_passed"] is False
                    and all(s["status"] == "NOT_RUN" and s["reason_code"] == "POLICY_DENIED" for s in stages.values()))
        if structure is None and (value["failure_code"] not in _FAILURES or value["worker_termination"] != "NORMAL"
                or any(s["reason_code"] != value["failure_code"] for s in stages.values())):
            return False
        return data["policy_allowed"] is True
    except Exception:
        return False


def _failed_result(data: dict, *, pid: int, termination: str, original_failure: str | None = None) -> dict:
    reason = original_failure if original_failure in TERMINAL_CODES else "WORKER_FAILED_OR_DEADLINE"
    return {"schema": "cdpd.capture-worker-result/v1", "capture_id": data["capture_id"],
            "capture_status": "PARTIAL", "failure_code": reason, "structure": None,
            "identity_checks": {k: False for k in ("before", "after", "current", "keeper", "lease")},
            "before": None, "after": None, "stages": {k: {"status": "NOT_RUN", "reason_code": reason, "duration_ms": 0} for k in ("S1", "S2")},
            "sanitization_passed": False, "worker_termination": termination, "command_audit": [],
            "script_sha256": script_sha256(data["capture_request"]["max_nodes"]), "probe_catalog_sha256": CATALOG_SHA256,
            "pid": pid}


def _settle_failed_child(proc: Any, *, eof_observed: bool, deadline: float) -> str:
    """Settle an owned failed child without ever authenticating a RESULT.

    A finished pipe is not necessarily a signalled Windows process. Only
    after EOF may the parent briefly await OS-reported natural termination;
    this wait stays inside the existing capture deadline, and cannot grant a
    late result or an extension of authority. All still-live children retain
    the original kill-and-confirm fallback.
    """
    if proc is None:
        return "NOT_STARTED"
    if proc.poll() is None and eof_observed:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                proc.wait(timeout=min(0.10, remaining))
            except subprocess.TimeoutExpired:
                pass
    if proc.poll() is not None:
        return "EXITED_ERROR"
    try:
        proc.kill()
        proc.wait(timeout=0.5)
        return "KILLED"
    except Exception:
        return "UNCONFIRMED"


class ProcessWorkerRunner:
    """Callable runner; process facts are separate from the closed WorkerResult."""
    def __init__(self) -> None:
        self.last_process_metadata: dict[str, Any] = {}

    def _command(self) -> list[str]:
        return [sys.executable, "-B", "-m", "cdpd_chatgpt_profile.worker_cli"]

    def __call__(self, job: WorkerRequest, *, permit: Callable[[dict], dict]) -> dict:
        return self.run(job, permit=permit)

    def run(self, job: WorkerRequest, *, permit: Callable[[dict], dict]) -> dict:
        self.last_process_metadata = {}
        if not isinstance(job, WorkerRequest) or not callable(permit):
            raise WorkerInputError()
        data = WorkerRequest.from_dict(job.to_dict()).to_dict()
        started = time.monotonic()
        deadline = started + data["capture_request"]["timeout_seconds"]
        proc = None
        reader = None
        value = None
        reason = "NONE"
        termination = "NOT_STARTED"
        checks = 0
        terminal_cause = None
        eof_observed = False
        nonces: set[str] = set()
        metadata = {"schema": "cdpd.capture-process-metadata/v1", "spawn_count": 0, "pid": None,
                    "child_exit_code": None, "termination": "NOT_STARTED", "failure_reason": "NONE",
                    "timed_out": False, "permit_checks": 0, "stdout_bytes": 0, "stderr_policy": "DISCARD_NO_FORWARDING",
                    "command": [], "platform": sys.platform,
                    "supervision_scope": "EXACT_OWNED_CHILD_ONLY_NO_L07_PROCESS_TREE_ACCEPTANCE"}
        self.last_process_metadata = metadata
        try:
            command = self._command()
            metadata["command"] = list(command)
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            command, environment, launch_strategy = plan_worker_launch(command, environment)
            metadata["command"] = list(command)
            metadata["python_launch_strategy"] = launch_strategy
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0, close_fds=True, env=environment)
            metadata.update(spawn_count=1, pid=proc.pid)
            if time.monotonic() >= deadline:
                raise PipeProtocolError("DEADLINE")
            reader = _LineReader(proc.stdout.fileno(), maximum=MAX_RESULT_BYTES,
                                 total=MAX_RESULT_BYTES + (MAX_PERMIT_MESSAGES + 1) * (MAX_PERMIT_BYTES + 1),
                                 messages=MAX_PERMIT_MESSAGES + 1)
            _write_line(proc.stdin.fileno(), data, maximum=MAX_REQUEST_BYTES, deadline=deadline)
            while True:
                kind, raw = reader.next(deadline)
                if kind == "EOF":
                    eof_observed = True
                    if value is None:
                        raise PipeProtocolError("MISSING_RESULT")
                    break
                if value is not None:
                    raise PipeProtocolError("OUTPUT_AFTER_RESULT")
                message = strict_json(raw, max_bytes=MAX_RESULT_BYTES)
                if type(message) is not dict:
                    raise PipeProtocolError("MESSAGE_INVALID")
                if message.get("schema") == "cdpd.capture-worker-permit-check/v1":
                    if len(raw) > MAX_PERMIT_BYTES or checks >= MAX_PERMIT_MESSAGES or not _valid_challenge(message, data, nonces):
                        raise PipeProtocolError("PERMIT_INVALID")
                    nonces.add(message["nonce"])
                    checks += 1
                    if is_terminal(message):
                        terminal_cause = terminal_cause or message["phase"].removeprefix("TERMINAL:")
                        metadata.setdefault("terminal_notice", dict(message))
                        # Authenticated owned pipe + exact job scope have been
                        # checked. Record before a callback can block/timeout.
                        prepare = getattr(permit, "prepare", None)
                        if callable(prepare):
                            prepare(dict(message))
                    permit_deadline = min(deadline, time.monotonic() + PERMIT_SECONDS)
                    try:
                        response = _bounded_call(lambda: permit(dict(message)), permit_deadline)
                    except PipeProtocolError as error:
                        raise PipeProtocolError("PERMIT_TIMEOUT" if error.code == "DEADLINE" else "PERMIT_CALLBACK_FAILED") from None
                    if not _valid_response(response, message):
                        if is_terminal(message):
                            # A malformed callback ACK grants no authority. A
                            # typed UNKNOWN lets the child preserve its first
                            # failure and exit normally within the same bound.
                            response = terminal_receipt(message)
                        else:
                            raise PipeProtocolError("PERMIT_INVALID")
                    if is_terminal(message):
                        metadata["terminal_receipt"] = dict(response)
                    _write_line(proc.stdin.fileno(), response, maximum=MAX_PERMIT_BYTES, deadline=permit_deadline)
                elif not _valid_result(message, data, proc.pid):
                    raise PipeProtocolError("RESULT_INVALID")
                else:
                    if terminal_cause is not None and (message["structure"] is not None
                            or message["failure_code"] != terminal_cause):
                        raise PipeProtocolError("RESULT_INVALID")
                    value = message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PipeProtocolError("DEADLINE")
            proc.wait(timeout=remaining)
            if time.monotonic() > deadline:
                raise PipeProtocolError("DEADLINE")
            if proc.returncode != 0:
                raise PipeProtocolError("CHILD_EXITED_ERROR")
            termination = "NORMAL"
        except PipeProtocolError as error:
            reason = error.code
        except subprocess.TimeoutExpired:
            reason = "DEADLINE"
        except Exception:
            reason = "SPAWN_FAILED" if proc is None else "PROTOCOL_INVALID"
        finally:
            if reason != "NONE":
                value = None
                termination = _settle_failed_child(
                    proc, eof_observed=eof_observed, deadline=deadline)
            if reader is not None:
                reader.stop.set()
            if proc is not None:
                for pipe in (proc.stdin, proc.stdout):
                    try:
                        pipe.close()
                    except Exception:
                        pass
            metadata.update(child_exit_code=None if proc is None else proc.poll(), termination=termination,
                            failure_reason=reason, timed_out=reason in {"DEADLINE", "PERMIT_TIMEOUT"},
                            permit_checks=checks, stdout_bytes=0 if reader is None else reader.bytes_received,
                            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
        return value if value is not None else _failed_result(data, pid=0 if proc is None else proc.pid,
            termination=termination, original_failure=terminal_cause)
