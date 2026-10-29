"""L06 CAP controls against the actual one-shot worker, with no browser/socket.

Every connection below is an in-memory fixture.  These tests exercise the
production request/permit/capture boundary without granting a live authority.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from cdpd import CaptureRequest, DiagnosticEvent, TargetBinding
from cdpd.collector import CaptureError
from cdpd_chatgpt_profile.worker import WorkerRequest, run_capture_worker
from chatgpt_web_api.target_guard import ParsedBinding


ROOT = Path(__file__).resolve().parents[1]
EPOCH = "254095102094427e3ef2c333da294588"
TARGET = "SYNTHETIC-TARGET-A"
ORIGIN = "https://chatgpt.com"
FEATURES = ("composer.input", "composer.send", "generation.stop", "attachment.input", "response.container")
READ_METHODS = {
    "Target.getTargetInfo", "Target.attachToTarget", "Target.detachFromTarget",
    "Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate",
}


def bound_record():
    return ParsedBinding(
        "22222222-2222-4222-8222-222222222222", TARGET, EPOCH,
        "2026-10-24T00:00:00Z", "2026-10-24T04:00:00Z",
    )


def request_data(*, policy=True, max_nodes=1000, timeout=10.0, loss=0, exact=True):
    record = bound_record()
    raw = record.to_bytes().decode("utf-8")
    event = DiagnosticEvent("l06-test-producer", "l06-instance-001", " caller-owned-operation ",
                            "MANUAL", "NONE", {"opaque_task_id": "caller-task"})
    envelope = {
        "schema": "cdpd.chatgpt-event-envelope/v1", "event_id": str(uuid4()),
        "producer_seq": 1, "occurred_at_utc": "2026-10-24T00:00:00Z",
        "binding_id": record.binding_id,
        "binding_sha256": hashlib.sha256(raw.encode()).hexdigest(), "control_generation": 1,
        "event": event.to_dict(),
        "profile": {"reason": "MANUAL_CAPTURE", "dispatch_state": "NOT_APPLICABLE",
                    "ui_context": "UNDETERMINED", "document_marker": None,
                    "loss_lower_bound": loss, "loss_count_exact": exact},
    }
    capture = CaptureRequest(event, TargetBinding.from_dict(record.to_dict()["target"]),
                             timeout_seconds=timeout, max_nodes=max_nodes)
    return {
        "schema": "cdpd.capture-worker-request/v1", "capture_request": capture.to_dict(),
        "trigger_envelope": envelope, "binding_record_raw_json": raw,
        "binding_record_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "control_generation": 1, "capture_id": "C-" + uuid4().hex,
        "supervisor_instance_id": str(uuid4()), "policy_allowed": policy,
    }


def node(index=0, parent=-1, *, tag="html", role=None):
    return {"index": index, "parent": parent, "tag": tag, "role": role,
            "shadowBoundary": False, "disabled": False, "hidden": False,
            "visible": True, "box": [0, 0, 200, 100]}


def valid_structure(*, truncated=False):
    return {
        "schema": "cdpd.structure/v1", "nodes": [node()], "truncated": truncated,
        "iframeCount": 0, "openShadowCount": 0,
        "coverage": {"scope": "MAIN_FRAME_AND_OPEN_SHADOW", "iframeDocuments": "NOT_CAPTURED",
                     "closedShadow": "NOT_OBSERVABLE", "accessibilityTree": "NOT_CAPTURED"},
        "observation": "STRUCTURE_ONLY_NOT_FUNCTIONAL_VERIFICATION",
        "profile": {"probes": [{"feature_id": feature, "match_count": 0, "visible_count": 0,
                                  "enabled_count": 0, "probe_complete": not truncated}
                                 for feature in FEATURES],
                    "feature_shapes": {feature: [] for feature in FEATURES}},
    }


class Permit:
    def __init__(self, transform=None):
        self.challenges = []
        self.transform = transform

    def __call__(self, challenge):
        self.challenges.append(deepcopy(challenge))
        response = {**challenge, "schema": "cdpd.capture-worker-permit/v1",
                    "allowed": True, "remaining_lease_seconds": 60.0, "reason_code": "NONE"}
        if self.transform:
            response = self.transform(response, len(self.challenges))
        return response


class Connection:
    """Exact fixed-target fake: no browser imports, socket, or page fallback."""
    def __init__(self, *, structure=None, before=None, after=None, fail_method=None):
        self.browser_id = EPOCH
        self.target_id = TARGET
        self.url = ORIGIN + "/"
        self.frame_id = "SYNTHETIC-FRAME-A"
        self.loader_id = "SYNTHETIC-LOADER-A"
        self.target_type = "page"
        self.structure = valid_structure() if structure is None else deepcopy(structure)
        self.after = after
        self.fail_method = fail_method
        self.extra_targets = []
        self.disconnected = False
        self.audit = []
        self.calls = []
        self.identity_calls = 0
        self.closed = False
        if before:
            before(self)

    def target_info(self):
        return {"targetId": self.target_id, "type": self.target_type, "url": self.url,
                "attached": True, "canAccessOpener": False}

    def identity(self):
        self.identity_calls += 1
        if self.disconnected:
            raise CaptureError("DISCONNECTED")
        return {"browser_id": self.browser_id,
                "targets": [self.target_info()] + deepcopy(self.extra_targets)}

    def remaining(self):
        return 8.0

    def send(self, method, params, session=None):
        assert method in READ_METHODS, "Unexpected browser capability: " + method
        self.audit.append(method)
        self.calls.append({"method": method, "target_id": params.get("targetId"), "session": session})
        if self.fail_method == method:
            raise RuntimeError("SYNTHETIC_SECRET_EXCEPTION_MUST_NOT_PERSIST")
        if self.disconnected:
            raise CaptureError("DISCONNECTED")
        if method == "Target.getTargetInfo":
            return {"targetInfo": self.target_info()}
        if method == "Target.attachToTarget":
            assert params["targetId"] == TARGET
            return {"sessionId": "SYNTHETIC-SESSION-A"}
        if method == "Target.detachFromTarget":
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": self.frame_id, "loaderId": self.loader_id,
                                            "url": self.url, "securityOrigin": ORIGIN}}}
        if method == "Page.createIsolatedWorld":
            assert params["frameId"] == self.frame_id
            return {"executionContextId": 11}
        if method == "Runtime.evaluate":
            result = {"result": {"value": deepcopy(self.structure)}}
            if self.after:
                self.after(self)
            return result
        raise AssertionError(method)

    def close(self, session=None):
        self.closed = True


def run(data=None, connection=None, permit=None, *, monotonic=None):
    data = request_data() if data is None else data
    connection = Connection() if connection is None else connection
    permit = Permit() if permit is None else permit
    creations = []
    def factory(endpoint, timeout):
        assert endpoint == "http://127.0.0.1:9222"
        assert 0 < timeout <= data["capture_request"]["timeout_seconds"]
        creations.append({"endpoint": endpoint, "timeout": timeout})
        return connection
    kwargs = {"permit": permit, "connection_factory": factory}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    result = run_capture_worker(WorkerRequest.from_dict(data), **kwargs)
    return result, connection, permit, creations


def observe(request, *, expected, actual, ac=("AC02",), cap=(), result=None):
    row = {"expected": expected, "actual": actual, "ac": list(ac), "cap": list(cap),
           "commands": [], "source_sha": hashlib.sha256((ROOT / "code/cdpd_chatgpt_profile/worker.py").read_bytes()).hexdigest()}
    request.node._l06_evidence = row
    request.node._l04_evidence = {**row, "evt": [], "tgt": [], "bind": []}
    target = os.environ.get("CDPD_L06_EVIDENCE_DIR")
    if target:
        directory = Path(target) / ("case_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "EXPECTED_ACTUAL.json").write_text(json.dumps({"nodeid": request.node.nodeid, **row}, indent=2) + "\n")
        if result is not None:
            (directory / "WORKER_RESULT.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


def test_cap01_complete_bound_capture(request):
    result, connection, permit, creations = run()
    assert result["capture_status"] == "COMPLETE"
    assert result["failure_code"] in (None, "NONE")
    assert result["structure"]["binding"]["target_id"] == TARGET
    assert result["structure"]["binding"]["browser_id"] == EPOCH
    assert result["structure"]["binding"]["origin"] == ORIGIN
    assert connection.audit.count("Runtime.evaluate") == 1
    assert connection.closed and len(creations) == 1
    attached = [row["target_id"] for row in connection.calls if row["method"] == "Target.attachToTarget"]
    assert attached == [TARGET]
    nonces = [row["nonce"] for row in permit.challenges]
    assert len(nonces) >= 3 and len(nonces) == len(set(nonces))
    assert set(connection.audit) <= READ_METHODS
    observe(request, expected={"capture_status": "COMPLETE", "evaluation_count": 1, "attached_targets": [TARGET], "unique_phase_nonces": True},
            actual={"capture_status": result["capture_status"], "evaluation_count": connection.audit.count("Runtime.evaluate"),
                    "attached_targets": attached, "unique_phase_nonces": len(nonces) == len(set(nonces))},
            ac=("AC01", "AC02", "AC03"), cap=("CAP-01",), result=result)


@pytest.mark.parametrize("change", ["target", "epoch", "origin", "page_type", "page_set"])
@pytest.mark.parametrize("moment", ["before", "after"])
def test_cap02_cap03_identity_change_never_keeps_wrong_dom(change, moment, request):
    def mutate(connection):
        if change == "target": connection.target_id = "SYNTHETIC-TARGET-B"
        elif change == "epoch": connection.browser_id = "a" * 32
        elif change == "origin": connection.url = "https://example.invalid/private"
        elif change == "page_type": connection.target_type = "service_worker"
        else: connection.extra_targets = [{"targetId": "SYNTHETIC-TARGET-B", "type": "page", "url": ORIGIN + "/"}]
    connection = Connection(**{moment: mutate})
    result, connection, _, creations = run(connection=connection)
    assert result["capture_status"] in ("PARTIAL", "REJECTED")
    assert result["structure"] is None
    assert connection.audit.count("Runtime.evaluate") == (0 if moment == "before" else 1)
    attached = [row["target_id"] for row in connection.calls if row["method"] == "Target.attachToTarget"]
    assert attached in ([], [TARGET]) and len(creations) == 1
    observe(request, expected={"structure_retained": False, "evaluations": 0 if moment == "before" else 1, "other_target_attached": False},
            actual={"structure_retained": result["structure"] is not None, "evaluations": connection.audit.count("Runtime.evaluate"),
                    "other_target_attached": any(target != TARGET for target in attached), "reason": result["failure_code"]},
            ac=("AC02", "AC03"), cap=("CAP-03" if change == "origin" else "CAP-02",), result=result)


@pytest.mark.parametrize("change", ["frame", "loader", "url_same_origin", "disconnect"])
def test_cap02_changed_document_or_disconnection_discards_provisional_dom(change, request):
    def mutate(connection):
        if change == "frame": connection.frame_id = "SYNTHETIC-FRAME-B"
        elif change == "loader": connection.loader_id = "SYNTHETIC-LOADER-B"
        elif change == "url_same_origin": connection.url = ORIGIN + "/different-document"
        else: connection.disconnected = True
    result, connection, _, _ = run(connection=Connection(after=mutate))
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    assert connection.audit.count("Runtime.evaluate") == 1
    observe(request, expected={"status": "PARTIAL", "structure_retained": False, "evaluations": 1},
            actual={"status": result["capture_status"], "structure_retained": result["structure"] is not None,
                    "evaluations": connection.audit.count("Runtime.evaluate"), "reason": result["failure_code"]},
            ac=("AC02", "AC03"), cap=("CAP-02", "CAP-04"), result=result)


def test_cap03_policy_denied_before_connection(request):
    result, connection, permit, creations = run(request_data(policy=False))
    assert result["capture_status"] == "REJECTED" and result["failure_code"] == "POLICY_DENIED"
    assert result["structure"] is None and not creations and not connection.calls
    observe(request, expected={"status": "REJECTED", "reason": "POLICY_DENIED", "connections": 0},
            actual={"status": result["capture_status"], "reason": result["failure_code"], "connections": len(creations)},
            ac=("AC02", "AC03"), cap=("CAP-03",), result=result)


def test_cap04_chrome_unresponsive_is_one_attempt_partial(request):
    attempts = []
    def unavailable(endpoint, timeout):
        attempts.append((endpoint, timeout))
        raise CaptureError("CDP_CONNECT_FAILED")
    result = run_capture_worker(WorkerRequest.from_dict(request_data()), permit=Permit(), connection_factory=unavailable)
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    assert result["failure_code"] == "CDP_CONNECT_FAILED" and len(attempts) == 1
    observe(request, expected={"status": "PARTIAL", "reason": "CDP_CONNECT_FAILED", "connection_attempts": 1, "structure_retained": False},
            actual={"status": result["capture_status"], "reason": result["failure_code"], "connection_attempts": len(attempts),
                    "structure_retained": result["structure"] is not None}, ac=("AC02", "AC03"), cap=("CAP-04",), result=result)


def test_cap04_collector_exception_has_only_fixed_safe_reason(request):
    result, connection, _, _ = run(connection=Connection(fail_method="Runtime.evaluate"))
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    assert "SYNTHETIC_SECRET_EXCEPTION_MUST_NOT_PERSIST" not in json.dumps(result)
    assert connection.audit.count("Runtime.evaluate") == 1
    observe(request, expected={"status": "PARTIAL", "structure_retained": False, "raw_exception_leaked": False},
            actual={"status": result["capture_status"], "structure_retained": result["structure"] is not None,
                    "raw_exception_leaked": "SYNTHETIC_SECRET_EXCEPTION_MUST_NOT_PERSIST" in json.dumps(result),
                    "reason": result["failure_code"]}, ac=("AC02", "AC03"), cap=("CAP-04",), result=result)


def test_cap01_bounded_truncation_is_partial_with_verified_structure(request):
    result, connection, _, _ = run(connection=Connection(structure=valid_structure(truncated=True)))
    assert result["capture_status"] == "PARTIAL" and result["failure_code"] == "TRUNCATED"
    assert result["structure"] is not None and result["structure"]["truncated"] is True
    observe(request, expected={"status": "PARTIAL", "reason": "TRUNCATED", "structure_retained": True},
            actual={"status": result["capture_status"], "reason": result["failure_code"], "structure_retained": result["structure"] is not None},
            ac=("AC03",), cap=("CAP-01",), result=result)
