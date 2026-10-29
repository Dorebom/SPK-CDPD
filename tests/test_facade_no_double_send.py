"""BIND-03/TGT-27: synthetic provider and command-audit proof of single dispatch.

All objects in this module are synthetic. Evidence exposes fixed metadata only.
No live browser, endpoint, service, or diagnostic worker is used.
"""
from __future__ import annotations

from dataclasses import asdict
import inspect
from types import SimpleNamespace

import pytest

from cdpd_chatgpt_adapter import BoundedEventQueue, InstrumentedFacade
from chatgpt_web_api import api
from chatgpt_web_api.facade_contract import (
    CONTRACT_MAJOR_VERSION, CONTRACT_NAME, REQUIRED_CAPABILITIES,
    FacadeContractViolation, validate_facade_contract,
)
from chatgpt_web_api.models import (
    FailureKind, GenerationState, PromptExecutionResult, PromptExecutionSubmission,
)
from chatgpt_web_api.target_guard import GuardError


class OperationSession:
    """Guard-interface fixture; not a production bypass or alternate origin."""
    def __init__(self):
        self.denied: str | None = None
        self.post_denied: str | None = None
        self.reconciliation = False
        self.checks: list[bool] = []
        self.operation_active = False
        self.instrumented_active = None
        self.instrumented_provider_phase = False
        self.instrumented_provider_started = False
        self.instrumented_attempts = set()
        self.reservations = []

    def check_operation(self, *, mutation=True):
        self.checks.append(mutation)
        if self.denied:
            raise GuardError(self.denied)
        if mutation and self.reconciliation:
            raise GuardError('RECONCILIATION_REQUIRED')
        if not mutation and self.post_denied:
            raise GuardError(self.post_denied)

    def require_reconciliation(self):
        self.reconciliation = True

    def begin_operation(self):
        if self.operation_active or (self.instrumented_active is not None and not self.instrumented_provider_phase):
            raise GuardError('OWNER_BUSY')
        self.operation_active = True
        self.reservations.append('begin')

    def end_operation(self):
        assert self.operation_active
        self.operation_active = False
        self.reservations.append('end')

    def begin_instrumented_operation(self, method, operation_id):
        key = (method, operation_id)
        if self.instrumented_active is not None or key in self.instrumented_attempts:
            raise GuardError('RECONCILIATION_REQUIRED')
        self.instrumented_attempts.add(key)
        self.instrumented_active = object()
        self.instrumented_provider_started = False
        return self.instrumented_active

    def end_instrumented_operation(self, token):
        assert token is self.instrumented_active
        self.instrumented_active = None

    def begin_instrumented_provider(self, token):
        if token is not self.instrumented_active or self.instrumented_provider_started:
            raise GuardError('OWNER_BUSY')
        self.instrumented_provider_started = True
        self.instrumented_provider_phase = True

    def end_instrumented_provider(self, token):
        assert token is self.instrumented_active
        self.instrumented_provider_phase = False


class Provider:
    contract_name = CONTRACT_NAME
    contract_version = CONTRACT_MAJOR_VERSION
    capabilities = REQUIRED_CAPABILITIES

    def __init__(self, result=None):
        self.session = OperationSession()
        self.result = result if result is not None else SimpleNamespace(
            ok=True, completed=True, failure_kind=None,
            submission=SimpleNamespace(ok=True, activation_attempted=True, web_submit_established=True),
        )
        self.error: BaseException | None = None
        self.calls: list[str] = []
        self.commands: list[dict] = []
        self.after_dispatch = None

    def _invoke(self, method):
        self.calls.append(method)
        self.commands.append({'method': method, 'target_id': 'SYNTHETIC_PINNED_A',
                              'binding_id': 'synthetic-binding-a', 'epoch': 'a' * 32,
                              'dispatch_count': 1})
        if self.after_dispatch:
            self.after_dispatch()
        if self.error is not None:
            raise self.error
        return self.result

    def open_project_chat(self, *, project_name, project_key, client_ref, reuse):
        return self._invoke('open_project_chat')

    def execute_prompt_and_wait(self, *, chat, prompt, attachments):
        return self._invoke('execute_prompt_and_wait')

    def download_response_zip(self, *, response, expected_filename, destination_dir):
        return self._invoke('download_response_zip')


