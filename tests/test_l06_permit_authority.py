"""L06 diagnostic permit bridge against actual accepted L04 BindingGuard.

Authority/Clock are deterministic local fixtures. No browser/provider/Preflight
or external state is used. A stored Binding alone is never an authority.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

import pytest

from chatgpt_web_api.target_guard import AuthoritySnapshot, BindingGuard, ClockSample, GuardError, ParsedBinding
from cdpd_chatgpt_profile.capture_service import L04CapturePermitAuthority
from cdpd_chatgpt_profile.worker import WorkerInputError


class Clock:
    def __init__(self):
        self.base = ClockSample(datetime(2026, 10, 24, 0, 1, tzinfo=timezone.utc), 100.0, 0)
        self.calls = 0
        self.fault_call = None
        self.fault = None

    def sample(self):
        self.calls += 1
        if self.calls == self.fault_call:
            if self.fault == 'raise':
                raise RuntimeError('SYNTHETIC_CLOCK_EXCEPTION_NOT_FOR_PERSISTENCE')
            if self.fault == 'discontinuous':
                return replace(self.base, utc=self.base.utc + timedelta(seconds=20))
            if self.fault == 'expired':
                return replace(self.base, utc=self.base.utc + timedelta(seconds=14340),
                               monotonic=self.base.monotonic + 14340)
        return self.base


class Authority:
    def __init__(self, record, clock):
        self.record, self.clock = record, clock
        self.current = AuthoritySnapshot(record.binding_id, record.record_hash, 1)
        self.invalidations = []
        self.operation_calls = []

    def snapshot(self):
        return self.current

    def invalidate(self, reason, *, binding_id=None, generation=None):
        self.invalidations.append((reason, binding_id, generation))
        if binding_id == self.current.binding_id and generation == self.current.generation:
            self.current = replace(self.current, revoked=True)

    def acquire_operation(self, token):
        self.operation_calls.append('acquire')
        raise AssertionError('L06 must not acquire a Web operation')

    def release_operation(self, token):
        self.operation_calls.append('release')
        raise AssertionError('L06 must not release a Web operation')

    def claim_operation_id(self, *args):
        self.operation_calls.append('claim')
        raise AssertionError('L06 must not claim a Web operation ID')


def setup_guard():
    record = ParsedBinding('22222222-2222-4222-8222-222222222222', 'SYNTHETIC-TARGET',
                           '254095102094427e3ef2c333da294588',
                           '2026-10-24T00:00:00Z', '2026-10-24T04:00:00Z')
    clock = Clock()
    authority = Authority(record, clock)
    guard = BindingGuard(record, authority, clock=clock)
    return guard, authority, clock


def challenge(bridge, guard):
    return {'schema': 'cdpd.capture-worker-permit-check/v1', 'phase': 'CONTENT',
            'nonce': uuid4().hex, 'capture_id': 'C-' + uuid4().hex,
            'binding_id': guard.record.binding_id, 'binding_sha256': bridge.binding_record_sha256,
            'control_generation': guard.generation, 'supervisor_instance_id': bridge.supervisor_instance_id}


def test_noncanonical_original_binding_hash_is_preserved_and_verified():
    guard, authority, _ = setup_guard()
    original_raw = json.dumps(guard.record.to_dict(), indent=2, sort_keys=False) + '\n'
    raw_hash = hashlib.sha256(original_raw.encode()).hexdigest()
    assert raw_hash != guard.record.record_hash
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()),
                                      binding_record_raw_json=original_raw)
    before = authority.snapshot()
    value = challenge(bridge, guard)
    response = bridge(value)
    assert response['allowed'] and response['binding_sha256'] == raw_hash
    assert response['reason_code'] == 'NONE' and 0 < response['remaining_lease_seconds'] <= 14340
    wrong = dict(value, binding_sha256=guard.record.record_hash)
    assert bridge(wrong)['allowed'] is False
    assert authority.snapshot() == before and not authority.operation_calls


def test_raw_binding_semantic_mismatch_and_current_identity_mismatch_deny():
    guard, authority, _ = setup_guard()
    changed = guard.record.to_dict()
    changed['target']['target_id'] = 'OTHER-TARGET'
    with pytest.raises(WorkerInputError):
        L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()),
                                  binding_record_raw_json=json.dumps(changed))
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    for key, value in [('control_generation', 2), ('supervisor_instance_id', str(uuid4())),
                       ('binding_id', str(uuid4())), ('binding_sha256', 'f' * 64)]:
        request = challenge(bridge, guard)
        request[key] = value
        assert bridge(request)['allowed'] is False
    assert not authority.operation_calls


def test_shared_revocation_stays_terminal_after_healthy_snapshot_returns():
    guard, authority, clock = setup_guard()
    peer = BindingGuard(guard.record, authority, clock=clock)
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    authority.current = replace(authority.current, revoked=True)
    assert bridge(challenge(bridge, guard))['allowed'] is False
    with pytest.raises(GuardError):
        peer.check()
    authority.current = replace(authority.current, revoked=False)
    assert bridge(challenge(bridge, guard))['allowed'] is False
    with pytest.raises(GuardError):
        peer.check()
    assert guard.state == peer.state == 'INVALIDATED'
    assert not authority.operation_calls


def test_reconciliation_mode_allows_diagnostics_but_never_mutation():
    guard, authority, _ = setup_guard()
    authority.current = replace(authority.current, mode='RECONCILIATION_REQUIRED')
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    assert bridge(challenge(bridge, guard))['allowed'] is True
    assert guard.mode == 'RECONCILIATION_REQUIRED'
    with pytest.raises(GuardError, match='RECONCILIATION_REQUIRED'):
        guard.check(mutation=True)
    authority.current = replace(authority.current, mode='NORMAL')
    assert bridge(challenge(bridge, guard))['allowed'] is True
    with pytest.raises(GuardError, match='RECONCILIATION_REQUIRED'):
        guard.check(mutation=True)
    assert not authority.operation_calls and not authority.invalidations


@pytest.mark.parametrize('fault', ['raise', 'discontinuous', 'expired'])
def test_middle_clock_sample_fault_latches_shared_guard_terminal(fault):
    guard, authority, clock = setup_guard()
    peer = BindingGuard(guard.record, authority, clock=clock)
    bridge = L04CapturePermitAuthority(guard, supervisor_instance_id=str(uuid4()))
    # First sample is guard.check; the second is the bridge's explicit sample
    # used to compute remaining lease. A later healthy sample cannot erase it.
    clock.fault_call = clock.calls + 2
    clock.fault = fault
    assert bridge(challenge(bridge, guard))['allowed'] is False
    assert guard.state in {'INVALIDATED', 'EXPIRED'}
    with pytest.raises(GuardError):
        peer.check()
    assert bridge(challenge(bridge, guard))['allowed'] is False
    assert authority.invalidations and not authority.operation_calls
