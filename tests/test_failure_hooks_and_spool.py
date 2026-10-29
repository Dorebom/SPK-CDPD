"""L05 hook tests use synthetic providers and local spool fixtures only."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cdpd import DiagnosticEvent
from cdpd_chatgpt_adapter import InstrumentedFacade
from cdpd_chatgpt_profile.failure_hooks import BindingContext, FailureHooks, HookLimits
from cdpd_chatgpt_profile.spool import FileMailbox, JournalSpool, canonical_json, read_history, read_journal, stream_key
from chatgpt_web_api import api, generation
from chatgpt_web_api.target_guard import GuardError
from test_facade_no_double_send import Provider, call
from test_fix001_postcheck import operation_fixture

PRODUCER, INSTANCE, OPERATION = 'synthetic-l05-producer', 'synthetic-l05-instance', ' original-l05-operation '


def event(kind='ACTION_FINISHED', failure='NONE', operation_id=OPERATION, correlation=None):
    return DiagnosticEvent(PRODUCER, INSTANCE, operation_id, kind, failure_code=failure, correlation=correlation or {})


def accepted(envelope):
    return {'stage': 'LOCAL_PUBLISHED', 'ack_purpose': 'EVENT_DURABILITY'}


def hooks(publisher=accepted, **kwargs):
    return FailureHooks(publisher, producer_id=PRODUCER, producer_instance_id=INSTANCE, **kwargs)


def wrapped(provider, sink):
    return InstrumentedFacade(provider, sink, producer_id=PRODUCER, producer_instance_id=INSTANCE,
                              correlation={'trace_id': 'opaque-01'})


def evidence(recorder, expected, actual, *, ac=('AC02',), evt=('EVT-03',)):
    recorder(expected=expected, actual=actual, ac=ac, evt=evt)
    for key, value in expected.items():
        assert actual[key] == value, key


def preserve(path, label):
    destination = os.environ.get('CDPD_L05_EVIDENCE_DIR')
    if destination:
        target = Path(destination) / label
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(path, target, dirs_exist_ok=True)


def test_five_event_kinds_wire_to_fixed_profile_reasons(evidence_case):
    captured = []
    sink = hooks(lambda envelope: (captured.append(envelope), accepted(envelope))[1])
    for kind, failure in [('ACTION_STARTED', 'NONE'), ('ACTION_FINISHED', 'NONE'),
                          ('ACTION_FAILED', 'UNKNOWN'), ('OBSERVATION_FAILED', 'UNKNOWN')]:
        assert sink.try_emit(event(kind, failure))
    assert sink.heartbeat(operation_id=OPERATION)
    assert sink.drain_once() == 5
    mapping = {row['event']['kind']: row['profile']['reason'] for row in captured}
    evidence(evidence_case,
             {'mapping': {'ACTION_STARTED': 'OPERATION_ARMED', 'ACTION_FINISHED': 'OPERATION_FINISHED',
                          'ACTION_FAILED': 'PROVIDER_FAILURE_UNCLASSIFIED',
                          'OBSERVATION_FAILED': 'OBSERVATION_UNDETERMINED', 'HEARTBEAT': 'HEARTBEAT_SAMPLE'},
              'ids_unchanged': True},
             {'mapping': mapping, 'ids_unchanged': all(r['event']['operation_id'] == OPERATION for r in captured)},
             ac=('AC01', 'AC03'), evt=('EVT-01',))


def test_queue_full_keeps_critical_reserve_and_next_loss_snapshot(evidence_case):
    sink = hooks(limits=HookLimits(queue_total=4, queue_critical_reserved=1))
    results = [sink.try_emit(event()) for _ in range(4)]
    critical = sink.try_emit(event('ACTION_FAILED', 'UNKNOWN'))
    full_critical = sink.try_emit(event('OBSERVATION_FAILED', 'UNKNOWN'))
    assert sink.drain_once() == 4
    assert sink.try_emit(event())
    history = sink.history()
    last = history['events'][-1]
    evidence(evidence_case,
             {'normal': [True, True, True, False], 'critical': True, 'full_critical': False,
              'drop_count': 2, 'last_seq': 7, 'last_loss': 2, 'complete': False},
             {'normal': results, 'critical': critical, 'full_critical': full_critical,
              'drop_count': sink.stats['queue_dropped'], 'last_seq': last['producer_seq'],
              'last_loss': last['profile']['loss_lower_bound'], 'complete': history['history_coverage']['complete']})


def test_heartbeat_coalesces_without_using_normal_queue_or_loss_counter(evidence_case):
    captured = []
    sink = hooks(lambda row: (captured.append(row), accepted(row))[1],
                 limits=HookLimits(queue_total=2, queue_critical_reserved=0))
    assert sink.try_emit(event()) and sink.try_emit(event())
    for _ in range(6):
        assert sink.heartbeat(operation_id=OPERATION)
    before = sink.stats
    assert sink.drain_once() == 3
    diagnostics = [row['producer_seq'] for row in captured if row['event']['kind'] != 'HEARTBEAT']
    samples = [row for row in captured if row['event']['kind'] == 'HEARTBEAT']
    evidence(evidence_case,
             {'queue_depth': 2, 'coalesced': 5, 'sample_seq': 6, 'diagnostics': [1, 2], 'sample_loss': 0, 'gap': 0},
             {'queue_depth': before['queue_depth'], 'coalesced': before['heartbeat_coalesced'],
              'sample_seq': samples[0]['producer_seq'], 'diagnostics': diagnostics,
              'sample_loss': samples[0]['profile']['loss_lower_bound'], 'gap': sink.history()['diagnostic_internal_gap']})


def test_sequence_and_cumulative_loss_survive_window_and_binding_roundtrip(evidence_case):
    clock = [0.0]
    first, second = BindingContext(str(uuid4()), 'a'*64, 1), BindingContext(str(uuid4()), 'b'*64, 2)
    sink = hooks(binding=first, monotonic=lambda: clock[0], limits=HookLimits(queue_total=1, queue_critical_reserved=0))
    assert sink.try_emit(event()) and not sink.try_emit(event())
    sink.drain_once()
    clock[0] = 121.0
    assert sink.try_emit(event())
    old = sink.history()
    sink.drain_once()
    sink.set_binding(second)
    assert sink.try_emit(event())
    new = sink.history()
    sink.drain_once()
    sink.set_binding(first)
    assert sink.try_emit(event())
    returned = sink.history()
    evidence(evidence_case,
             {'old_seq': 3, 'old_loss': 1, 'old_complete': False, 'new_seq': 4, 'new_loss': 0, 'returned_seq': 5, 'returned_loss': 1},
             {'old_seq': old['events'][-1]['producer_seq'], 'old_loss': old['history_dropped_count'],
              'old_complete': old['history_coverage']['complete'], 'new_seq': new['events'][-1]['producer_seq'],
              'new_loss': new['history_dropped_count'], 'returned_seq': returned['events'][-1]['producer_seq'],
              'returned_loss': returned['history_dropped_count']})


def test_history_count_and_byte_truncation_remain_explicit(evidence_case):
    count_sink = hooks(limits=HookLimits(history_max_events=2))
    for _ in range(3):
        assert count_sink.try_emit(event())
    byte_size = len(canonical_json(count_sink.history()['events'][-1]))
    byte_sink = hooks(limits=HookLimits(history_max_bytes=byte_size + 20))
    assert byte_sink.try_emit(event()) and byte_sink.try_emit(event())
    count_history, byte_history = count_sink.history(), byte_sink.history()
    evidence(evidence_case,
             {'count_retained': 2, 'count_sampled': True, 'count_lower': 1, 'byte_retained': 1, 'byte_sampled': True, 'byte_complete': False},
             {'count_retained': len(count_history['events']), 'count_sampled': count_history['history_coverage']['sampled'],
              'count_lower': count_history['history_dropped_count'], 'byte_retained': len(byte_history['events']),
              'byte_sampled': byte_history['history_coverage']['sampled'], 'byte_complete': byte_history['history_coverage']['complete']})


def test_ambiguous_mapping_rejected_until_trusted_reason_supplied(evidence_case):
    sink = hooks()
    generic = event('ACTION_FAILED', 'TARGET_MISSING')
    inferred = sink.try_emit(generic)
    explicit = sink.record(generic, reason='TARGET_IDENTITY_MISMATCH', dispatch_state='CONFIRMED')
    result = sink.history()
    evidence(evidence_case,
             {'inferred': False, 'explicit': True, 'exact': False, 'complete': False, 'reason': 'TARGET_IDENTITY_MISMATCH', 'dispatch': 'CONFIRMED'},
             {'inferred': inferred, 'explicit': explicit, 'exact': result['history_coverage']['loss_count_exact'],
              'complete': result['history_coverage']['complete'], 'reason': result['events'][0]['profile']['reason'],
              'dispatch': result['events'][0]['profile']['dispatch_state']})


@pytest.mark.parametrize('kind,failure,reason,dispatch,expected', [
    ('ACTION_FAILED', 'UNKNOWN', 'PROVIDER_FAILURE_UNCLASSIFIED', 'NOT_DISPATCHED', 'UNKNOWN'),
    ('ACTION_FAILED', 'UNKNOWN', 'PROVIDER_FAILURE_UNCLASSIFIED', 'CONFIRMED', 'CONFIRMED'),
    ('ACTION_FAILED', 'DISCONNECTED', 'PROVIDER_DISCONNECTED', 'POSSIBLE', 'POSSIBLE'),
    ('OBSERVATION_FAILED', 'UNKNOWN', 'OBSERVATION_UNDETERMINED', 'NOT_DISPATCHED', 'NOT_DISPATCHED'),
])
def test_dispatch_metadata_keeps_existing_uncertainty(kind, failure, reason, dispatch, expected, evidence_case):
    sink = hooks()
    assert sink.record(event(kind, failure), reason=reason, dispatch_state=dispatch)
    actual = sink.history()['events'][0]['profile']['dispatch_state']
    evidence(evidence_case, {'dispatch_state': expected}, {'dispatch_state': actual}, ac=('AC03',), evt=())


def test_lock_contention_returns_without_waiting_and_unknown_is_sticky(evidence_case):
    sink = hooks()
    sink._lock.acquire()
    completed, observed = Event(), []
    def emit():
        observed.append(sink.try_emit(event()))
        completed.set()
    thread = Thread(target=emit)
    thread.start()
    returned_while_locked = completed.wait(0.5)
    sink._lock.release()
    thread.join(1)
    assert sink.try_emit(event())
    first = sink.history()
    assert sink.try_emit(event())
    last = sink.history()
    evidence(evidence_case,
             {'returned_while_locked': True, 'accepted': [False], 'first_exact': False, 'later_exact': False, 'complete': False},
             {'returned_while_locked': returned_while_locked, 'accepted': observed,
              'first_exact': first['history_coverage']['loss_count_exact'],
              'later_exact': last['history_coverage']['loss_count_exact'], 'complete': last['history_coverage']['complete']},
             ac=('AC02', 'AC03'), evt=('EVT-03',))


def test_stalled_file_writer_never_blocks_original_provider_or_result(evidence_case):
    entered, release, publications = Event(), Event(), []
    def stalled(row):
        entered.set()
        release.wait(3)
        publications.append(row)
        return accepted(row)
    sink = hooks(stalled)
    sink.start()
    assert sink.heartbeat(operation_id=OPERATION) and entered.wait(1)
    provider = Provider()
    result = call(wrapped(provider, sink), operation_id=OPERATION)
    returned_before_release = not release.is_set()
    writer_still_waiting = not publications
    release.set()
    assert sink.close(timeout=2)
    evidence(evidence_case,
             {'returned_before_release': True, 'writer_still_waiting': True, 'original_result': True, 'provider_calls': 1, 'resends': 0, 'published': 3},
             {'returned_before_release': returned_before_release, 'writer_still_waiting': writer_still_waiting, 'original_result': result is provider.result,
              'provider_calls': len(provider.calls), 'resends': len(provider.calls)-1, 'published': len(publications)}, ac=('AC03',), evt=())


def test_inflight_diagnostic_counts_toward_queue_bound(evidence_case):
    entered, release = Event(), Event()
    def stalled(row):
        entered.set()
        release.wait(3)
        return accepted(row)
    sink = hooks(stalled, limits=HookLimits(queue_total=2, queue_critical_reserved=0))
    assert sink.try_emit(event())
    sink.start()
    assert entered.wait(1)
    second, third, stats = sink.try_emit(event()), sink.try_emit(event()), sink.stats
    release.set()
    assert sink.close(timeout=2)
    evidence(evidence_case, {'second': True, 'third': False, 'queued': 1, 'inflight': True},
             {'second': second, 'third': third, 'queued': stats['queue_depth'], 'inflight': stats['writer_inflight']})


def test_try_emit_never_calls_fsync_or_publisher(monkeypatch, evidence_case):
    calls = []
    def forbidden(*args):
        calls.append('UNEXPECTED_IO')
        raise AssertionError('UNEXPECTED_IO')
    sink = hooks(forbidden)
    monkeypatch.setattr(os, 'fsync', forbidden)
    result = sink.try_emit(event())
    evidence(evidence_case, {'accepted': True, 'io_calls': 0, 'stage': 'MEMORY_ACCEPTED'},
             {'accepted': result, 'io_calls': len(calls), 'stage': sink.history()['stage']}, ac=('AC03',), evt=())


@pytest.mark.parametrize('method', ['open_project_chat', 'execute_prompt_and_wait', 'download_response_zip'])
def test_full_queue_keeps_exact_result_and_original_exception(method, evidence_case):
    outcomes = []
    for raises in (False, True):
        sink = hooks(limits=HookLimits(queue_total=1, queue_critical_reserved=0))
        assert sink.try_emit(event('ACTION_STARTED'))
        provider = Provider()
        original = RuntimeError('SYNTHETIC_EXCEPTION_SENTINEL_DO_NOT_PERSIST')
        if raises:
            provider.error = original
        adapter = wrapped(provider, sink)
        if raises:
            with pytest.raises(RuntimeError) as caught:
                call(adapter, method, OPERATION)
            same = caught.value is original
        else:
            same = call(adapter, method, OPERATION) is provider.result
        with pytest.raises(GuardError):
            call(adapter, method, OPERATION)
        outcomes.append({'same': same, 'calls': len(provider.calls), 'resends': len(provider.calls)-1,
                         'operation_id': adapter.operation_records[0]['operation_id']})
    evidence(evidence_case, {'outcomes': [{'same': True, 'calls': 1, 'resends': 0, 'operation_id': OPERATION}] * 2},
             {'outcomes': outcomes}, ac=('AC03',), evt=())


def test_introspection_failure_preserves_result_and_records_observation(evidence_case):
    class Unobservable:
        @property
        def completed(self):
            raise RuntimeError('SYNTHETIC_OBSERVATION_TEXT_DO_NOT_PERSIST')
    provider, sink = Provider(Unobservable()), hooks()
    result = call(wrapped(provider, sink), operation_id=OPERATION)
    rows = sink.history()['events']
    evidence(evidence_case,
             {'same_result': True, 'calls': 1, 'kind': 'OBSERVATION_FAILED', 'reason': 'OBSERVATION_UNDETERMINED', 'reconciliation': True},
             {'same_result': result is provider.result, 'calls': len(provider.calls), 'kind': rows[-1]['event']['kind'],
              'reason': rows[-1]['profile']['reason'], 'reconciliation': provider.session.reconciliation}, ac=('AC03',), evt=())


def test_confirmed_prompt_facts_survive_final_revocation_with_new_sink(monkeypatch, tmp_path, evidence_case):
    page, session, request = operation_fixture('execute_prompt_and_wait', tmp_path)
    monkeypatch.setattr(generation.time, 'sleep', lambda delay: None)
    provider = api.create_facade(session=session)
    original_finish, originals = api._finish_operation, []
    def finish(owner, result):
        originals.append(result)
        return original_finish(owner, result)
    monkeypatch.setattr(api, '_finish_operation', finish)
    original_check = session.check_operation
    def check(*, mutation=True):
        if not mutation:
            session.fixture_transport.emit('Target.targetDestroyed', {'targetId': 'SYNTHETIC-TARGET-A'})
        return original_check(mutation=mutation)
    monkeypatch.setattr(session, 'check_operation', check)
    sink = hooks()
    adapter = wrapped(provider, sink)
    result = adapter.execute_prompt_and_wait(operation_id=OPERATION, **request)
    original, = originals
    with pytest.raises(GuardError):
        adapter.execute_prompt_and_wait(operation_id=OPERATION, **request)
    evidence(evidence_case,
             {'original_submission': True, 'original_response': True, 'confirmed': True, 'clicks': 1,
              'provider_calls': 1, 'failure': 'SESSION_BINDING_FAILURE', 'operation_id': OPERATION, 'ambiguous_event_rejected': 1},
             {'original_submission': result.submission is original.submission, 'original_response': result.response is original.response,
              'confirmed': result.submission.web_submit_established and result.response is not None,
              'clicks': page.clicks, 'provider_calls': adapter.operation_records[0]['provider_calls'],
              'failure': result.failure_kind.value, 'operation_id': adapter.operation_records[0]['operation_id'],
              'ambiguous_event_rejected': sink.stats['invalid_rejected']}, ac=('AC03',), evt=())


def test_publish_failure_is_unknown_not_false_known_loss_or_web_retry(evidence_case):
    published = []
    def uncertain(row):
        published.append(row)
        raise RuntimeError('SYNTHETIC_STORAGE_TEXT_DO_NOT_PERSIST')
    sink = hooks(uncertain)
    assert sink.try_emit(event()) and sink.drain_once() == 0
    assert sink.heartbeat(operation_id=OPERATION)
    result = sink.history()
    evidence(evidence_case,
             {'published_then_error': 1, 'loss_lower_bound': 0, 'exact': False, 'complete': False, 'publish_failed': 1},
             {'published_then_error': len(published), 'loss_lower_bound': result['history_dropped_count'],
              'exact': result['history_coverage']['loss_count_exact'], 'complete': result['history_coverage']['complete'],
              'publish_failed': sink.stats['publish_failed']})


def test_persisted_spool_contains_only_allowed_metadata_not_original_payloads(tmp_path, evidence_case):
    root = tmp_path / 'privacy_spool'
    mailbox = FileMailbox(root)
    sink = hooks(mailbox.publish, checkpoint_publisher=mailbox.publish_checkpoint)
    payloads = ['SYNTHETIC_PROMPT_SENTINEL_93bc', 'SYNTHETIC_RESPONSE_SENTINEL_68f1',
                'SYNTHETIC_COOKIE_SENTINEL_66bd', 'SYNTHETIC_EXCEPTION_SENTINEL_217a', '/synthetic-private-url-path-091d']
    original = SimpleNamespace(ok=True, completed=True, failure_kind=None,
                               response=SimpleNamespace(text=payloads[1]),
                               submission=SimpleNamespace(ok=True, activation_attempted=True, web_submit_established=True),
                               debug={'cookie': payloads[2], 'url': payloads[4]})
    provider = Provider(original)
    result = wrapped(provider, sink).execute_prompt_and_wait(operation_id=OPERATION, chat=object(), prompt=payloads[0], attachments=())
    failed_provider = Provider()
    original_error = RuntimeError(payloads[3])
    failed_provider.error = original_error
    with pytest.raises(RuntimeError) as caught:
        call(wrapped(failed_provider, sink), operation_id=' original-l05-exception ')
    assert caught.value is original_error
    assert sink.observation_failed(operation_id=OPERATION, dispatch_state='CONFIRMED')
    assert sink.heartbeat(operation_id=OPERATION, correlation={'trace_id': 'opaque-preserved'})
    assert sink.close(timeout=3)
    with JournalSpool(root) as journal:
        acks = journal.process_pending()
    combined = b'\n'.join(p.read_bytes() for p in root.rglob('*') if p.is_file())
    data = read_journal(root)
    history = read_history(root, stream=stream_key(data['rows'][0]['envelope']))
    actual = {'original_result': result is original, 'provider_calls': len(provider.calls),
              'sentinel_leaks': [text for text in payloads if text.encode() in combined], 'rows': len(data['rows']),
              'ack_count': sum(a['stage'] == 'ACK_DURABLE' for a in acks),
              'ids_preserved': all(r['envelope']['event']['operation_id'] in {OPERATION, ' original-l05-exception '} for r in data['rows']),
              'original_exception': caught.value is original_error, 'exception_provider_calls': len(failed_provider.calls),
              'event_keys_only': all(set(r['envelope']['event']) == {'schema', 'producer_id', 'producer_instance_id',
                    'operation_id', 'kind', 'failure_code', 'correlation'} for r in data['rows']),
              'clean_history': history['history_coverage']['complete']}
    preserve(root, 'hooks_privacy_spool')
    evidence(evidence_case,
             {'original_result': True, 'provider_calls': 1, 'sentinel_leaks': [], 'rows': 6, 'ack_count': 6,
              'ids_preserved': True, 'event_keys_only': True, 'clean_history': True,
              'original_exception': True, 'exception_provider_calls': 1}, actual,
             ac=('AC01', 'AC03', 'AC04'), evt=('EVT-01', 'EVT-04'))


@pytest.mark.parametrize('bad_kind', ['control_in_id', 'newline_correlation', 'overlong_payload', 'unknown_reason'])
def test_forbidden_or_invalid_metadata_never_reaches_publisher(bad_kind, evidence_case):
    published = []
    sink = hooks(lambda row: (published.append(row), accepted(row))[1])
    if bad_kind == 'control_in_id':
        ok = sink.try_emit(event(operation_id='opaque\nCOOKIE_SENTINEL'))
    elif bad_kind == 'newline_correlation':
        ok = sink.try_emit(event(correlation={'opaque': 'value\nSYNTHETIC_SECRET'}))
    elif bad_kind == 'overlong_payload':
        ok = sink.try_emit(event(correlation={f'k{i}': '\u65e5' * 1024 for i in range(16)}))
    else:
        ok = sink.record(event(), reason='SYNTHETIC_EXCEPTION_STRING')
    sink.drain_once()
    evidence(evidence_case, {'accepted': False, 'published': 0, 'exact': False},
             {'accepted': ok, 'published': len(published), 'exact': sink.history()['history_coverage']['loss_count_exact']},
             ac=('AC04',), evt=('EVT-04',))