def call(wrapper, method='execute_prompt_and_wait', operation_id=' original-op-01 '):
    if method == 'execute_prompt_and_wait':
        return wrapper.execute_prompt_and_wait(operation_id=operation_id, chat=object(),
                                               prompt='synthetic payload', attachments=())
    if method == 'open_project_chat':
        return wrapper.open_project_chat(operation_id=operation_id, project_name='synthetic',
                                         project_key='synthetic-key', client_ref='caller-ref', reuse=False)
    return wrapper.download_response_zip(operation_id=operation_id, response=object(),
                                         expected_filename='synthetic.zip', destination_dir='unused')


def report(evidence_case, provider, *, expected, actual, ac=('AC01', 'AC02', 'AC04'),
           tgt=('TGT-27',), bind=('BIND-03',)):
    evidence_case(expected=expected, actual=actual, commands=provider.commands,
                  ac=list(ac), tgt=list(tgt), bind=list(bind))


@pytest.mark.parametrize('method', ['open_project_chat', 'execute_prompt_and_wait', 'download_response_zip'])
def test_each_facade_operation_calls_original_provider_once_and_blocks_replay(method, evidence_case):
    """Normal dispatch <=1; exact original ID and result preserved; replay =0."""
    provider = Provider()
    sink = BoundedEventQueue()
    wrapper = InstrumentedFacade(provider, sink)
    returned = call(wrapper, method)
    assert returned is provider.result
    with pytest.raises(GuardError):
        call(wrapper, method)
    events = sink.drain()
    assert len(provider.calls) == 1
    assert [event.operation_id for event in events] == [' original-op-01 ', ' original-op-01 ']
    assert [event.kind for event in events] == ['ACTION_STARTED', 'ACTION_FINISHED']
    record, = wrapper.operation_records
    assert record['provider_calls'] == 1
    assert record['provider_dispatched'] is True
    assert record['operation_id'] == ' original-op-01 '
    report(evidence_case, provider, expected={'provider_calls': 1, 'retry_calls': 0, 'original_result': True, 'original_operation_id': True},
           actual={'provider_calls': len(provider.calls), 'retry_calls': len(provider.calls)-1,
                   'original_result': returned is provider.result, 'original_operation_id': all(e.operation_id == ' original-op-01 ' for e in events)})


@pytest.mark.parametrize('reason', ['BINDING_REQUIRED', 'BINDING_RECORD_INVALID', 'BINDING_REVOKED',
                                  'BINDING_EXPIRED', 'RECONCILIATION_REQUIRED'])
def test_predispatch_guard_rejection_never_invokes_provider(reason, evidence_case):
    """Any registration/lease/overlay rejection happens before provider dispatch."""
    provider = Provider()
    provider.session.denied = reason
    sink = BoundedEventQueue()
    wrapper = InstrumentedFacade(provider, sink)
    with pytest.raises(GuardError) as caught:
        call(wrapper)
    assert caught.value.code == reason
    assert not provider.calls and not sink.drain()
    record, = wrapper.operation_records
    assert record['provider_calls'] == 0 and not record['provider_dispatched']
    report(evidence_case, provider, expected={'provider_calls': 0, 'send_state': 'NOT_DISPATCHED', 'reason': reason},
           actual={'provider_calls': len(provider.calls), 'send_state': record['send_state'], 'reason': caught.value.code})


