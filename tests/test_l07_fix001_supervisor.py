"""FIX001 closure controls for R1 F001/F002/F004/F005.

Actual Supervisor, accepted L04 guard/permit and accepted L05 durable journal
are used. Capture integration uses the inherited in-memory CDP fixture only.
Process references are our own read-only pidfd or an explicitly created sleep
surrogate. No browser, business operation, external process or native Windows.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from chatgpt_web_api.target_guard import BindingGuard, GuardError
from cdpd_chatgpt_profile.capture_service import CaptureServiceError
from cdpd_chatgpt_profile.evidence_store import verify_profile_bundle
from cdpd_chatgpt_profile.owned_process import RegisteredProcess, process_creation_identity
from cdpd_chatgpt_profile.spool import JournalSpool, read_journal
from cdpd_chatgpt_profile.supervisor import Supervisor, SupervisorConfig, SupervisorError
from cdpd_chatgpt_profile.supervisor_authority import NonblockingRestriction
from test_binding_guard import fixture_guard, FakeAuthority, FakeClock, record
from test_capture_worker import request_data
from test_l06_capture_service import Runner, snapshot
from test_l07_supervisor_boundaries import claim_fixture, supervisor_fixture


ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / 'contracts/diagnostic_event_matrix.json').read_bytes())['rules']
INVALIDATE = [row for row in MATRIX if row['binding_effect'] == 'INVALIDATE']
KEEP_IF_PROVEN = [row for row in MATRIX if row['binding_effect'] == 'KEEP_IF_PROVEN']
RUN = 'l07-fix001-supervisor-closure'
PHASES = ['COORDINATOR_START', 'S1', 'S2', 'POSTCHECK', 'COORDINATOR_FINAL', 'COORDINATOR_COMMIT']


def evidence(request, finding, expected, actual):
    request.node._l07_evidence = {
        'expected': expected, 'actual': actual, 'finding': finding,
        'ac': ['AC01', 'AC02', 'AC04'], 'wd': [],
        'scope': 'LOCAL_SYNTHETIC_EXACT_GUARD_PERMIT_JOURNAL_NO_BROWSER',
    }


def subject(tmp_path, *, process=None, role='consumer'):
    state = tmp_path / 'state'
    for name in ('operations', 'binding_metadata'):
        (state / name).mkdir(parents=True)
    now = [1000.0]
    guard, authority, clock = fixture_guard()
    sup = Supervisor(SupervisorConfig(state, tmp_path, RUN), runner=Runner(),
                     monotonic=lambda: now[0], utcnow=lambda: clock.sample().utc)
    sup._started = True
    sup._pump = SimpleNamespace(error=None, events=queue.Queue())
    # Hold only the execution queue. The actual monitoring and permit logic run.
    sup._task = SimpleNamespace(is_alive=lambda: True)
    sup.register_binding(guard, producer_id='l06-test-producer',
                         producer_instance_id='l06-instance-001', **{role: process})
    return sup, guard, authority, now


def event(*, row=None, seq=1, kind='MANUAL', operation=None):
    value = deepcopy(request_data()['trigger_envelope'])
    value['producer_seq'] = seq
    value['event']['correlation'] = {'run_id': RUN}
    if operation is not None:
        value['event']['operation_id'] = operation
    if row is not None:
        value['event'].update(kind=row['event_kind'], failure_code=row['failure_code'])
        value['profile']['reason'] = row['profile_reason']
    elif kind == 'ACTION_STARTED':
        value['event'].update(kind=kind, failure_code='NONE')
        value['profile'].update(reason='OPERATION_ARMED', dispatch_state='NOT_DISPATCHED')
    elif kind == 'ACTION_FINISHED':
        value['event'].update(kind=kind, failure_code='NONE')
        value['profile'].update(reason='OPERATION_FINISHED', dispatch_state='CONFIRMED')
    elif kind == 'HEARTBEAT':
        value['event'].update(kind=kind, failure_code='NONE')
        value['profile'].update(reason='HEARTBEAT_SAMPLE', dispatch_state='UNKNOWN')
    return value


def durable_receive(sup, value, *, queued=False):
    with JournalSpool(sup.root / 'spool') as writer:
        receipt = writer.append(value)
    assert receipt['stage'] == 'ACK_DURABLE'
    stored = next(row['envelope'] for row in read_journal(sup.root / 'spool')['rows']
                  if row['event_id'] == value['event_id'])
    assert stored == value
    if queued:
        sup._pump.events.put((stored, sup.monotonic()))
    else:
        sup._receive(stored, sup.monotonic())
    return stored


def begin(sup, *, claimed=True):
    initial = durable_receive(sup, event(kind='MANUAL' if claimed else 'ACTION_STARTED'))
    sup.tick()
    operation = next(iter(sup._operations.values()))
    if claimed:
        assert operation.claimed
        assert operation.context.restriction.wait(.25) == 'CONFIRMED'
    return initial, operation


def challenge(job, phase):
    data = job.to_dict()
    return {'schema': 'cdpd.capture-worker-permit-check/v1', 'phase': phase,
            'nonce': uuid4().hex, 'capture_id': data['capture_id'],
            'binding_id': data['trigger_envelope']['binding_id'],
            'binding_sha256': data['binding_record_sha256'],
            'control_generation': data['control_generation'],
            'supervisor_instance_id': data['supervisor_instance_id']}


def mutation_allowed(guard):
    try:
        guard.check(mutation=True)
    except GuardError:
        return False
    return True


def queued_captures(sup):
    return sum(item[0] == 'capture' for item in sup._pending)


@pytest.mark.parametrize('row', INVALIDATE, ids=lambda row: row['case_id'])
@pytest.mark.parametrize('phase', ['BEFORE_CLAIM', *PHASES])
def test_all_matrix_invalidations_deny_every_remaining_permit(tmp_path, request, row, phase):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup, claimed=phase != 'BEFORE_CLAIM')
    job = sup._job(initial)
    permit = sup._scoped_permit(operation.context)
    if phase != 'BEFORE_CLAIM':
        for prior in PHASES[:PHASES.index(phase) + 1]:
            assert permit(challenge(job, prior))['allowed']
    failure = event(row=row, seq=2)
    durable_receive(sup, failure)
    sup.tick()
    operation.context.restriction.wait(.25)
    responses = {name: permit(challenge(job, name))['allowed'] for name in PHASES}
    # Same run/operation cannot acquire a second capture after more failures.
    durable_receive(sup, event(row=row, seq=3))
    sup.tick()
    actual = {'all_phase_permits_denied': not any(responses.values()),
              'terminal': guard._terminal, 'state': guard.state,
              'target': operation.context.restriction.target,
              'capture_count': queued_captures(sup), 'mutation_allowed': mutation_allowed(guard)}
    expected = {'all_phase_permits_denied': True, 'terminal': True, 'state': 'INVALIDATED',
                'target': 'REVOKE', 'capture_count': 1, 'mutation_allowed': False}
    evidence(request, 'L07-F-001', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('row', KEEP_IF_PROVEN, ids=lambda row: row['case_id'])
def test_keep_if_proven_rows_preserve_diagnostic_permission(tmp_path, request, row):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    durable_receive(sup, event(row=row, seq=2))
    sup.tick()
    operation.context.restriction.wait(.25)
    allowed = sup._scoped_permit(operation.context)(challenge(sup._job(initial), 'COORDINATOR_START'))['allowed']
    actual = {'diagnostic_allowed': allowed, 'terminal': guard._terminal,
              'mutation_allowed': mutation_allowed(guard), 'capture_count': queued_captures(sup)}
    expected = {'diagnostic_allowed': True, 'terminal': False,
                'mutation_allowed': False, 'capture_count': 1}
    evidence(request, 'L07-F-001', expected, actual)
    assert actual == expected


def test_matrix_scope_mismatch_cannot_invalidate_registered_generation(tmp_path, request):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    foreign = event(row=INVALIDATE[0], seq=2)
    foreign['control_generation'] = 2
    with pytest.raises(SupervisorError, match='SCOPE_MISMATCH'):
        sup._receive(foreign, now[0])
    actual = {'terminal': guard._terminal, 'target': operation.context.restriction.target}
    expected = {'terminal': False, 'target': 'MODE'}
    evidence(request, 'L07-F-001', expected, actual)
    assert actual == expected


def test_invalidation_latch_survives_operation_capacity_rejection(tmp_path, request):
    sup, guard, authority, now = subject(tmp_path)
    sup.config = replace(sup.config, max_operations=1)
    begin(sup, claimed=False)
    failure = event(row=INVALIDATE[0], seq=2, operation='another-operation')
    with pytest.raises(SupervisorError, match='SPOOL_BACKPRESSURE'):
        durable_receive(sup, failure)
    context = next(iter(sup._contexts.values()))
    actual = {'terminal': guard._terminal, 'target': context.restriction.target,
              'operations': len(sup._operations)}
    expected = {'terminal': True, 'target': 'REVOKE', 'operations': 1}
    evidence(request, 'L07-F-001', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('row', INVALIDATE, ids=lambda row: row['case_id'])
@pytest.mark.parametrize('phase', ['COORDINATOR_FINAL', 'COORDINATOR_COMMIT'])
def test_actual_finalization_cannot_commit_structure_after_invalidation(tmp_path, request, row, phase):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    job = sup._job(initial)
    scoped = sup._scoped_permit(operation.context)
    injected = []

    def permit(value):
        if value['phase'] == phase and not injected:
            injected.append(value['phase'])
            durable_receive(sup, event(row=row, seq=2))
            sup.tick()
        return scoped(value)

    coordinator = sup._coordinator(permit)
    outcome = None
    failure = None
    try:
        outcome = coordinator.capture(job)
    except CaptureServiceError as error:
        failure = error.code
    assert injected == [phase]
    capture_path = sup.root / 'bundles' / job.to_dict()['capture_id']
    if phase == 'COORDINATOR_FINAL':
        assert outcome.failure_code == 'CLOCK_OR_KEEPER_LOST'
        assert verify_profile_bundle(outcome.bundle_path)['integrity_valid']
    else:
        assert failure == 'PUBLISH_GUARD_DENIED'
        # The accepted store retains unpublished metadata for investigation;
        # rejection must remove structure and COMMIT, not erase that evidence.
        assert not (capture_path / 'COMMIT.json').exists()
        assert verify_profile_bundle(capture_path)['publication_state'] == 'INCOMPLETE'
    actual = {'structure_committed': (capture_path / 'STRUCTURE.json').exists(),
              'runner_calls': len(sup.runner.calls), 'terminal': guard._terminal,
              'queued_captures': queued_captures(sup)}
    expected = {'structure_committed': False, 'runner_calls': 1, 'terminal': True,
                'queued_captures': 1}
    evidence(request, 'L07-F-001', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('row', INVALIDATE, ids=lambda row: row['case_id'])
def test_late_invalidation_keeps_existing_commit_and_terminal_first_cause(tmp_path, request, row):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    job = sup._job(initial)
    outcome = sup._coordinator(sup._scoped_permit(operation.context)).capture(job)
    bundle = Path(outcome.bundle_path)
    assert (bundle / 'STRUCTURE.json').is_file()
    before = snapshot(bundle)
    guard.invalidate('BINDING_EXPIRED')
    durable_receive(sup, event(row=row, seq=2))
    sup.tick()
    actual = {'committed_hashes_unchanged': before == snapshot(bundle),
              'first_cause': guard.reason, 'state': guard.state,
              'runner_calls': len(sup.runner.calls), 'queued_captures': queued_captures(sup)}
    expected = {'committed_hashes_unchanged': True, 'first_cause': 'BINDING_EXPIRED',
                'state': 'EXPIRED', 'runner_calls': 1, 'queued_captures': 1}
    evidence(request, 'L07-F-001', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('role', ['consumer', 'chrome'])
@pytest.mark.parametrize('claimed', [False, True], ids=['unclaimed', 'claimed'])
@pytest.mark.parametrize('observation', ['closed_reference', 'confirmed_exit'])
def test_process_observation_loss_and_death_keep_distinct_effects(tmp_path, request, role, claimed, observation):
    child = None
    ref = None
    try:
        if observation == 'closed_reference':
            pid = os.getpid()
        else:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            pid = child.pid
        ref = RegisteredProcess.open(pid, process_creation_identity(pid))
        sup, guard, authority, now = subject(tmp_path, process=ref, role=role)
        initial, operation = begin(sup, claimed=claimed)
        job = sup._job(initial)
        permit = sup._scoped_permit(operation.context)
        if claimed:
            assert permit(challenge(job, 'COORDINATOR_START'))['allowed']
        if observation == 'closed_reference':
            ref.close()
        else:
            child.terminate()
            child.wait(timeout=3)
        sup.tick()
        operation.context.restriction.wait(.25)
        reason = 'CLOCK_OR_KEEPER_LOST' if observation == 'closed_reference' else role.upper() + '_EXITED'
        selected = (sup._pending[-1][1]['profile']['reason'] if claimed
                    else sup.observations[-1]['reason'])
        terminal = observation == 'closed_reference' or role == 'chrome'
        responses = {phase: permit(challenge(job, phase))['allowed'] for phase in PHASES}
        sup.tick()
        actual = {'reason': selected, 'terminal': guard._terminal,
                  'all_permits_denied': not any(responses.values()),
                  'diagnostic_only': guard.mode == 'DIAGNOSTIC_ONLY',
                  'capture_count': queued_captures(sup), 'mutation_allowed': mutation_allowed(guard)}
        expected = {'reason': reason, 'terminal': terminal, 'all_permits_denied': terminal,
                    'diagnostic_only': True, 'capture_count': 1, 'mutation_allowed': False}
        evidence(request, 'L07-F-002', expected, actual)
        assert actual == expected
    finally:
        if ref is not None:
            ref.close()
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


@pytest.mark.parametrize('phase', ['COORDINATOR_FINAL', 'COORDINATOR_COMMIT', 'AFTER_COMMIT'])
def test_keeper_loss_reaches_actual_commit_boundary(tmp_path, request, phase):
    ref = RegisteredProcess.open(os.getpid(), process_creation_identity(os.getpid()))
    try:
        sup, guard, authority, now = subject(tmp_path, process=ref)
        initial, operation = begin(sup)
        job = sup._job(initial)
        scoped = sup._scoped_permit(operation.context)
        injections = []

        def lose_reference():
            ref.close()
            sup.tick()
            injections.append('closed_reference')

        def permit(value):
            if value['phase'] == phase and not injections:
                lose_reference()
            return scoped(value)

        coordinator = sup._coordinator(permit)
        outcome = None
        failure = None
        try:
            outcome = coordinator.capture(job)
        except CaptureServiceError as error:
            failure = error.code
        bundle = sup.root / 'bundles' / job.to_dict()['capture_id']
        before = snapshot(bundle) if phase == 'AFTER_COMMIT' else None
        if phase == 'AFTER_COMMIT':
            assert (bundle / 'STRUCTURE.json').is_file()
            lose_reference()
            assert before == snapshot(bundle)
        elif phase == 'COORDINATOR_FINAL':
            assert outcome.failure_code == 'CLOCK_OR_KEEPER_LOST'
            assert verify_profile_bundle(bundle)['integrity_valid']
        else:
            assert failure == 'PUBLISH_GUARD_DENIED'
            assert not (bundle / 'COMMIT.json').exists()
            assert verify_profile_bundle(bundle)['publication_state'] == 'INCOMPLETE'
        actual = {'terminal': guard._terminal, 'one_capture_only': len(sup.runner.calls) == 1,
                  'new_structure_after_loss': phase != 'AFTER_COMMIT' and (bundle / 'STRUCTURE.json').exists(),
                  'first_claim_preserved': queued_captures(sup) == 1,
                  'keeper_metadata_queued': any(item[0] == 'record_only' and
                    item[1]['profile']['reason'] == 'CLOCK_OR_KEEPER_LOST' for item in sup._pending)}
        expected = {'terminal': True, 'one_capture_only': True, 'new_structure_after_loss': False,
                    'first_claim_preserved': True, 'keeper_metadata_queued': True}
        evidence(request, 'L07-F-002', expected, actual)
        assert actual == expected
    finally:
        ref.close()


@pytest.mark.parametrize('when', [1009.99, 1010.0, 1010.1], ids=['before', 'equal', 'after'])
@pytest.mark.parametrize('order', [('ACTION_FINISHED',), ('HEARTBEAT', 'ACTION_FINISHED'),
                                  ('ACTION_FINISHED', 'HEARTBEAT')],
                         ids=['finish', 'heartbeat_then_finish', 'finish_then_heartbeat'])
def test_finish_and_heartbeat_order_cannot_hide_expiry(tmp_path, request, when, order):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup, claimed=False)
    now[0] = 1009.9
    sup.tick()
    now[0] = when
    for seq, kind in enumerate(order, 2):
        durable_receive(sup, event(kind=kind, seq=seq), queued=True)
    sup.tick()
    expired = when >= 1010.0
    # A finish accepted before expiry stays disarmed; later HB never invents
    # expiry. A real expiry stays restricted after fresh heartbeat/finish.
    now[0] = 1020.0
    durable_receive(sup, event(kind='HEARTBEAT', seq=10), queued=True)
    durable_receive(sup, event(kind='ACTION_FINISHED', seq=11), queued=True)
    sup.tick()
    actual = {'finished': operation.finished, 'claimed': operation.claimed,
              'expiry_observed': any(row['reason'] == 'HEARTBEAT_EXPIRED' for row in sup.observations),
              'mutation_allowed': mutation_allowed(guard), 'capture_count': queued_captures(sup),
              'tracker_diagnostic_only': operation.tracker.diagnostic_only}
    expected = {'finished': True, 'claimed': expired, 'expiry_observed': expired,
                'mutation_allowed': not expired, 'capture_count': int(expired),
                'tracker_diagnostic_only': expired}
    evidence(request, 'L07-F-004', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('error', ['STORAGE_FAILED', 'STORAGE_UNAVAILABLE'])
@pytest.mark.parametrize('state', ['unstarted', 'active', 'claimed'])
@pytest.mark.parametrize('callback', ['confirmed', 'raises', 'blocked'])
def test_permanent_writer_error_restricts_registered_local_guard(tmp_path, request, error, state, callback):
    sup, guard, authority, now = subject(tmp_path)
    context = next(iter(sup._contexts.values()))
    context = replace(context, restriction=NonblockingRestriction(guard, timeout_seconds=.02))
    sup._contexts[next(iter(sup._contexts))] = context
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = authority.set_mode

    def notify(*args, **kwargs):
        entered.set()
        try:
            if callback == 'raises':
                raise RuntimeError('SYNTHETIC_SHARED_AUTHORITY_UNAVAILABLE')
            if callback == 'blocked':
                release.wait(3)
            original(*args, **kwargs)
        finally:
            completed.set()

    authority.set_mode = notify
    try:
        if state != 'unstarted':
            durable_receive(sup, event(kind='MANUAL' if state == 'claimed' else 'ACTION_STARTED'))
            sup.tick()
        sup._pump.error = error
        sup.tick()
        # The tick returned while the callback can still be blocked.
        assert entered.wait(.2)
        assert not mutation_allowed(guard)
        status = context.restriction.wait(.25)
        expected_status = 'CONFIRMED' if callback == 'confirmed' else 'UNCONFIRMED'
        assert status == expected_status
        release.set()
        assert completed.wait(.3)
        sup.tick()
        actual = {'guard_mode': guard.mode, 'mutation_allowed': mutation_allowed(guard),
                  'worker_blocked': sup.worker_blocked, 'supervisor_diagnostic_only': sup.diagnostic_only,
                  'notification_attempts': context.restriction.notification_attempts,
                  'shared_status_after_late_completion': context.restriction.status}
        expected = {'guard_mode': 'DIAGNOSTIC_ONLY', 'mutation_allowed': False,
                    'worker_blocked': True, 'supervisor_diagnostic_only': True,
                    'notification_attempts': 1, 'shared_status_after_late_completion': expected_status}
        evidence(request, 'L07-F-005', expected, actual)
        assert actual == expected
    finally:
        release.set()


@pytest.mark.parametrize('error', ['STORAGE_FAILED', 'STORAGE_UNAVAILABLE'])
def test_writer_failure_restricts_all_existing_and_later_registered_guards(tmp_path, request, error):
    sup, first, authority, now = subject(tmp_path)
    guards = [first]
    for binding in ['33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444']:
        rec, clock = record(binding_id=binding), FakeClock()
        guard = BindingGuard(rec, FakeAuthority(rec), clock=clock)
        sup.register_binding(guard, producer_id='other-producer', producer_instance_id=binding)
        guards.append(guard)
    sup._pump.error = error
    sup.tick()
    # The permanent latch survives an accidentally cleared volatile error flag.
    sup._pump.error = None
    rec, clock = record(binding_id='55555555-5555-4555-8555-555555555555'), FakeClock()
    later = BindingGuard(rec, FakeAuthority(rec), clock=clock)
    sup.register_binding(later, producer_id='later-producer', producer_instance_id='later-instance')
    guards.append(later)
    actual = {'registered_count': len(guards), 'all_local_modes_restricted': all(g.mode == 'DIAGNOSTIC_ONLY' for g in guards),
              'any_mutation_allowed': any(mutation_allowed(g) for g in guards),
              'worker_blocked': sup.worker_blocked}
    expected = {'registered_count': 4, 'all_local_modes_restricted': True,
                'any_mutation_allowed': False, 'worker_blocked': True}
    evidence(request, 'L07-F-005', expected, actual)
    assert actual == expected


def test_writer_failure_preserves_reconciliation_and_original_terminal_cause(tmp_path, request):
    sup, guard, authority, now = subject(tmp_path)
    guard.require_reconciliation()
    guard.invalidate('BINDING_EXPIRED')
    sup._pump.error = 'STORAGE_FAILED'
    sup.tick()
    actual = {'mode': guard.mode, 'reason': guard.reason, 'state': guard.state}
    expected = {'mode': 'RECONCILIATION_REQUIRED', 'reason': 'BINDING_EXPIRED', 'state': 'EXPIRED'}
    evidence(request, 'L07-F-005', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('error', ['STORAGE_FAILED', 'STORAGE_UNAVAILABLE'])
def test_late_registration_is_restricted_before_failing_metadata_write(tmp_path, request, monkeypatch, error):
    sup, guard, authority, now = subject(tmp_path)
    sup._pump.error = error
    sup.tick()
    rec, clock = record(binding_id='66666666-6666-4666-8666-666666666666'), FakeClock()
    later = BindingGuard(rec, FakeAuthority(rec), clock=clock)
    observed_modes = []

    def failed_write(*args, **kwargs):
        observed_modes.append(later.mode)
        raise OSError('SYNTHETIC_METADATA_STORAGE_FAILED')

    monkeypatch.setattr('cdpd_chatgpt_profile.supervisor._write_new', failed_write)
    with pytest.raises(OSError):
        sup.register_binding(later, producer_id='later-producer', producer_instance_id='later-instance')
    actual = {'mode_at_io_boundary': observed_modes, 'mutation_allowed': mutation_allowed(later),
              'registered_count': len(sup._contexts), 'worker_blocked': sup.worker_blocked}
    expected = {'mode_at_io_boundary': ['DIAGNOSTIC_ONLY'], 'mutation_allowed': False,
                'registered_count': 1, 'worker_blocked': True}
    evidence(request, 'L07-F-005', expected, actual)
    assert actual == expected


@pytest.mark.parametrize('malformation', ['boolean_flag', 'missing_field', 'negative_count',
    'boolean_count', 'missing_confirmed', 'extra_field', 'false_confirmation', 'false_uncertainty'])
def test_restart_rejects_malformed_resource_cleanup_facts(tmp_path, request, malformation):
    sup = supervisor_fixture(tmp_path)
    directory, outcome = claim_fixture(sup)
    process = outcome['process_metadata']
    process['resource_cleanup_unconfirmed'] = False
    process['resource_cleanup'] = {'confirmed': True, 'pending_handle_count': 0,
        'pending_attribute_list_count': 0, 'pending_stream_count': 0}
    resources = process['resource_cleanup']
    if malformation == 'boolean_flag':
        process['resource_cleanup_unconfirmed'] = 1
    elif malformation == 'missing_field':
        resources.pop('pending_stream_count')
    elif malformation == 'negative_count':
        resources['pending_handle_count'] = -1
    elif malformation == 'boolean_count':
        resources['pending_handle_count'] = False
    elif malformation == 'missing_confirmed':
        resources.pop('confirmed')
    elif malformation == 'extra_field':
        resources['other'] = 0
    elif malformation == 'false_confirmation':
        resources['pending_handle_count'] = 1
    else:
        resources['confirmed'] = False
    (directory / 'OUTCOME.json').write_text(json.dumps(outcome))
    with pytest.raises(SupervisorError, match='INPUT_INVALID'):
        sup._load_records()
    evidence(request, 'L07-F-003', {'restart_rejected': True}, {'restart_rejected': True})


@pytest.mark.parametrize('pending_field', ['pending_handle_count', 'pending_attribute_list_count', 'pending_stream_count'])
def test_restart_blocks_resources_without_falsifying_confirmed_process_exit(tmp_path, request, pending_field):
    sup = supervisor_fixture(tmp_path)
    directory, outcome = claim_fixture(sup)
    process = outcome['process_metadata']
    process.update(spawn_attempt_count=1, spawn_count=1, pid=12345, termination='KILLED',
        termination_unconfirmed=False, resource_cleanup_unconfirmed=False,
        new_worker_prohibited=False,
        termination_facts={'requested': True, 'confirmed': True, 'reason': 'CONFIRMED',
                           'exit_code': 1, 'active_processes': 0, 'native_error': None},
        resource_cleanup={'confirmed': False, 'pending_handle_count': 0,
                          'pending_attribute_list_count': 0, 'pending_stream_count': 0})
    process['resource_cleanup'][pending_field] = 1
    (directory / 'OUTCOME.json').write_text(json.dumps(outcome))
    sup._load_records()
    actual = {'worker_blocked': sup.worker_blocked, 'reason': sup._block_reason,
              'saved_lifetime_confirmed': json.loads((directory / 'OUTCOME.json').read_text())
                  ['process_metadata']['termination_facts']['confirmed'],
              'replay_queued': bool(sup._pending)}
    expected = {'worker_blocked': True, 'reason': 'WORKER_RESOURCE_CLEANUP_UNCONFIRMED',
                'saved_lifetime_confirmed': True, 'replay_queued': False}
    evidence(request, 'L07-F-003', expected, actual)
    assert actual == expected


def test_runner_resource_block_denies_permits_without_lifetime_unknown(tmp_path, request):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    permit = sup._scoped_permit(operation.context)
    job = sup._job(initial)
    assert permit(challenge(job, 'COORDINATOR_START'))['allowed']
    sup.runner.termination_unconfirmed = False
    sup.runner.resource_cleanup_unconfirmed = True
    actual = {'worker_blocked': sup.worker_blocked,
              'permit_allowed': permit(challenge(job, 'COORDINATOR_COMMIT'))['allowed'],
              'lifetime_unknown': sup.runner.termination_unconfirmed}
    expected = {'worker_blocked': True, 'permit_allowed': False, 'lifetime_unknown': False}
    evidence(request, 'L07-F-003', expected, actual)
    assert actual == expected
