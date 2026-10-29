"""FIX001 internal EXECUTION controls for the live terminal-authority bridge.

These fixtures instantiate the unchanged accepted L04 BindingGuard and current
authority model. They use synthetic state only. Passing these controls does not
close L06-F-001, accept L06, or constitute an independent Assessment.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest

from chatgpt_web_api.target_guard import AuthoritySnapshot, BindingGuard, GuardError
from cdpd_chatgpt_profile.capture_service import L04CapturePermitAuthority
from test_l06_permit_authority import Authority, Clock, challenge, setup_guard


ROOT = Path(__file__).resolve().parents[1]
TERMINAL_SCHEMA = "cdpd.capture-terminal-receipt/v1"
FIRST_REASON = "TARGET_ORIGIN_MISMATCH"


def record_observation(request, *, expected, actual, rr):
    """Store safe, explicit state observations with exact exercised sources."""
    sources = [
        "code/cdpd_chatgpt_profile/capture_service.py",
        "code/cdpd_chatgpt_profile/capture_transport.py",
        "code/cdpd_chatgpt_profile/terminal_failure.py",
        "code/cdpd_chatgpt_profile/worker.py",
        "packages/chatgpt-web-api/src/chatgpt_web_api/target_guard.py",
        "packages/chatgpt-web-api/src/chatgpt_web_api/session.py",
        "tests/test_l06_fix001_authority.py",
        "tests/test_l06_fix001_transport.py",
    ]
    row = {
        "expected": expected, "actual": actual, "ac": ["AC02"],
        "rr": list(rr), "commands": [],
        "scope": "EXECUTION_INTERNAL_QA_SYNTHETIC_ONLY",
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in sources if (ROOT / name).is_file()},
    }
    request.node._l06_evidence = row
    output = os.environ.get("CDPD_L06_EVIDENCE_DIR")
    if output:
        case = Path(output) / ("case_" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20])
        case.mkdir(parents=True, exist_ok=True)
        (case / "EXPECTED_ACTUAL.json").write_text(
            json.dumps(row, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def registered(bridge, guard, *, capture_id=None):
    value = challenge(bridge, guard)
    value["phase"] = "START"
    if capture_id is not None:
        value["capture_id"] = capture_id
    assert bridge(value)["allowed"] is True
    return value["capture_id"]


def notification(bridge, guard, capture_id, *, reason=FIRST_REASON):
    value = challenge(bridge, guard)
    value.update(phase="TERMINAL:" + reason, capture_id=capture_id)
    return value


def assert_terminal(receipt, *, status, shared_state, reason=FIRST_REASON):
    assert receipt["schema"] == TERMINAL_SCHEMA
    assert receipt["status"] == status
    assert receipt["reason_code"] == reason
    assert receipt["local_state"] == "INVALIDATED"
    assert receipt["shared_state"] == shared_state
    assert "allowed" not in receipt


@pytest.mark.parametrize("field,replacement", [
    ("schema", "cdpd.untrusted-terminal/v1"),
    ("binding_id", "33333333-3333-4333-8333-333333333333"),
    ("binding_sha256", "f" * 64),
    ("control_generation", 2),
    ("control_generation", True),
    ("supervisor_instance_id", "33333333-3333-4333-8333-333333333333"),
    ("capture_id", "C-" + "f" * 32),
    ("nonce", "not-a-valid-nonce"),
    ("nonce", True),
    ("extra_untrusted_field", True),
])
def test_terminal_notice_requires_exact_registered_scope(field, replacement, request):
    """Different schema/identity/type/capture/nonce cannot invalidate a guard."""
    guard, authority, _ = setup_guard()
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(bridge, guard)
    value = notification(bridge, guard, capture_id)
    value[field] = replacement
    before = authority.current
    receipt = bridge(value)
    assert receipt["schema"] == TERMINAL_SCHEMA and receipt["status"] == "REJECTED"
    assert guard.state == "BOUND" and authority.current == before
    assert authority.invalidations == [] and authority.operation_calls == []
    guard.check(mutation=True)
    assert bridge(challenge(bridge, guard))["allowed"] is True
    record_observation(request,
        expected={"notice": "REJECTED", "local_state": "BOUND", "shared_unchanged": True,
                  "invalidations": 0, "new_authority_side_effects": 0},
        actual={"field": field, "replacement": replacement, "notice": receipt,
                "local_state": guard.state, "shared_unchanged": authority.current == before,
                "invalidations": authority.invalidations, "operation_calls": authority.operation_calls},
        rr=("L06-RR-001", "L06-RR-003"))


def test_terminal_notice_requires_successful_start_registration(request):
    """Knowledge of binding fields alone is insufficient to report a capture."""
    guard, authority, _ = setup_guard()
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    capture_id = "C-" + uuid4().hex
    value = notification(bridge, guard, capture_id)
    rejected = bridge(value)
    assert rejected["status"] == "REJECTED"
    assert not authority.invalidations and guard.state == "BOUND"
    registered(bridge, guard, capture_id=capture_id)
    accepted = bridge(notification(bridge, guard, capture_id))
    assert_terminal(accepted, status="CONFIRMED", shared_state="REVOKED")
    assert len(authority.invalidations) == 1
    record_observation(request,
        expected={"before_start": "REJECTED", "after_start": "CONFIRMED", "invalidations": 1},
        actual={"before_start": rejected, "after_start": accepted,
                "invalidations": authority.invalidations},
        rr=("L06-RR-001", "L06-RR-003"))


@pytest.mark.parametrize("reason", ["COLLECTOR_EXCEPTION", "NAVIGATION_DURING_CAPTURE", "POLICY_DENIED"])
def test_nonterminal_reason_cannot_be_promoted_to_terminal_notification(reason, request):
    """A well-scoped envelope still cannot promote a nonterminal outcome."""
    guard, authority, _ = setup_guard()
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(bridge, guard)
    result = bridge(notification(bridge, guard, capture_id, reason=reason))
    assert result.get("status") != "CONFIRMED" and result.get("allowed") is not True
    assert guard.state == "BOUND" and authority.invalidations == []
    guard.check(mutation=True)
    record_observation(request,
        expected={"terminal_confirmation": False, "local_state": "BOUND", "invalidations": 0},
        actual={"reason": reason, "result": result, "local_state": guard.state,
                "invalidations": authority.invalidations},
        rr=("L06-RR-001", "L06-RR-005"))


def test_confirmed_terminal_notice_denies_existing_peer_and_same_generation(request):
    """Shared revocation is observed by an already-created peer guard."""
    guard, authority, clock = setup_guard()
    peer = BindingGuard(guard.record, authority, clock=clock)
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(bridge, guard)
    value = notification(bridge, guard, capture_id)
    receipt = bridge(value)
    assert_terminal(receipt, status="CONFIRMED", shared_state="REVOKED")
    scoped_call = (FIRST_REASON, guard.record.binding_id, guard.generation)
    assert authority.invalidations == [scoped_call]
    assert authority.current.revoked is True and guard.reason == FIRST_REASON
    with pytest.raises(GuardError):
        peer.check(mutation=True)
    following = challenge(bridge, guard)
    following["phase"] = "START"
    denied = bridge(following)
    assert denied["allowed"] is False and peer.state == "INVALIDATED"
    assert authority.operation_calls == []
    record_observation(request,
        expected={"receipt": "CONFIRMED", "shared": "REVOKED", "peer_mutation": "DENIED",
                  "next_capture": "DENIED", "unscoped_invalidation": 0},
        actual={"receipt": receipt, "shared": asdict(authority.current),
                "local_reason": guard.reason, "peer_state": peer.state, "next_permit": denied,
                "scoped_first_call": scoped_call, "operation_calls": authority.operation_calls},
        rr=("L06-RR-001", "L06-RR-002"))


def test_replay_is_idempotent_and_first_reason_survives_healthy_snapshot(request):
    """Replay and a later different failure cannot clear or replace first cause."""
    guard, authority, _ = setup_guard()
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(bridge, guard)
    value = notification(bridge, guard, capture_id)
    first = bridge(value)
    replay = bridge(dict(value))
    assert replay == first
    later = bridge(notification(bridge, guard, capture_id, reason="BROWSER_IDENTITY_MISMATCH"))
    assert guard.reason == FIRST_REASON
    assert later["reason_code"] == FIRST_REASON
    assert authority.invalidations == [(FIRST_REASON, guard.record.binding_id, guard.generation)]
    authority.current = replace(authority.current, state="BOUND", revoked=False)
    denied = bridge(challenge(bridge, guard))
    assert denied["allowed"] is False
    with pytest.raises(GuardError, match=FIRST_REASON):
        guard.check(mutation=True)
    assert guard.state == "INVALIDATED" and guard.reason == FIRST_REASON
    record_observation(request,
        expected={"replay_identical": True, "callback_count": 1, "first_reason": FIRST_REASON,
                  "healthy_snapshot_cannot_restore_local": True},
        actual={"first": first, "replay": replay, "later_different_failure": later,
                "invalidations": authority.invalidations, "restored_snapshot": asdict(authority.current),
                "denied_after_restore": denied, "local_state": guard.state, "local_reason": guard.reason},
        rr=("L06-RR-003", "L06-RR-004"))


class FaultAuthority(Authority):
    """Faults below the accepted guard, which intentionally swallows throws."""
    def __init__(self, record, clock):
        super().__init__(record, clock)
        self.fault = None
        self.unreadable = False

    def snapshot(self):
        if self.unreadable:
            raise RuntimeError("SYNTHETIC_SHARED_READBACK_UNAVAILABLE")
        return super().snapshot()

    def invalidate(self, reason, *, binding_id=None, generation=None):
        if self.fault in {"raise", "no_effect"}:
            self.invalidations.append((reason, binding_id, generation))
            if self.fault == "raise":
                raise RuntimeError("SYNTHETIC_SHARED_INVALIDATION_EXCEPTION")
            return
        super().invalidate(reason, binding_id=binding_id, generation=generation)
        if self.fault == "readback_unavailable":
            self.unreadable = True


@pytest.mark.parametrize("fault", ["raise", "no_effect", "readback_unavailable"])
def test_shared_notification_uncertainty_is_explicit_and_local_denial_is_sticky(fault, request):
    """Swallowed throw/no-op/unreadable ACK never produces false confirmation."""
    prototype, _, _ = setup_guard()
    clock = Clock()
    authority = FaultAuthority(prototype.record, clock)
    guard = BindingGuard(prototype.record, authority, clock=clock)
    peer = BindingGuard(prototype.record, authority, clock=clock)
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    capture_id = registered(bridge, guard)
    authority.fault = fault
    value = notification(bridge, guard, capture_id)
    receipt = bridge(value)
    assert_terminal(receipt, status="UNCONFIRMED", shared_state="UNKNOWN")
    assert guard.reason == FIRST_REASON
    assert len(authority.invalidations) == 1
    # This explicitly records the limitation of an unavailable shared callback:
    # a local latch is not evidence that an unrelated peer observed revocation.
    peer_observation = "NOT_QUERIED_READBACK_UNAVAILABLE"
    if fault != "readback_unavailable":
        peer.check(mutation=True)
        peer_observation = "BOUND_SHARED_PROPAGATION_NOT_CONFIRMED"
        assert authority.current.revoked is False
    stored_current = asdict(authority.current)
    authority.fault = None
    authority.unreadable = False
    authority.current = replace(authority.current, state="BOUND", revoked=False)
    denied = bridge(challenge(bridge, guard))
    repeated = bridge(dict(value))
    assert denied["allowed"] is False and repeated == receipt
    assert len(authority.invalidations) == 1
    with pytest.raises(GuardError, match=FIRST_REASON):
        guard.check(mutation=True)
    record_observation(request,
        expected={"notification": "UNCONFIRMED", "shared_confirmation": "UNKNOWN",
                  "local_state": "INVALIDATED", "automatic_retry": 0, "false_peer_denial_claim": False},
        actual={"fault": fault, "receipt": receipt, "shared_at_return": stored_current,
                "peer_observation": peer_observation, "denied_after_recovery": denied,
                "replay_after_recovery": repeated, "invalidations": authority.invalidations,
                "local_state": guard.state, "local_reason": guard.reason},
        rr=("L06-RR-004",))


@pytest.mark.parametrize("new_binding_id", [False, True])
def test_late_old_notification_cannot_invalidate_current_generation(new_binding_id, request):
    """Old scoped callbacks are never retargeted after explicit new issuance."""
    guard, authority, clock = setup_guard()
    old_bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    old_capture = registered(old_bridge, guard)
    late_value = notification(old_bridge, guard, old_capture)
    new_record = replace(guard.record, binding_id=str(uuid4())) if new_binding_id else guard.record
    authority.current = AuthoritySnapshot(new_record.binding_id, new_record.record_hash, 2)
    new_guard = BindingGuard(new_record, authority, clock=clock)
    new_bridge = L04CapturePermitAuthority(new_guard, supervisor_instance_id=str(uuid4()))
    new_capture = registered(new_bridge, new_guard)
    issued = authority.current
    receipt = old_bridge(late_value)
    assert_terminal(receipt, status="UNCONFIRMED", shared_state="NOT_CURRENT")
    assert authority.current == issued and not authority.current.revoked
    assert guard.state == "INVALIDATED" and new_guard.state == "BOUND"
    assert authority.invalidations == [(FIRST_REASON, guard.record.binding_id, 1)]
    # Even a directly misrouted old notification cannot mutate the new bridge.
    misrouted = new_bridge(late_value)
    assert misrouted["status"] == "REJECTED"
    new_guard.check(mutation=True)
    continued = challenge(new_bridge, new_guard)
    continued["capture_id"] = new_capture
    assert new_bridge(continued)["allowed"] is True
    assert authority.current == issued and authority.operation_calls == []
    record_observation(request,
        expected={"old_local": "INVALIDATED", "old_notification": "UNCONFIRMED",
                  "shared_relation": "NOT_CURRENT", "new_generation": 2, "new_terminal": False,
                  "old_misroute": "REJECTED", "retry_or_retarget": 0},
        actual={"new_binding_id": new_binding_id, "old_notification": receipt,
                "misrouted_old_notice": misrouted, "shared": asdict(authority.current),
                "old_local_state": guard.state, "new_local_state": new_guard.state,
                "invalidations": authority.invalidations, "operation_calls": authority.operation_calls},
        rr=("L06-RR-003", "L06-RR-005"))


def test_terminal_scope_preserves_exact_noncanonical_original_raw_hash(request):
    """Semantically equal canonical bytes cannot replace the admitted raw hash."""
    guard, authority, _ = setup_guard()
    raw = json.dumps(guard.record.to_dict(), indent=2, sort_keys=False) + "\n"
    raw_hash = hashlib.sha256(raw.encode()).hexdigest()
    assert raw_hash != guard.record.record_hash
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()),
                                      binding_record_raw_json=raw)
    capture_id = registered(bridge, guard)
    wrong = notification(bridge, guard, capture_id)
    wrong["binding_sha256"] = guard.record.record_hash
    refused = bridge(wrong)
    assert refused["status"] == "REJECTED" and authority.invalidations == []
    correct = notification(bridge, guard, capture_id)
    receipt = bridge(correct)
    assert_terminal(receipt, status="CONFIRMED", shared_state="REVOKED")
    assert receipt["binding_sha256"] == raw_hash
    record_observation(request,
        expected={"canonical_substitution": "REJECTED", "exact_original": "CONFIRMED"},
        actual={"canonical_sha256": guard.record.record_hash, "raw_sha256": raw_hash,
                "canonical_substitution": refused, "exact_original": receipt,
                "invalidations": authority.invalidations},
        rr=("L06-RR-001", "L06-RR-003"))


@pytest.mark.parametrize("event", ["CDP.disconnected", "Inspector.detached", "Target.detachedFromTarget"])
def test_known_operational_control_loss_already_invalidates_shared_l04_and_l06(event, request):
    """Accepted control-channel loss remains terminal; no reconnect or resend."""
    from test_strict_target_binding import bound_session
    session, cached = bound_session()
    guard = session._guard
    authority = session.fixture_authority
    peer = BindingGuard(guard.record, authority, clock=session.fixture_clock)
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    registered(bridge, guard)
    transport = session.fixture_transport
    connections_before = list(transport.connections)
    transport.emit(event, {})
    assert transport.is_connected()
    assert guard.state == "INVALIDATED" and guard.reason == "CDP_CONNECT_FAILED"
    assert authority.current.revoked is True
    with pytest.raises(GuardError):
        peer.check(mutation=True)
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    denied = bridge(challenge(bridge, guard))
    assert denied["allowed"] is False
    assert transport.connections == connections_before and transport.primitive_commands == []
    record_observation(request,
        expected={"channel": "ESTABLISHED_OPERATIONAL_CONTROL", "reason": "CDP_CONNECT_FAILED",
                  "shared": "REVOKED", "peer_mutation": "DENIED", "capture_permit": "DENIED",
                  "reconnect": 0, "resend": 0, "content_dispatch": 0},
        actual={"channel": "ESTABLISHED_OPERATIONAL_CONTROL", "event": event,
                "reason": guard.reason, "shared": asdict(authority.current), "peer_state": peer.state,
                "capture_permit": denied, "reconnect": len(transport.connections) - len(connections_before),
                "resend": 0, "content_dispatch": len(transport.primitive_commands),
                "synthetic_metadata_commands": transport.audit},
        rr=("L06-RR-005", "L06-RR-006", "L06-RR-008"))


def session_fixture(permit, *, on_detect=None, timeout_seconds=0.05, job_timeout_seconds=1.0):
    from cdpd_chatgpt_profile.terminal_failure import CapturePermitSession
    from test_capture_worker import request_data
    data = request_data(timeout=job_timeout_seconds)
    observations = []
    session = CapturePermitSession(data, permit, on_detect=on_detect or observations.append,
                                   timeout_seconds=timeout_seconds)
    notice = {
        "schema": "cdpd.capture-worker-permit-check/v1", "phase": "TERMINAL:" + FIRST_REASON,
        "nonce": uuid4().hex, "capture_id": data["capture_id"],
        "binding_id": data["trigger_envelope"]["binding_id"],
        "binding_sha256": data["binding_record_sha256"],
        "control_generation": data["control_generation"],
        "supervisor_instance_id": data["supervisor_instance_id"],
    }
    return session, notice, observations


def synthetic_confirmed(challenge_value):
    """Callback-protocol fixture only; does not assert actual shared revocation."""
    from cdpd_chatgpt_profile.terminal_failure import terminal_receipt
    return terminal_receipt(challenge_value, status="CONFIRMED", local_state="INVALIDATED",
                            shared_state="REVOKED")


@pytest.mark.parametrize("fault", ["exception", "unknown", "shape", "scope", "wrong_reason"])
def test_session_callback_failure_preserves_first_notice_and_never_retries(fault, request):
    """A failed or contradictory acknowledgement cannot restore a live permit."""
    from cdpd_chatgpt_profile.terminal_failure import terminal_receipt
    calls = []

    def callback(value):
        calls.append(dict(value))
        if fault == "exception":
            raise RuntimeError("SYNTHETIC_NOTIFICATION_CALLBACK_EXCEPTION")
        if fault == "unknown":
            return terminal_receipt(value)
        response = synthetic_confirmed(value)
        if fault == "shape":
            response["untrusted_extra"] = True
        elif fault == "scope":
            response["control_generation"] += 1
        elif fault == "wrong_reason":
            response["reason_code"] = "BROWSER_IDENTITY_MISMATCH"
        return response

    session, notice, observations = session_fixture(callback)
    receipt = session(notice)
    assert receipt["status"] == "UNCONFIRMED" and session.confirmed is False
    assert receipt["reason_code"] == FIRST_REASON and session.failure_code == FIRST_REASON
    assert session.first_notice == notice and observations == [notice]
    following = dict(notice, nonce=uuid4().hex, phase="CONTENT")
    denied = session(following)
    assert denied["allowed"] is False and denied["reason_code"] == FIRST_REASON
    repeated = session(dict(notice))
    assert repeated == receipt and len(calls) == 1
    finalized = session.finish()
    assert finalized == receipt
    record_observation(request,
        expected={"callback": "UNCONFIRMED", "first_reason": FIRST_REASON,
                  "callback_invocations": 1, "content_permit": "DENIED", "automatic_retry": 0},
        actual={"fault": fault, "receipt": receipt, "first_notice": session.first_notice,
                "on_detect_calls": observations, "callback_calls": calls, "next_content": denied,
                "repeated": repeated, "finalized": finalized, "durable": session.durable,
                "confirmed": session.confirmed, "scope": "SYNTHETIC_CALLBACK_PROTOCOL_ONLY"},
        rr=("L06-RR-001", "L06-RR-004"))


def test_session_timeout_is_bounded_and_late_receipt_cannot_upgrade_final_result(request):
    """After timeout, the first unknown result remains sealed after late success."""
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def delayed(value):
        calls.append(dict(value))
        entered.set()
        release.wait(2.0)
        result = synthetic_confirmed(value)
        completed.set()
        return result

    session, notice, observations = session_fixture(delayed)
    started = time.monotonic()
    try:
        receipt = session(notice)
        elapsed = time.monotonic() - started
        assert entered.is_set() and elapsed < 1.0
        assert receipt["status"] == "UNCONFIRMED" and session.confirmed is False
        finalized = session.finish()
        release.set()
        assert completed.wait(1.0)
        assert session.receipt == finalized == receipt and session.confirmed is False
        after_finish = session(dict(notice, nonce=uuid4().hex))
        assert after_finish["status"] == "REJECTED"
        denied = session(dict(notice, phase="CONTENT", nonce=uuid4().hex))
        assert denied["allowed"] is False and len(calls) == 1
    finally:
        release.set()
        completed.wait(1.0)
    record_observation(request,
        expected={"notification_timeout_seconds": 0.05, "elapsed_upper_bound_seconds": 1.0,
                  "initial_status": "UNCONFIRMED", "late_upgrade": False,
                  "callback_invocations": 1, "content_after_finish": "DENIED"},
        actual={"elapsed_seconds": elapsed, "receipt": receipt, "finalized": finalized,
                "after_late_callback": session.receipt, "callback_calls": calls,
                "after_finish": after_finish, "next_content": denied, "observations": observations,
                "scope": "SYNTHETIC_CALLBACK_PROTOCOL_ONLY"},
        rr=("L06-RR-003", "L06-RR-004", "L06-RR-007"))


def test_session_expired_deadline_retains_failure_without_starting_callback(request):
    """An expired immutable deadline permits zero external notification calls."""
    calls = []
    session, notice, observations = session_fixture(lambda value: calls.append(value),
                                                   job_timeout_seconds=0.1)
    original_deadline = session.deadline
    time.sleep(0.12)
    receipt = session(notice)
    assert receipt["status"] == "UNCONFIRMED" and session.confirmed is False
    assert calls == [] and observations == [] and session.durable is False
    assert session.deadline == original_deadline
    assert session.failure_code == FIRST_REASON
    assert session(dict(notice, phase="CONTENT", nonce=uuid4().hex))["allowed"] is False
    record_observation(request,
        expected={"deadline": "EXPIRED", "callback_invocations": 0, "result": "UNCONFIRMED",
                  "first_notice_preserved": True, "durable": False, "on_detect_calls": 0,
                  "content_permit": "DENIED"},
        actual={"receipt": receipt, "callback_calls": calls, "on_detect_calls": observations,
                "deadline_unchanged": session.deadline == original_deadline,
                "first_notice": session.first_notice, "first_reason": session.failure_code,
                "durable": session.durable},
        rr=("L06-RR-004", "L06-RR-007"))


def test_session_durable_notice_failure_cannot_be_declared_confirmed(request):
    """Shared callback confirmation cannot conceal a failed parent latch write."""
    def failed_local_record(value):
        raise OSError("SYNTHETIC_TERMINAL_LATCH_STORAGE_FAILED")

    session, notice, _ = session_fixture(synthetic_confirmed, on_detect=failed_local_record)
    receipt = session(notice)
    assert receipt["status"] == "CONFIRMED"
    assert session.durable is False and session.confirmed is False
    assert session.first_notice == notice and session.failure_code == FIRST_REASON
    denied = session(dict(notice, phase="CONTENT", nonce=uuid4().hex))
    assert denied["allowed"] is False
    record_observation(request,
        expected={"callback_status": "CONFIRMED", "parent_record_durable": False,
                  "overall_confirmed": False, "first_reason": FIRST_REASON, "content_permit": "DENIED"},
        actual={"receipt": receipt, "durable": session.durable, "confirmed": session.confirmed,
                "first_notice": session.first_notice, "content_permit": denied},
        rr=("L06-RR-004", "L06-RR-007"))


@pytest.mark.parametrize("condition", ["wrong_capture", "bool_generation", "after_finish"])
def test_session_rejects_wrong_scope_and_sealed_notices_without_callbacks(condition, request):
    """Wrong job and closed sessions cannot create or mutate the failure latch."""
    calls = []
    session, notice, observations = session_fixture(lambda value: calls.append(value))
    value = dict(notice)
    if condition == "wrong_capture":
        value["capture_id"] = "C-" + uuid4().hex
    elif condition == "bool_generation":
        value["control_generation"] = True
    else:
        assert session.finish() is None
    response = session(value)
    assert response["status"] == "REJECTED"
    assert session.first_notice is None and session.receipt is None
    assert calls == [] and observations == [] and session.confirmed is False
    record_observation(request,
        expected={"result": "REJECTED", "callback_invocations": 0, "parent_latch_invocations": 0},
        actual={"condition": condition, "result": response, "callback_calls": calls,
                "on_detect_calls": observations, "first_notice": session.first_notice,
                "recorded_receipt": session.receipt},
        rr=("L06-RR-003", "L06-RR-004", "L06-RR-007"))


def test_bounded_live_invalidation_cannot_retarget_new_binding_when_old_callback_finishes(request):
    """A real accepted guard callback remains scoped across a timed-out race."""
    from cdpd_chatgpt_profile.terminal_failure import CapturePermitSession
    from chatgpt_web_api.target_guard import ParsedBinding
    from test_capture_worker import request_data
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    observed_calls = []

    class DelayedAuthority(Authority):
        def invalidate(self, reason, *, binding_id=None, generation=None):
            observed_calls.append((reason, binding_id, generation))
            entered.set()
            if not release.wait(2.0):
                raise TimeoutError("SYNTHETIC_TEST_RELEASE_MISSING")
            return super().invalidate(reason, binding_id=binding_id, generation=generation)

    data = request_data(timeout=1.0)
    record = ParsedBinding.parse(data["binding_record_raw_json"])
    clock = Clock()
    authority = DelayedAuthority(record, clock)
    old_guard = BindingGuard(record, authority, clock=clock)
    old_bridge = L04CapturePermitAuthority(old_guard, supervisor_instance_id=data["supervisor_instance_id"],
                                          binding_record_raw_json=data["binding_record_raw_json"])
    observed_notices = []

    def callback(value):
        response = old_bridge(value)
        if value["phase"].startswith("TERMINAL:"):
            completed.set()
        return response

    session = CapturePermitSession(data, callback, on_detect=observed_notices.append, timeout_seconds=0.05)
    start = challenge(old_bridge, old_guard)
    start.update(phase="START", capture_id=data["capture_id"])
    assert session(start)["allowed"] is True
    notice = notification(old_bridge, old_guard, data["capture_id"])
    began = time.monotonic()
    try:
        receipt = session(notice)
        elapsed = time.monotonic() - began
        assert entered.is_set() and elapsed < 1.0
        assert receipt["status"] == "UNCONFIRMED" and receipt["shared_state"] == "UNKNOWN"
        assert old_guard.state == "INVALIDATED" and old_guard.reason == FIRST_REASON
        finalized = session.finish()
        new_record = replace(record, binding_id=str(uuid4()))
        authority.current = AuthoritySnapshot(new_record.binding_id, new_record.record_hash, 2)
        new_guard = BindingGuard(new_record, authority, clock=clock)
        new_bridge = L04CapturePermitAuthority(new_guard, supervisor_instance_id=str(uuid4()))
        registered(new_bridge, new_guard)
        issued = authority.current
        release.set()
        assert completed.wait(1.0)
        assert authority.current == issued and not authority.current.revoked
        new_guard.check(mutation=True)
        assert new_guard.state == "BOUND"
        assert session.receipt == finalized == receipt and session.confirmed is False
        assert observed_calls == [(FIRST_REASON, old_guard.record.binding_id, 1)]
        assert authority.invalidations == observed_calls
    finally:
        release.set()
        completed.wait(1.0)
    record_observation(request,
        expected={"old_scope": "INVALIDATED", "timeout": "UNCONFIRMED", "late_upgrade": False,
                  "new_generation": 2, "new_revoked": False, "callback_invocations": 1,
                  "retry_or_retarget": 0},
        actual={"elapsed_seconds": elapsed, "receipt": receipt, "finalized": finalized,
                "after_late_callback": session.receipt, "old_local_state": old_guard.state,
                "new_local_state": new_guard.state, "current": asdict(authority.current),
                "callback_scope": observed_calls, "observed_notices": observed_notices},
        rr=("L06-RR-001", "L06-RR-003", "L06-RR-004"))


def test_blocked_parent_latch_write_is_bounded_and_cannot_late_upgrade(request):
    """A blocked parent record writer cannot prevent bounded capture cleanup."""
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    late_records = []

    def delayed_record(value):
        entered.set()
        release.wait(2.0)
        late_records.append(dict(value))
        completed.set()

    session, notice, _ = session_fixture(synthetic_confirmed, on_detect=delayed_record)
    began = time.monotonic()
    try:
        receipt = session(notice)
        elapsed = time.monotonic() - began
        assert entered.is_set() and elapsed < 1.0
        assert session.first_notice == notice and session.failure_code == FIRST_REASON
        assert session.durable is False and session.confirmed is False
        finalized = session.finish()
        release.set()
        assert completed.wait(1.0)
        assert session.durable is False and session.confirmed is False
        assert session.receipt == finalized == receipt and late_records == [notice]
        denied = session(dict(notice, phase="CONTENT", nonce=uuid4().hex))
        assert denied["allowed"] is False
    finally:
        release.set()
        completed.wait(1.0)
    record_observation(request,
        expected={"elapsed_upper_bound_seconds": 1.0, "memory_first_notice": "PRESERVED",
                  "parent_record_durable": False, "late_upgrade": False, "content_permit": "DENIED"},
        actual={"elapsed_seconds": elapsed, "receipt": receipt, "finalized": finalized,
                "first_notice": session.first_notice, "late_records": late_records,
                "durable_after_late_write": session.durable, "confirmed": session.confirmed,
                "content_permit": denied},
        rr=("L06-RR-004", "L06-RR-007"))