def test_original_provider_without_session_guard_is_rejected_before_call(evidence_case):
    provider = Provider()
    provider.session = object()
    wrapper = InstrumentedFacade(provider, BoundedEventQueue())
    with pytest.raises(GuardError) as caught:
        call(wrapper)
    assert caught.value.code == 'BINDING_REQUIRED'
    assert not provider.calls
    report(evidence_case, provider, expected={'provider_calls': 0, 'reason': 'BINDING_REQUIRED'},
           actual={'provider_calls': len(provider.calls), 'reason': caught.value.code})


def test_guard_is_rechecked_after_action_started_hook_before_dispatch(evidence_case):
    provider = Provider()
    class RevokingSink:
        def try_emit(self, event):
            provider.session.denied = 'BINDING_REVOKED'
            return True
    wrapper = InstrumentedFacade(provider, RevokingSink())
    with pytest.raises(GuardError) as caught:
        call(wrapper)
    assert caught.value.code == 'BINDING_REVOKED' and not provider.calls
    report(evidence_case, provider, expected={'provider_calls': 0, 'guard_checks': 2},
           actual={'provider_calls': len(provider.calls), 'guard_checks': len(provider.session.checks)})


def test_provider_exception_is_preserved_and_different_id_does_not_resend(evidence_case):
    provider = Provider()
    original = RuntimeError('synthetic provider failure')
    provider.error = original
    sink = BoundedEventQueue()
    wrapper = InstrumentedFacade(provider, sink)
    with pytest.raises(RuntimeError) as caught:
        call(wrapper)
    assert caught.value is original
    with pytest.raises(GuardError):
        call(wrapper, operation_id='replacement-op-forbidden')
    assert len(provider.calls) == 1 and provider.session.reconciliation
    record, = wrapper.operation_records
    assert record['send_state'] == 'UNKNOWN'
    assert all(event.operation_id == ' original-op-01 ' for event in sink.drain())
    report(evidence_case, provider, expected={'provider_calls': 1, 'retry_calls': 0, 'original_exception': True, 'send_state': 'UNKNOWN'},
           actual={'provider_calls': len(provider.calls), 'retry_calls': len(provider.calls)-1,
                   'original_exception': caught.value is original, 'send_state': record['send_state']})


def test_postdispatch_guard_failure_preserves_result_and_forbids_new_mutation(evidence_case):
    provider = Provider()
    provider.session.post_denied = 'TARGET_BINDING_INVALID'
    wrapper = InstrumentedFacade(provider, BoundedEventQueue())
    result = call(wrapper)
    assert result is provider.result
    with pytest.raises(GuardError):
        call(wrapper, operation_id='new-id')
    record, = wrapper.operation_records
    assert record['postcheck_failed'] and provider.session.reconciliation
    report(evidence_case, provider, expected={'provider_calls': 1, 'original_result': True, 'postcheck_failed': True, 'new_mutation': 0},
           actual={'provider_calls': len(provider.calls), 'original_result': result is provider.result,
                   'postcheck_failed': record['postcheck_failed'], 'new_mutation': len(provider.calls)-1})


def test_result_observation_failure_keeps_exact_returned_object(evidence_case):
    class Unobservable:
        @property
        def completed(self):
            raise RuntimeError('synthetic observation failure')
    provider = Provider(Unobservable())
    sink = BoundedEventQueue()
    wrapper = InstrumentedFacade(provider, sink)
    result = call(wrapper)
    assert result is provider.result
    events = sink.drain()
    assert events[-1].kind == 'OBSERVATION_FAILED'
    assert provider.session.reconciliation and len(provider.calls) == 1
    report(evidence_case, provider, expected={'provider_calls': 1, 'original_result': True, 'observation_event': 'OBSERVATION_FAILED'},
           actual={'provider_calls': len(provider.calls), 'original_result': result is provider.result,
                   'observation_event': events[-1].kind})


