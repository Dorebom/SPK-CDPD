"""FIX001 F-002: terminal clock-fault denial through the real L03/Session path.

The R1 probe's preflight/guard x unknown/one-shot-exception stimuli and healthy
baseline are preserved here with corrected assertions. This is EXECUTION
evidence, not an independent assessment. Clock, metadata and transport are
synthetic; the accepted L03 owner, bridge, guard and Session are real classes.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

from chatgpt_web_api.session import ChatGPTSession
from chatgpt_web_api.target_guard import BindingGuard, ClockSample, GuardError
from cdpd_chatgpt_adapter.l03_binding import L03BindingBridge
from strict_fakes import EPOCH, FakeCDPTransport, SimplePage

ORIGINAL_OPERATION = ("execute_prompt_and_wait", "FIX001-CLOCK-ORIGINAL-OPERATION")


class FaultClock:
    def __init__(self):
        self.value = ClockSample(datetime(2026, 10, 24, tzinfo=timezone.utc), 1000.0, 0)
        self.exception_once = False
        self.samples = 0

    def sample(self):
        self.samples += 1
        if self.exception_once:
            self.exception_once = False
            raise RuntimeError("SYNTHETIC_CLOCK_FAULT")
        return self.value

    def fault(self, kind):
        if kind == "unknown":
            self.value = replace(self.value, continuity_known=False)
        elif kind == "exception_once":
            self.exception_once = True
        else:
            raise AssertionError(kind)

    def normalize(self):
        self.value = replace(self.value, continuity_known=True)
        self.exception_once = False

    def advance(self, seconds):
        self.value = replace(self.value, utc=self.value.utc + timedelta(seconds=seconds),
                             monotonic=self.value.monotonic + seconds)


def denied(callback, expected):
    with pytest.raises(GuardError) as error:
        callback()
    assert error.value.code == expected
    return error.value.code


def states(env):
    return {"bridge": env.bridge.state, "bridge_reason": env.bridge.reason,
            "guard": env.guard.state, "guard_reason": env.guard.reason,
            "l03_owner": env.owner.state, "l03_owner_reason": env.owner._terminal_reason,
            "generation": env.bridge.generation}


def ledger(env):
    return [{"method": method, "operation_id": operation_id}
            for method, operation_id in sorted(env.bridge._claimed_operations)]


@pytest.fixture
def real_owner_runtime(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    path = root / "accepted_l03/code/cdpd_chatgpt_profile/preflight.py"
    name = "_fix001_real_l03_clock_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    clock = FaultClock()
    env = SimpleNamespace(module=module, clock=clock, probes=[], owner_calls=[],
                          fail_probe=False, sessions=[], transports=[])
    monkeypatch.setattr(module, "_check_runtime_dependency", lambda: None)
    monkeypatch.setattr(module, "_utc_now", lambda: clock.sample().utc)
    monkeypatch.setattr(module, "_monotonic", lambda: clock.sample().monotonic)

    def synthetic_probe(config, expected, audit):
        env.probes.append({"expected_record_present": expected is not None,
                           "injected_failure": env.fail_probe})
        if env.fail_probe:
            raise module.PreflightError("TARGET_MISSING")
        return "SYNTHETIC-TARGET-A", EPOCH, {"fixture": "SYNTHETIC_NO_OS_INSPECTION"}

    monkeypatch.setattr(module, "_probe", synthetic_probe)
    owner = module.PreflightOwner()
    original_preflight = owner.preflight

    def observed_preflight(config, **kwargs):
        result = original_preflight(config, **kwargs)
        env.owner_calls.append({"status": result["status"], "reason": result["reason_code"],
                                "audit": result["audit"]})
        return result

    monkeypatch.setattr(owner, "preflight", observed_preflight)
    env.owner = owner
    env.bridge = L03BindingBridge(owner, clock=clock)
    env.config = module.PreflightConfig("synthetic-chrome", "synthetic-profile", "synthetic-state")
    env.initial = env.bridge.preflight(env.config, policy_allowed=True, work_settled=True)
    assert env.initial["status"] == "PASS"
    env.guard = env.bridge.create_guard()
    token = object()
    env.guard.begin_operation(token)
    env.guard.claim_operation_id(*ORIGINAL_OPERATION)
    env.guard.end_operation(token)
    env.old_record = env.guard.record
    env.old_bytes = env.old_record.to_bytes()
    env.transport = FakeCDPTransport([SimplePage()])
    env.session = ChatGPTSession(transport=env.transport, binding_guard=env.guard)
    env.sessions.append(env.session)
    env.transports.append(env.transport)
    assert env.session.bind_chatgpt_page().ok
    env.cached = env.session._bound_page()
    assert env.cached is not None
    assert env.cached.ops.evaluate("() => true") == {"synthetic": True}
    assert len(env.transport.primitive_commands) == 1
    env.baseline = dict(env.session.dispatch_state)
    env.fixed_l03_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    yield env
    for session in env.sessions:
        session.close()
    env.bridge.close()


def assert_cached_denied(env, reason):
    """Read, arbitrary evaluate, and trusted mutation all stop before dispatch."""
    checks = {
        "read": denied(env.cached.ops.title, reason),
        "evaluate": denied(lambda: env.cached.ops.evaluate("() => true"), reason),
        "trusted_click": denied(lambda: env.cached.ops.trusted_click_marked_action(
            "synthetic", timeout_ms=1), reason),
    }
    assert len(env.transport.primitive_commands) == 1
    assert env.session.dispatch_state == env.baseline
    return checks


@pytest.mark.parametrize("entrypoint", ["preflight", "guard_check"])
@pytest.mark.parametrize("fault", ["unknown", "exception_once"])
def test_clock_fault_terminal_then_only_new_preflight_id_works(
        entrypoint, fault, real_owner_runtime, evidence_case):
    """Detected clock loss is terminal; only a new explicit ID admits new work."""
    env = real_owner_runtime
    # FIX002: the shared clock boundary classifies every sampling loss by the
    # approved L01 clock reason. FIX001's one generic-exception fallback was
    # BINDING_REVOKED; the original fixture is preserved in FIX001 history.
    reason = "CLOCK_DISCONTINUITY"
    before_state = states(env)
    before_ledger = ledger(env)
    env.clock.fault(fault)
    callback = (lambda: env.bridge.preflight(env.config, policy_allowed=True, work_settled=True)) \
        if entrypoint == "preflight" else (lambda: env.guard.check(mutation=True))
    initial_reason = denied(callback, reason)
    fault_state = states(env)
    assert env.bridge.state == "INVALIDATED"
    assert env.bridge._tombstones[env.old_record.binding_id] == reason
    assert len(env.probes) == len(env.owner_calls) == 1
    assert env.owner.current_record.to_bytes() == env.old_bytes
    assert len(env.transport.primitive_commands) == 1
    env.clock.normalize()
    recovery_audit_start = len(env.transport.audit)
    cached_rejections = assert_cached_denied(env, reason)
    factory_reason = denied(env.bridge.create_guard, reason)
    reconstructed_reason = denied(lambda: BindingGuard(env.old_record, env.bridge, clock=env.clock), reason)
    recovered_state = states(env)
    assert env.guard.record.to_bytes() == env.old_bytes
    assert len(env.transport.connections) == 1
    assert not env.transport.audit[recovery_audit_start:]
    assert ledger(env) == before_ledger

    # A later *explicit* successful Preflight is the only new admission. Its
    # L03 owner delegates once, and it does not reconnect any old Session.
    env.clock.advance(1)
    replacement = env.bridge.preflight(env.config, policy_allowed=True, work_settled=True)
    assert replacement["status"] == "PASS"
    new_guard = env.bridge.create_guard()
    assert new_guard.record.binding_id != env.old_record.binding_id
    assert new_guard.record.record_hash != env.old_record.record_hash
    assert new_guard.generation == env.guard.generation + 1 == 2
    assert len(env.probes) == len(env.owner_calls) == 2
    assert len(env.transport.connections) == 1
    assert_cached_denied(env, reason)
    old_after_new = denied(lambda: BindingGuard(env.old_record, env.bridge, clock=env.clock), "BINDING_REVOKED")
    assert env.bridge.state == "BOUND"
    assert env.bridge._tombstones[env.old_record.binding_id] == reason
    assert ledger(env) == before_ledger

    token = object()
    new_guard.begin_operation(token)
    duplicate_reason = denied(lambda: new_guard.claim_operation_id(*ORIGINAL_OPERATION), "RECONCILIATION_REQUIRED")
    new_guard.end_operation(token)
    assert ledger(env) == before_ledger
    new_transport = FakeCDPTransport([SimplePage()])
    new_session = ChatGPTSession(transport=new_transport, binding_guard=new_guard)
    env.sessions.append(new_session)
    env.transports.append(new_transport)
    assert new_session.bind_chatgpt_page().ok
    new_bound = new_session._bound_page()
    assert new_bound is not None
    assert new_bound.ops.evaluate("() => true") == {"synthetic": True}
    assert len(new_transport.primitive_commands) == 1
    assert len(new_transport.connections) == 1
    assert len(env.transport.primitive_commands) == 1
    assert env.owner.current_record.to_bytes() == new_guard.record.to_bytes()
    assert len(env.probes) == 2
    evidence_case(
        expected={"old_cached_primitive_dispatch_after_recovery": 0, "old_cached_mutation_dispatch_after_recovery": 0,
                  "old_guard_revival": 0, "new_generation": 2, "new_id_required": True,
                  "implicit_probe": 0, "old_session_reconnect": 0, "operation_ledger_retained": True},
        actual={"entrypoint": entrypoint, "fault": fault, "initial_reason": initial_reason,
                "states_before": before_state, "states_after_fault": fault_state,
                "states_after_clock_normalization": recovered_state, "cached_rejections": cached_rejections,
                "states_after_new_generation": {**states(env), "new_guard": new_guard.state,
                                                 "new_guard_reason": new_guard.reason},
                "factory_rejection": factory_reason, "reconstructed_old_guard_rejection": reconstructed_reason,
                "old_guard_after_new_generation": old_after_new,
                "old_guard_revival": 0,
                "old_cached_primitive_dispatch_after_recovery": len(env.transport.primitive_commands) - 1,
                "old_cached_mutation_dispatch_after_recovery": env.session.dispatch_state["mutation_dispatches"] - env.baseline["mutation_dispatches"],
                "old_record": env.old_record.to_dict(), "old_record_sha256": env.old_record.record_hash,
                "old_record_bytes_unchanged": env.guard.record.to_bytes() == env.old_bytes,
                "new_record": new_guard.record.to_dict(), "new_record_sha256": new_guard.record.record_hash,
                "new_generation": new_guard.generation, "new_guard_state": new_guard.state,
                "new_id_required": new_guard.record.binding_id != env.old_record.binding_id,
                "new_session_primitive_dispatch": len(new_transport.primitive_commands),
                "new_session_mutation_dispatch": new_session.dispatch_state["mutation_dispatches"],
                "explicit_owner_preflight_calls": len(env.owner_calls), "synthetic_probe_calls": len(env.probes),
                "old_session_connections": len(env.transport.connections), "new_session_connections": len(new_transport.connections),
                "implicit_probe": len(env.probes) - 2,
                "old_session_reconnect": len(env.transport.connections) - 1,
                "operation_ledger_retained": ledger(env) == before_ledger,
                "ledger_before": before_ledger, "ledger_after": ledger(env), "duplicate_operation_reason": duplicate_reason,
                "old_id_tombstone": env.bridge._tombstones[env.old_record.binding_id],
                "l03_owner_calls": env.owner_calls, "accepted_l03_source_sha256": env.fixed_l03_sha256},
        commands=env.transport.audit + new_transport.audit,
        ac=["AC01", "AC02", "AC04"], tgt=["TGT-10", "TGT-24", "TGT-25", "TGT-27"], bind=["BIND-02", "BIND-03"])


@pytest.mark.parametrize("precondition,expected_reason", [
    ("policy", "POLICY_DENIED"), ("work", "WORK_NOT_SETTLED"),
    ("reservation", "WORK_NOT_SETTLED"), ("concurrent_bridge_lock", "OWNER_BUSY"),
    ("unresolved_overlay", "WORK_NOT_SETTLED"),
])
@pytest.mark.parametrize("fault", ["unknown", "exception_once"])
def test_preflight_preconditions_still_refuse_before_sampling(
        precondition, expected_reason, fault, real_owner_runtime, evidence_case):
    """Precondition denial neither samples the clock nor admits/revokes a generation."""
    env = real_owner_runtime
    before_ledger = ledger(env)
    before_state = states(env)
    token = object()
    ready, release = threading.Event(), threading.Event()
    thread = None
    if precondition == "reservation":
        env.guard.begin_operation(token)
    elif precondition == "unresolved_overlay":
        env.guard.require_reconciliation()
    elif precondition == "concurrent_bridge_lock":
        def hold_lock():
            with env.bridge._lock:
                ready.set()
                assert release.wait(timeout=2)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        assert ready.wait(timeout=2)
    try:
        env.clock.fault(fault)
        sample_count = env.clock.samples
        first_reason = denied(lambda: env.bridge.preflight(
            env.config, policy_allowed=precondition != "policy",
            work_settled=precondition not in {"work", "unresolved_overlay"}), expected_reason)
        denied_sample_delta = env.clock.samples - sample_count
        assert denied_sample_delta == 0
        if fault == "exception_once":
            assert env.clock.exception_once is True
        assert len(env.probes) == len(env.owner_calls) == 1
        assert env.bridge.state == "BOUND" and env.bridge.generation == 1
        assert env.bridge.current_record.to_bytes() == env.old_bytes
        assert env.bridge._tombstones == {}
        assert len(env.transport.primitive_commands) == 1
    finally:
        env.clock.normalize()
        release.set()
        if thread is not None:
            thread.join(timeout=2)
            assert not thread.is_alive()
        if precondition == "reservation":
            env.guard.end_operation(token)
    if precondition == "unresolved_overlay":
        post_recovery_reason = denied(lambda: env.cached.ops.evaluate("() => true"), "RECONCILIATION_REQUIRED")
        assert len(env.transport.primitive_commands) == 1
        assert env.bridge._mode == "RECONCILIATION_REQUIRED"
    else:
        assert env.cached.ops.evaluate("() => true") == {"synthetic": True}
        post_recovery_reason = "ADMITTED_SAME_VALID_GENERATION"
        assert len(env.transport.primitive_commands) == 2
    assert ledger(env) == before_ledger
    evidence_case(
        expected={"reason": expected_reason, "clock_samples_during_denied_preflight": 0,
                  "explicit_owner_preflight_calls": 1, "generation": 1, "tombstones": 0},
        actual={"precondition": precondition, "fault_waiting_at_clock_boundary": fault,
                "reason": first_reason, "clock_samples_during_denied_preflight": denied_sample_delta,
                "state_before": before_state, "state": states(env),
                "old_binding_id": env.old_record.binding_id,
                "current_binding_id": env.bridge.current_record.binding_id,
                "old_record_sha256": env.old_record.record_hash,
                "old_record_bytes_unchanged": env.bridge.current_record.to_bytes() == env.old_bytes,
                "tombstones": len(env.bridge._tombstones), "synthetic_probe_calls": len(env.probes),
                "explicit_owner_preflight_calls": len(env.owner_calls),
                "ledger_before": before_ledger, "ledger_after": ledger(env),
                "after_normalization": post_recovery_reason,
                "primitive_dispatch_after_normalization": len(env.transport.primitive_commands) - 1,
                "mode": env.bridge._mode, "transport_connections": len(env.transport.connections)},
        commands=env.transport.audit, ac=["AC01", "AC02", "AC04"],
        tgt=["TGT-10", "TGT-25", "TGT-27"], bind=["BIND-02", "BIND-03"])


def test_ordinary_replacement_failure_remains_terminal(real_owner_runtime, evidence_case):
    """A normal failed replacement cannot restore old cached references or the ledger."""
    env = real_owner_runtime
    before_ledger = ledger(env)
    before_state = states(env)
    env.fail_probe = True
    failed = env.bridge.preflight(env.config, policy_allowed=True, work_settled=True)
    assert failed["status"] == "BLOCKED" and failed["reason_code"] == "TARGET_MISSING"
    assert env.bridge.state == env.owner.state == "UNBOUND"
    assert env.owner.current_record is env.bridge.current_record is None
    assert env.bridge._tombstones[env.old_record.binding_id] == "BINDING_REVOKED"
    assert len(env.owner_calls) == len(env.probes) == 2
    owner_calls_at_failure = len(env.owner_calls)
    failed_state = states(env)
    assert_cached_denied(env, "TARGET_MISSING")
    denied(env.bridge.create_guard, "TARGET_MISSING")
    denied(lambda: BindingGuard(env.old_record, env.bridge, clock=env.clock), "TARGET_MISSING")
    assert ledger(env) == before_ledger
    assert len(env.transport.connections) == 1
    env.fail_probe = False
    env.clock.advance(1)
    recovered = env.bridge.preflight(env.config, policy_allowed=True, work_settled=True)
    new_guard = env.bridge.create_guard()
    assert recovered["status"] == "PASS" and new_guard.generation == 2
    assert new_guard.record.binding_id != env.old_record.binding_id
    assert_cached_denied(env, "TARGET_MISSING")
    denied(lambda: BindingGuard(env.old_record, env.bridge, clock=env.clock), "BINDING_REVOKED")
    new_guard.check(mutation=True)
    assert ledger(env) == before_ledger
    evidence_case(
        expected={"ordinary_replacement": "BLOCKED", "old_reference_dispatch": 0,
                  "automatic_retry": 0, "new_id_required": True, "ledger_retained": True},
        actual={"ordinary_replacement": failed["status"], "failure_reason": failed["reason_code"],
                "state_before": before_state, "state_after_failed_replacement": failed_state,
                "state_after_new_generation": {**states(env), "new_guard": new_guard.state,
                                               "new_guard_reason": new_guard.reason},
                "old_reference_dispatch": len(env.transport.primitive_commands) - 1,
                "automatic_retry": owner_calls_at_failure - 2,
                "old_mutation_dispatch": env.session.dispatch_state["mutation_dispatches"] - env.baseline["mutation_dispatches"],
                "explicit_owner_preflight_calls": len(env.owner_calls), "synthetic_probe_calls": len(env.probes),
                "old_record_sha256": env.old_record.record_hash, "new_record_sha256": new_guard.record.record_hash,
                "old_id": env.old_record.binding_id, "new_id": new_guard.record.binding_id,
                "new_id_required": new_guard.record.binding_id != env.old_record.binding_id,
                "ledger_retained": ledger(env) == before_ledger,
                "new_generation": new_guard.generation, "ledger_before": before_ledger, "ledger_after": ledger(env),
                "old_session_connections": len(env.transport.connections), "l03_owner_calls": env.owner_calls},
        commands=env.transport.audit, ac=["AC01", "AC02", "AC04"],
        tgt=["TGT-04", "TGT-10", "TGT-25", "TGT-27"], bind=["BIND-02", "BIND-03"])