@pytest.mark.parametrize('fault', ['raise', 'drop'])
def test_diagnostic_hook_failure_cannot_replace_result_or_retry(fault, evidence_case):
    provider = Provider()
    class FailedSink:
        def try_emit(self, event):
            if event.kind == 'ACTION_STARTED':
                return True
            if fault == 'raise':
                raise RuntimeError('synthetic sink failure')
            return False
    wrapper = InstrumentedFacade(provider, FailedSink())
    result = call(wrapper)
    assert result is provider.result and len(provider.calls) == 1
    assert wrapper.operation_records[0]['hook_failed']
    assert provider.session.reconciliation
    with pytest.raises(GuardError):
        call(wrapper, operation_id='new-id-after-hook-failure')
    report(evidence_case, provider, expected={'provider_calls': 1, 'original_result': True, 'hook_failed': True},
           actual={'provider_calls': len(provider.calls), 'original_result': result is provider.result,
                   'hook_failed': wrapper.operation_records[0]['hook_failed']})


def test_hook_cannot_recursively_reenter_original_mutation_under_any_id(evidence_case):
    provider = Provider()
    blocked = []
    class RecursiveSink:
        def try_emit(self, event):
            for proposed_id in [event.operation_id, 'diagnostic-replacement-id']:
                try:
                    call(wrapper, operation_id=proposed_id)
                except GuardError:
                    blocked.append(proposed_id)
            return True
    wrapper = InstrumentedFacade(provider, RecursiveSink())
    result = call(wrapper)
    assert result is provider.result and len(provider.calls) == 1 and len(blocked) == 4
    report(evidence_case, provider, expected={'provider_calls': 1, 'recursive_calls': 0, 'recursive_rejections': 4},
           actual={'provider_calls': len(provider.calls), 'recursive_calls': len(provider.calls)-1, 'recursive_rejections': len(blocked)})


def test_diagnostic_complete_does_not_turn_uncertain_submission_into_work_success(evidence_case):
    failed = PromptExecutionResult(
        submission=PromptExecutionSubmission(ok=False, activation_attempted=True,
                                             trusted_click=True, web_submit_established=False),
        completed=False, response=None, generation_state=GenerationState.UNKNOWN,
        failure_kind=FailureKind.RECONCILE_REQUIRED,
    )
    provider = Provider(failed)
    captures = []
    class CompleteDiagnosticSink:
        def try_emit(self, event):
            if event.kind == 'ACTION_FAILED':
                captures.append({'capture_status': 'COMPLETE', 'assessment_status': 'NOT_PERFORMED'})
            return True
    wrapper = InstrumentedFacade(provider, CompleteDiagnosticSink())
    result = call(wrapper)
    assert result is failed and result.completed is False
    assert result.submission.activation_attempted and not result.submission.web_submit_established
    assert captures == [{'capture_status': 'COMPLETE', 'assessment_status': 'NOT_PERFORMED'}]
    record, = wrapper.operation_records
    assert record['send_state'] == 'UNKNOWN' and provider.session.reconciliation
    report(evidence_case, provider, expected={'provider_calls': 1, 'work_completed': False, 'send_state': 'UNKNOWN', 'original_result': True},
           actual={'provider_calls': len(provider.calls), 'work_completed': result.completed,
                   'send_state': record['send_state'], 'original_result': result is failed})


def test_original_exception_survives_both_failed_diagnostic_and_state_hooks(evidence_case):
    provider = Provider()
    original = RuntimeError('synthetic provider error')
    provider.error = original
    def broken_reconciliation():
        raise RuntimeError('synthetic state hook failure')
    provider.session.require_reconciliation = broken_reconciliation
    class BrokenSink:
        def try_emit(self, event):
            raise RuntimeError('synthetic sink failure')
    wrapper = InstrumentedFacade(provider, BrokenSink())
    with pytest.raises(RuntimeError) as caught:
        call(wrapper)
    assert caught.value is original and len(provider.calls) == 1
    with pytest.raises(GuardError):
        call(wrapper, operation_id='new-id')
    report(evidence_case, provider, expected={'provider_calls': 1, 'original_exception': True, 'new_mutation': 0},
           actual={'provider_calls': len(provider.calls), 'original_exception': caught.value is original,
                   'new_mutation': len(provider.calls)-1})


def test_provider_validator_and_wrapper_remain_different_contracts(evidence_case):
    provider = Provider()
    wrapper = InstrumentedFacade(provider, BoundedEventQueue())
    assert validate_facade_contract(provider).major_version == 2
    with pytest.raises(FacadeContractViolation):
        validate_facade_contract(wrapper)
    assert 'operation_id' not in inspect.signature(provider.execute_prompt_and_wait).parameters
    assert 'operation_id' in inspect.signature(wrapper.execute_prompt_and_wait).parameters
    assert not provider.calls
    report(evidence_case, provider, expected={'provider_contract': 2, 'wrapper_is_distinct': True, 'provider_calls': 0},
           actual={'provider_contract': 2, 'wrapper_is_distinct': True, 'provider_calls': len(provider.calls)},
           ac=('AC04',), tgt=('TGT-33',), bind=())


@pytest.mark.parametrize('method', ['open_project_chat', 'execute_prompt_and_wait', 'download_response_zip'])
def test_offline_facade_rejects_unregistered_operation_with_existing_result_model(method, monkeypatch, evidence_case):
    """Offline construction allowed; none of the domain operations may execute."""
    provider = Provider()
    def forbidden(*args, **kwargs):
        provider.calls.append('forbidden-domain-dispatch')
        raise AssertionError('unregistered operation reached domain')
    monkeypatch.setattr(api, 'open_project', forbidden)
    monkeypatch.setattr(api, 'start_new_chat', forbidden)
    monkeypatch.setattr(api, 'submit_prompt_and_wait_for_response', forbidden)
    monkeypatch.setattr(api, 'download_zip_artifact', forbidden)
    facade = api.create_facade(session=object())
    if method == 'open_project_chat':
        result = facade.open_project_chat(project_name='synthetic', project_key='synthetic-key', client_ref='ref', reuse=False)
    elif method == 'execute_prompt_and_wait':
        result = facade.execute_prompt_and_wait(chat=object(), prompt='synthetic payload', attachments=())
    else:
        result = facade.download_response_zip(response=object(), expected_filename='synthetic.zip', destination_dir='unused')
    assert result.failure_kind is FailureKind.SESSION_BINDING_FAILURE
    assert result.reason == 'BINDING_REQUIRED'
    assert not provider.calls
    report(evidence_case, provider, expected={'domain_dispatch': 0, 'failure_kind': 'SESSION_BINDING_FAILURE', 'reason': 'BINDING_REQUIRED'},
           actual={'domain_dispatch': len(provider.calls), 'failure_kind': result.failure_kind.value, 'reason': result.reason},
           ac=('AC02', 'AC04'), tgt=('TGT-08', 'TGT-24'), bind=('BIND-02',))


@pytest.mark.parametrize('reason,expected_failure', [
    ('BINDING_RECORD_INVALID', FailureKind.SESSION_BINDING_FAILURE),
    ('BROWSER_IDENTITY_MISMATCH', FailureKind.TARGET_IDENTITY_MISMATCH),
    ('TARGET_AMBIGUOUS', FailureKind.AMBIGUOUS_OBSERVATION),
    ('RECONCILIATION_REQUIRED', FailureKind.RECONCILE_REQUIRED),
])
def test_public_facade_guard_uses_approved_failure_mapping(reason, expected_failure, evidence_case):
    provider = Provider()
    provider.session.denied = reason
    facade = api.create_facade(session=provider.session)
    result = facade.execute_prompt_and_wait(chat=object(), prompt='synthetic payload', attachments=())
    assert result.failure_kind is expected_failure and result.reason == reason
    assert not result.completed and not result.submission.activation_attempted
    assert not provider.calls
    report(evidence_case, provider, expected={'failure_kind': expected_failure.value, 'domain_dispatch': 0, 'activation_attempted': False},
           actual={'failure_kind': result.failure_kind.value, 'domain_dispatch': len(provider.calls),
                   'activation_attempted': result.submission.activation_attempted},
           ac=('AC02', 'AC04'), tgt=('TGT-08', 'TGT-24'), bind=('BIND-02',))


@pytest.mark.parametrize('method', ['open_project_chat', 'execute_prompt_and_wait', 'download_response_zip'])
def test_new_wrapper_on_same_session_cannot_replay_operation_id(method, evidence_case):
    provider = Provider()
    first = InstrumentedFacade(provider, BoundedEventQueue())
    second = InstrumentedFacade(provider, BoundedEventQueue())
    result = call(first, method)
    with pytest.raises(GuardError):
        call(second, method)
    assert result is provider.result and len(provider.calls) == 1
    assert second.operation_records[0]['provider_calls'] == 0
    report(evidence_case, provider, expected={'provider_calls': 1, 'second_wrapper_calls': 0, 'original_result': True},
           actual={'provider_calls': len(provider.calls), 'second_wrapper_calls': second.operation_records[0]['provider_calls'],
                   'original_result': result is provider.result})


def test_diagnostic_hook_cannot_enter_second_wrapper_with_a_different_id(evidence_case):
    provider = Provider()
    second_sink = BoundedEventQueue()
    second = InstrumentedFacade(provider, second_sink)
    blocked = []
    class RecursiveSink:
        def try_emit(self, event):
            try:
                call(second, operation_id='nested-' + event.kind)
            except GuardError:
                blocked.append(event.kind)
            return True
    first = InstrumentedFacade(provider, RecursiveSink())
    result = call(first)
    assert len(provider.calls) == 1 and result is provider.result
    assert blocked == ['ACTION_STARTED', 'ACTION_FINISHED']
    assert not second_sink.drain()
    report(evidence_case, provider, expected={'provider_calls': 1, 'recursive_calls': 0, 'recursive_rejections': 2},
           actual={'provider_calls': len(provider.calls), 'recursive_calls': 0, 'recursive_rejections': len(blocked)})


@pytest.mark.parametrize('fault', ['conversion', 'result_validation'])
def test_facade_postdispatch_conversion_fault_preserves_exception_and_releases_reservation(fault, monkeypatch, evidence_case):
    provider = Provider()
    original = RuntimeError('synthetic conversion fault')
    class BrokenRaw:
        @property
        def submission(self):
            raise original
    raw = BrokenRaw() if fault == 'conversion' else SimpleNamespace(
        submission=None, completed=False, response=None,
        generation_state=GenerationState.NOT_STARTED, failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
        reason='SYNTHETIC_FAILURE', evidence=None,
    )
    def invoke(*args, **kwargs):
        provider._invoke('execute_prompt_and_wait')
        assert provider.session.operation_active
        return raw
    monkeypatch.setattr(api, 'submit_prompt_and_wait_for_response', invoke)
    if fault == 'result_validation':
        def invalid(*args, **kwargs):
            raise original
        monkeypatch.setattr(api, 'validate_operation_result', invalid)
    facade = api.create_facade(session=provider.session)
    with pytest.raises(RuntimeError) as caught:
        facade.execute_prompt_and_wait(chat=object(), prompt='synthetic payload', attachments=())
    assert caught.value is original and len(provider.calls) == 1
    assert provider.session.reconciliation and not provider.session.operation_active
    assert provider.session.reservations == ['begin', 'end']
    with pytest.raises(GuardError):
        provider.session.check_operation(mutation=True)
    report(evidence_case, provider, expected={'provider_calls': 1, 'original_exception': True, 'reservation_released': True, 'new_mutation': 0},
           actual={'provider_calls': len(provider.calls), 'original_exception': caught.value is original,
                   'reservation_released': not provider.session.operation_active, 'new_mutation': 0})


def test_direct_facade_reservation_blocks_reentrant_mutation(monkeypatch, evidence_case):
    provider = Provider()
    nested = []
    raw = SimpleNamespace(submission=None, completed=False, response=None,
                          generation_state=GenerationState.NOT_STARTED,
                          failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                          reason='SYNTHETIC_NOT_SENT', evidence=None)
    facade = api.create_facade(session=provider.session)
    def invoke(*args, **kwargs):
        provider._invoke('execute_prompt_and_wait')
        nested.append(facade.execute_prompt_and_wait(chat=object(), prompt='synthetic payload', attachments=()))
        return raw
    monkeypatch.setattr(api, 'submit_prompt_and_wait_for_response', invoke)
    facade.execute_prompt_and_wait(chat=object(), prompt='synthetic payload', attachments=())
    assert len(provider.calls) == 1 and nested[0].reason == 'OWNER_BUSY'
    assert provider.session.reservations == ['begin', 'end']
    report(evidence_case, provider, expected={'provider_calls': 1, 'nested_domain_calls': 0, 'nested_reason': 'OWNER_BUSY'},
           actual={'provider_calls': len(provider.calls), 'nested_domain_calls': 0, 'nested_reason': nested[0].reason})


def test_hook_cannot_reenter_original_facade_directly_outside_provider_phase(monkeypatch, evidence_case):
    from strict_fakes import make_bound_session
    session = make_bound_session()
    provider = api.create_facade(session=session)
    calls = []
    nested = []
    raw = SimpleNamespace(submission=None, completed=False, response=None,
                          generation_state=GenerationState.NOT_STARTED,
                          failure=FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                          reason='SYNTHETIC_NOT_SENT', evidence=None)
    def invoke(*args, **kwargs):
        calls.append('execute_prompt_and_wait')
        return raw
    monkeypatch.setattr(api, 'submit_prompt_and_wait_for_response', invoke)
    class DirectRecursiveSink:
        def try_emit(self, event):
            result = provider.execute_prompt_and_wait(chat=object(), prompt='synthetic', attachments=())
            nested.append((event.kind, result.reason))
            return True
    wrapper = InstrumentedFacade(provider, DirectRecursiveSink())
    result = call(wrapper)
    assert len(calls) == 1 and not result.completed
    assert nested == [('ACTION_STARTED', 'OWNER_BUSY'), ('ACTION_FAILED', 'OWNER_BUSY')]
    with pytest.raises(GuardError):
        call(InstrumentedFacade(provider, BoundedEventQueue()))
    assert len(calls) == 1
    evidence_case(expected={'domain_calls': 1, 'direct_hook_mutations': 0, 'replay_calls': 0},
                  actual={'domain_calls': len(calls), 'direct_hook_mutations': 0, 'replay_calls': 0},
                  commands=session.fixture_transport.audit,
                  ac=['AC01', 'AC02', 'AC04'], tgt=['TGT-27'], bind=['BIND-03'])


@pytest.mark.parametrize('provider_raises', [False, True])
def test_provider_phase_close_fault_never_replaces_original_outcome(provider_raises, evidence_case):
    provider = Provider()
    original = RuntimeError('synthetic provider failure')
    if provider_raises:
        provider.error = original
    class PhaseHookFault(BaseException):
        pass
    close = provider.session.end_instrumented_provider
    def broken_close(token):
        close(token)
        raise PhaseHookFault()
    provider.session.end_instrumented_provider = broken_close
    wrapper = InstrumentedFacade(provider, BoundedEventQueue())
    if provider_raises:
        with pytest.raises(RuntimeError) as caught:
            call(wrapper)
        retained = caught.value is original
    else:
        retained = call(wrapper) is provider.result
    assert retained and len(provider.calls) == 1 and provider.session.reconciliation
    assert wrapper.operation_records[0]['provider_phase_end_failed']
    with pytest.raises(GuardError):
        call(wrapper, operation_id='new-id')
    report(evidence_case, provider,
           expected={'provider_calls': 1, 'original_outcome': True, 'new_mutation': 0},
           actual={'provider_calls': len(provider.calls), 'original_outcome': retained, 'new_mutation': 0})
