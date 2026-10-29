"""FIX001 F-001: final Binding checks through all three real v2 Facade paths.

Only clock/CDP/Page boundaries are synthetic. The production project/chat,
composer/generation and artifact pipelines construct the precheck outcomes.
No browser, socket, service, native hook or original evidence is used or edited.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from functools import wraps
import hashlib
import io
from pathlib import Path
import zipfile

import pytest

from cdpd_chatgpt_adapter import BoundedEventQueue, InstrumentedFacade
from chatgpt_web_api import api, generation
from chatgpt_web_api.facade_contract import validate_operation_result
from chatgpt_web_api.models import (
    AssistantResponse, FailureKind, GenerationState, MessageIdentity,
    MessageRole, ProjectChatOpenResult, PromptExecutionResult,
    ResponseZipDownloadResult, WebDebugEvidence, WebOperationEvidenceRecord,
)
from chatgpt_web_api.target_guard import GuardError
from strict_fakes import make_bound_session
from test_facade_generation_guard import SubmissionPage


METHODS = ('open_project_chat', 'execute_prompt_and_wait', 'download_response_zip')
CAPABILITIES = dict(zip(METHODS, ('project_chat.open', 'prompt.execute_and_wait',
                               'artifact.download_response_zip')))
OPERATION_ID = ' original-fix001-operation-01 '


class ThreeOperationPage(SubmissionPage):
    """Exact synthetic observations for production domain functions."""
    def __init__(self, method):
        super().__init__()
        self.method = method
        self.domain_failure = False
        if method == 'open_project_chat':
            self.url = 'https://chatgpt.com/g/g-p-synthetic'
        self.fetches = 0
        self.navigations = 0
        buffer = io.BytesIO()
        # Fixed ZIP timestamp makes the fixture payload deterministic.
        with zipfile.ZipFile(buffer, 'w') as archive:
            info = zipfile.ZipInfo('synthetic.txt', (2026, 10, 24, 0, 0, 0))
            archive.writestr(info, b'synthetic fixture')
        self.archive = buffer.getvalue()

    def goto(self, url, *, timeout_ms):
        self.navigations += 1
        self.url = url

    def evaluate(self, expression, arg=None):
        if 'SHIRABE_PROJECT_OBSERVE_V1' in expression:
            if self.domain_failure:
                return {'url': self.url, 'title': 'Synthetic Page', 'currentProjectKey': None,
                        'matchingCandidateCount': 0, 'currentMatchingCandidateCount': 0, 'candidates': []}
            return {'url': self.url, 'title': 'Synthetic Page',
                    'currentProjectKey': 'g-p-synthetic',
                    'matchingCandidateCount': 1, 'currentMatchingCandidateCount': 1,
                    'candidates': [{'name': 'Synthetic Project', 'projectKey': 'g-p-synthetic',
                                    'rootUrl': 'https://chatgpt.com/g/g-p-synthetic',
                                    'href': 'https://chatgpt.com/g/g-p-synthetic',
                                    'tag': 'A', 'aria': 'Synthetic Project', 'text': 'Synthetic Project'}]}
        if 'SHIRABE_CHAT_OBSERVE_V1' in expression:
            return {'url': self.url, 'projectKey': 'g-p-synthetic',
                    'projectSegment': 'g-p-synthetic', 'conversationId': None,
                    'isProjectRoot': True, 'messageCount': 0}
        if 'SHIRABE_ARTIFACT_DISCOVERY_V1' in expression:
            if self.domain_failure:
                return {'url': self.url, 'assistantRows': [{
                    'assistantIndex': 0, 'text': 'synthetic-response',
                    'rawId': 'synthetic-assistant-1', 'controls': []}]}
            return {'url': self.url, 'assistantRows': [{
                'assistantIndex': 0, 'text': 'synthetic-response', 'rawId': 'synthetic-assistant-1',
                'controls': [{'controlIndex': 0, 'tag': 'a',
                              'href': 'https://chatgpt.com/backend-api/estuary/fix001',
                              'download': 'synthetic.zip', 'text': 'synthetic.zip',
                              'ariaLabel': 'Download synthetic.zip', 'dataTestId': 'artifact-download',
                              'disabled': False}]}]}
        if 'SHIRABE_ARTIFACT_FETCH_V1' in expression:
            self.fetches += 1
            return {'ok': True, 'status': 200, 'contentType': 'application/zip',
                    'contentDisposition': 'attachment; filename="synthetic.zip"',
                    'contentLength': str(len(self.archive)),
                    'finalUrl': 'https://chatgpt.com/backend-api/estuary/fix001',
                    'bodyBase64': base64.b64encode(self.archive).decode('ascii')}
        return super().evaluate(expression, arg)


def operation_fixture(method, destination):
    page = ThreeOperationPage(method)
    session = make_bound_session(page=page)
    page.session = session
    assert session.bind_chatgpt_page().ok
    if method == 'open_project_chat':
        request = dict(project_name='Synthetic Project', project_key='g-p-synthetic',
                       client_ref='synthetic-ref', reuse=False)
    elif method == 'execute_prompt_and_wait':
        from chatgpt_web_api.models import ChatIdentity
        chat = ChatIdentity(project_key='g-p-synthetic', url=page.url,
                            conversation_id='synthetic-conversation', client_ref='synthetic-ref')
        request = dict(chat=chat, prompt='synthetic', attachments=())
    else:
        identity = MessageIdentity(project_key='g-p-synthetic',
                                   conversation_id='synthetic-conversation', role=MessageRole.ASSISTANT,
                                   role_index=0, semantic_text_sha256=hashlib.sha256(b'synthetic-response').hexdigest(),
                                   message_id_sha256=hashlib.sha256(b'synthetic-assistant-1').hexdigest())
        request = dict(response=AssistantResponse(identity, 'synthetic-response'),
                       expected_filename='synthetic.zip', destination_dir=destination)
    return page, session, request


def success(result):
    return result.completed if isinstance(result, PromptExecutionResult) else result.ok


def evidence_reason(result):
    evidence = result.operation_evidence
    if isinstance(evidence, WebDebugEvidence):
        return evidence.details['binding_postcheck']['reason']
    return next(value.split('=', 1)[1] for value in evidence.observations
                if value.startswith('binding-postcheck-reason='))


def evidence_operation_id(result):
    evidence = result.operation_evidence
    if isinstance(evidence, WebDebugEvidence):
        return evidence.details['binding_postcheck']['operation_id']
    return next(value.split('=', 1)[1] for value in evidence.observations
                if value.startswith('instrumented-operation-id='))


def public_result_metadata(result):
    """Content-free result/evidence facts for source-linked case records."""
    evidence = result.operation_evidence
    value = {'type': type(result).__name__, 'aggregate_success': success(result),
             'failure_kind': result.failure_kind.value if result.failure_kind else None,
             'operation_evidence': {'type': type(evidence).__name__}}
    if isinstance(evidence, WebDebugEvidence):
        value['operation_evidence'].update(
            operation=evidence.operation, observations=list(evidence.observations),
            binding_postcheck=evidence.details.get('binding_postcheck'))
    elif isinstance(evidence, WebOperationEvidenceRecord):
        value['operation_evidence'].update(
            operation=evidence.operation, outcome=evidence.outcome, operation_id=evidence.operation_id,
            observations=list(evidence.observations),
            failure_kind=evidence.failure.value if evidence.failure else None)
    if isinstance(result, PromptExecutionResult):
        value.update(submission_ok=result.submission.ok, activation_attempted=result.submission.activation_attempted,
                     trusted_click=result.submission.trusted_click,
                     web_submit_established=result.submission.web_submit_established,
                     generation_state=result.generation_state.value, response_present=result.response is not None,
                     response_identity_sha256=result.response.identity.semantic_text_sha256 if result.response else None)
    elif isinstance(result, ResponseZipDownloadResult):
        value.update(response_binding_verified=result.response_binding_verified,
                     artifact_present=result.artifact is not None,
                     artifact_sha256=result.artifact.sha256 if result.artifact else None)
    else:
        value.update(project_identity_present=result.project_identity is not None,
                     chat_identity_present=result.chat_identity is not None,
                     postcondition=result.postcondition.value if result.postcondition else None)
    return value


def record_case(evidence_case, session, expected, actual):
    evidence_case(expected=expected, actual=actual, commands=session.fixture_transport.audit,
                  ac=['AC01', 'AC02', 'AC04'], tgt=['TGT-10', 'TGT-11', 'TGT-27'],
                  bind=['BIND-02', 'BIND-03'])


def run_case(method, wrapper, fault, phase, monkeypatch, tmp_path, evidence_case):
    page, session, request = operation_fixture(method, tmp_path)
    cached_ops = session._bound_page().ops
    monkeypatch.setattr(generation.time, 'sleep', lambda delay: None)
    provider = api.create_facade(session=session)
    original_method = getattr(provider, method)
    provider_calls = []
    @wraps(original_method)
    def invoke(**kwargs):
        provider_calls.append(method)
        return original_method(**kwargs)
    monkeypatch.setattr(provider, method, invoke)
    original_finish = api._finish_operation
    original_results = []
    def finish(current_session, result):
        original_results.append(result)
        return original_finish(current_session, result)
    monkeypatch.setattr(api, '_finish_operation', finish)
    original_check = session.check_operation
    read_checks = []
    injected = []
    def check(*, mutation=True):
        if not mutation:
            read_checks.append(False)
            if fault != 'normal' and len(read_checks) == (2 if phase == 'wrapper' else 1):
                injected.append(fault)
                if fault == 'target':
                    session.fixture_transport.emit('Target.targetDestroyed', {'targetId': 'SYNTHETIC-TARGET-A'})
                elif fault == 'lease':
                    session.fixture_clock.advance(14400)
                else:
                    raise RuntimeError('synthetic final validation unavailable')
        return original_check(mutation=mutation)
    monkeypatch.setattr(session, 'check_operation', check)
    sink = BoundedEventQueue()
    instrumented = InstrumentedFacade(provider, sink) if wrapper else None
    target = instrumented or provider
    call_request = dict(request, operation_id=OPERATION_ID) if wrapper else request
    result = getattr(target, method)(**call_request)
    assert len(provider_calls) == 1 and len(original_results) == 1
    original, = original_results
    assert success(original), 'fault must follow a successful production domain result'
    assert bool(injected) is (fault != 'normal')
    assert success(result) is (fault == 'normal')
    # The pre-existing contract validator accepts the corrected result.
    validate_operation_result(CAPABILITIES[method], result, request=request)
    events = sink.drain()
    records = instrumented.operation_records if wrapper else ()
    if fault == 'normal':
        assert result is original and result.failure_kind is None
        original_check(mutation=True)
        if wrapper:
            assert records[0]['postcheck_failed'] is False
            assert events[-1].kind == 'ACTION_FINISHED' and events[-1].failure_code == 'NONE'
    else:
        expected_reason = {'target': 'TARGET_MISSING', 'lease': 'BINDING_EXPIRED',
                           'unverifiable': 'TARGET_UNVERIFIED'}[fault]
        assert result.failure_kind is FailureKind.SESSION_BINDING_FAILURE
        assert result.reason == expected_reason == evidence_reason(result)
        with pytest.raises(GuardError):
            original_check(mutation=True)
        before = len(session.fixture_transport.primitive_commands)
        with pytest.raises(GuardError):
            cached_ops.evaluate('synthetic denied mutation')
        assert len(session.fixture_transport.primitive_commands) == before
        if wrapper:
            assert records[0]['postcheck_failed'] is True
            assert records[0]['postcheck_reason'] == expected_reason
            assert records[0]['provider_calls'] == 1
            assert events[-1].kind == 'ACTION_FAILED' and events[-1].failure_code != 'NONE'
            assert evidence_operation_id(result) == records[0]['operation_id'] == OPERATION_ID
            assert all(event.operation_id == OPERATION_ID for event in events)
            with pytest.raises(GuardError):
                getattr(instrumented, method)(**dict(request, operation_id='forbidden-replacement-id'))
            assert len(provider_calls) == 1
    if isinstance(result, PromptExecutionResult):
        assert result.submission is original.submission
        assert result.response is original.response and result.response is not None
        assert result.generation_state is GenerationState.COMPLETED
        assert result.submission.ok and result.submission.activation_attempted
        assert result.submission.trusted_click and result.submission.web_submit_established
        assert page.clicks == 1
        if fault != 'normal':
            assert result.operation_evidence.details['binding_postcheck']['original_completed'] is True
            assert result.operation_evidence.details['binding_postcheck']['original_generation_state'] == 'COMPLETED'
    elif isinstance(result, ProjectChatOpenResult):
        assert result.project_identity is original.project_identity
        assert result.chat_identity is original.chat_identity
        assert result.postcondition is original.postcondition
        assert page.clicks == 0
    else:
        assert result.artifact is original.artifact
        assert result.response_binding_verified is original.response_binding_verified is True
        assert page.fetches == 1 and page.clicks == 0
        assert Path(result.artifact.saved_path).read_bytes() == page.archive
        assert result.operation_evidence.operation_id == original.operation_evidence.operation_id
        assert result.operation_evidence.artifact is original.operation_evidence.artifact
        assert result.operation_evidence.response_identity is original.operation_evidence.response_identity
        if fault != 'normal':
            assert result.operation_evidence.outcome == 'BINDING_POSTCHECK_FAILED'
            assert 'provider-outcome=DOWNLOADED' in result.operation_evidence.observations
    retained = True
    record_case(evidence_case, session,
                {'provider_calls': 1, 'retry_calls': 0, 'aggregate_success': fault == 'normal',
                 'original_facts_retained': True, 'failed_postcheck_visible': fault != 'normal'},
                {'method': method, 'wrapper': wrapper, 'fault': fault, 'phase': phase,
                 'provider_calls': len(provider_calls), 'retry_calls': len(provider_calls)-1,
                 'aggregate_success': success(result), 'original_facts_retained': retained,
                 'failed_postcheck_visible': bool(fault != 'normal' and result.failure_kind),
                 'send_activations': page.clicks, 'artifact_fetches': page.fetches,
                 'failure_kind': result.failure_kind.value if result.failure_kind else None,
                 'postcheck_reason': evidence_reason(result) if fault != 'normal' else None,
                 'operation_id': evidence_operation_id(result) if wrapper and fault != 'normal' else None,
                 'events': [{'kind': event.kind, 'failure_code': event.failure_code,
                             'operation_id': event.operation_id} for event in events],
                 'operation_records': list(records),
                 'original_public_result': public_result_metadata(original),
                 'returned_public_result': public_result_metadata(result),
                 'confirmed_generation': result.generation_state.value if isinstance(result, PromptExecutionResult) else None})


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('wrapper', (False, True), ids=('direct', 'instrumented'))
@pytest.mark.parametrize('fault', ('normal', 'target', 'lease', 'unverifiable'))
def test_final_postcheck_reports_failure_and_preserves_confirmed_facts(method, wrapper, fault,
                                                                     monkeypatch, tmp_path, evidence_case):
    """Three real Facade operations: normal control or final Target/lease/unknown failure."""
    run_case(method, wrapper, fault, 'direct', monkeypatch, tmp_path, evidence_case)


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('fault', ('target', 'lease', 'unverifiable'))
def test_wrapper_own_final_postcheck_is_not_hidden_by_provider_success(method, fault,
                                                                     monkeypatch, tmp_path, evidence_case):
    """A fault first observed by the wrapper's own final check is caller-visible."""
    run_case(method, True, fault, 'wrapper', monkeypatch, tmp_path, evidence_case)


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('wrapper', (False, True), ids=('direct', 'instrumented'))
def test_original_exception_survives_postdispatch_failure_and_broken_hooks(method, wrapper,
                                                                          monkeypatch, tmp_path, evidence_case):
    """Original postdispatch exception is re-raised unchanged; state/sink failures never retry."""
    page, session, request = operation_fixture(method, tmp_path)
    monkeypatch.setattr(generation.time, 'sleep', lambda delay: None)
    original = RuntimeError('synthetic original domain exception')
    helper = {'open_project_chat': 'start_new_chat', 'execute_prompt_and_wait': 'submit_prompt_and_wait_for_response',
              'download_response_zip': 'download_zip_artifact'}[method]
    lower = getattr(api, helper)
    calls = []
    def domain(*args, **kwargs):
        calls.append(method)
        value = lower(*args, **kwargs)
        assert value.completed if method == 'execute_prompt_and_wait' else value.ok
        session.fixture_transport.emit('Target.targetDestroyed', {'targetId': 'SYNTHETIC-TARGET-A'})
        raise original
    monkeypatch.setattr(api, helper, domain)
    def broken_state():
        raise RuntimeError('synthetic state hook unavailable')
    monkeypatch.setattr(session, 'require_reconciliation', broken_state)
    class BrokenSink:
        def try_emit(self, event):
            raise RuntimeError('synthetic sink unavailable')
    provider = api.create_facade(session=session)
    target = InstrumentedFacade(provider, BrokenSink()) if wrapper else provider
    call_request = dict(request, operation_id=OPERATION_ID) if wrapper else request
    with pytest.raises(RuntimeError) as caught:
        getattr(target, method)(**call_request)
    assert caught.value is original and len(calls) == 1
    with pytest.raises(GuardError):
        session.check_operation(mutation=True)
    if wrapper:
        record, = target.operation_records
        assert record['provider_calls'] == 1 and record['outcome'] == 'PROVIDER_EXCEPTION'
        assert record['operation_id'] == OPERATION_ID and record['hook_failed'] is True
        with pytest.raises(GuardError):
            getattr(target, method)(**dict(request, operation_id='forbidden-replacement-id'))
    assert len(calls) == 1
    record_case(evidence_case, session,
                {'original_exception': True, 'domain_calls': 1, 'retry_calls': 0},
                {'method': method, 'wrapper': wrapper, 'original_exception': caught.value is original,
                 'domain_calls': len(calls), 'retry_calls': len(calls)-1,
                 'send_activations': page.clicks, 'artifact_fetches': page.fetches})


@pytest.mark.parametrize('method', METHODS)
def test_diagnostic_sink_failure_preserves_successful_provider_result(method, monkeypatch, tmp_path, evidence_case):
    """A failed diagnostic sink remains distinct from a detected Binding failure."""
    page, session, request = operation_fixture(method, tmp_path)
    monkeypatch.setattr(generation.time, 'sleep', lambda delay: None)
    captured = []
    original_finish = api._finish_operation
    def finish(current_session, result):
        captured.append(result)
        return original_finish(current_session, result)
    monkeypatch.setattr(api, '_finish_operation', finish)
    class BrokenSink:
        def try_emit(self, event):
            raise RuntimeError('synthetic diagnostic sink failure')
    wrapper = InstrumentedFacade(api.create_facade(session=session), BrokenSink())
    result = getattr(wrapper, method)(**dict(request, operation_id=OPERATION_ID))
    assert len(captured) == 1 and result is captured[0] and success(result)
    record, = wrapper.operation_records
    assert record['provider_calls'] == 1 and record['hook_failed'] and not record['postcheck_failed']
    with pytest.raises(GuardError):
        getattr(wrapper, method)(**dict(request, operation_id='forbidden-replacement-id'))
    record_case(evidence_case, session,
                {'provider_calls': 1, 'retry_calls': 0, 'original_result': True, 'postcheck_failed': False},
                {'method': method, 'provider_calls': record['provider_calls'], 'retry_calls': 0,
                 'original_result': result is captured[0], 'postcheck_failed': record['postcheck_failed'],
                 'original_public_result': public_result_metadata(captured[0]),
                 'returned_public_result': public_result_metadata(result),
                 'send_activations': page.clicks, 'artifact_fetches': page.fetches})


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('wrapper', (False, True), ids=('direct', 'instrumented'))
def test_original_failed_result_and_evidence_survive_an_additional_final_binding_failure(
        method, wrapper, monkeypatch, tmp_path, evidence_case):
    """An existing domain failure remains intact; final Binding failure is additional evidence."""
    page, session, request = operation_fixture(method, tmp_path)
    page.domain_failure = True
    if method == 'execute_prompt_and_wait':
        page.fault = 'generation'
    monkeypatch.setattr(generation.time, 'sleep', lambda delay: None)
    original_results = []
    original_finish = api._finish_operation
    def finish(current_session, result):
        original_results.append(result)
        return original_finish(current_session, result)
    monkeypatch.setattr(api, '_finish_operation', finish)
    original_check = session.check_operation
    injected = []
    def check(*, mutation=True):
        if not mutation and not injected:
            injected.append(True)
            session.fixture_transport.emit('Target.targetDestroyed', {'targetId': 'SYNTHETIC-TARGET-A'})
        return original_check(mutation=mutation)
    monkeypatch.setattr(session, 'check_operation', check)
    sink = BoundedEventQueue()
    provider = api.create_facade(session=session)
    target = InstrumentedFacade(provider, sink) if wrapper else provider
    result = getattr(target, method)(**(dict(request, operation_id=OPERATION_ID) if wrapper else request))
    original, = original_results
    assert not success(original) and not success(result)
    assert result.failure_kind is original.failure_kind and result.reason == original.reason
    assert result.failure_kind is not FailureKind.SESSION_BINDING_FAILURE
    assert evidence_reason(result) == 'TARGET_MISSING'
    validate_operation_result(CAPABILITIES[method], result, request=request)
    if isinstance(result.operation_evidence, WebDebugEvidence):
        for key, value in original.operation_evidence.details.items():
            assert result.operation_evidence.details[key] == value
        assert result.operation_evidence.details['binding_postcheck']['original_failure_kind'] == original.failure_kind.value
        assert result.operation_evidence.details['binding_postcheck']['original_reason'] == original.reason
    else:
        assert result.operation_evidence.operation_id == original.operation_evidence.operation_id
        assert result.operation_evidence.failure is original.operation_evidence.failure
        assert result.operation_evidence.reason == original.operation_evidence.reason
        assert 'provider-outcome=FAILED' in result.operation_evidence.observations
    for observation in original.operation_evidence.observations:
        assert observation in result.operation_evidence.observations
    if isinstance(result, PromptExecutionResult):
        assert result.submission is original.submission and result.submission.web_submit_established
        assert result.response is original.response and result.generation_state is original.generation_state
        assert page.clicks == 1
    if wrapper:
        record, = target.operation_records
        events = sink.drain()
        assert record['postcheck_reason'] == 'TARGET_MISSING' and record['provider_calls'] == 1
        assert evidence_operation_id(result) == record['operation_id'] == OPERATION_ID
        assert events[-1].kind == 'ACTION_FAILED' and events[-1].failure_code == 'TARGET_MISSING'
        assert all(event.operation_id == OPERATION_ID for event in events)
    record_case(evidence_case, session,
                {'original_failure_retained': True, 'original_evidence_retained': True,
                 'additional_binding_failure': 'TARGET_MISSING', 'retry_calls': 0},
                {'method': method, 'wrapper': wrapper, 'original_failure_retained': True,
                 'original_evidence_retained': True, 'additional_binding_failure': evidence_reason(result),
                 'failure_kind': result.failure_kind.value, 'retry_calls': 0,
                 'original_public_result': public_result_metadata(original),
                 'returned_public_result': public_result_metadata(result),
                 'operation_records': list(target.operation_records) if wrapper else [],
                 'events': [{'kind': event.kind, 'failure_code': event.failure_code,
                             'operation_id': event.operation_id} for event in events] if wrapper else [],
                 'send_activations': page.clicks, 'artifact_fetches': page.fetches})


def test_wrapper_adds_typed_postcheck_evidence_for_failed_custom_provider_without_evidence(evidence_case):
    """A custom failed v2 download may omit evidence; wrapper uses the existing caller ID."""
    from test_facade_no_double_send import Provider, call
    original = ResponseZipDownloadResult(ok=False, artifact=None, response_binding_verified=False,
                                         failure_kind=FailureKind.ARTIFACT_NOT_FOUND,
                                         reason='synthetic original domain failure', operation_evidence=None)
    provider = Provider(original)
    provider.session.post_denied = 'TARGET_UNVERIFIED'
    sink = BoundedEventQueue()
    wrapper = InstrumentedFacade(provider, sink)
    result = call(wrapper, 'download_response_zip', OPERATION_ID)
    assert result.failure_kind is original.failure_kind and result.reason == original.reason
    assert result.artifact is None and not result.response_binding_verified and not result.ok
    evidence = result.operation_evidence
    assert isinstance(evidence, WebOperationEvidenceRecord)
    assert evidence.operation_id == OPERATION_ID and evidence_operation_id(result) == OPERATION_ID
    assert evidence.operation == 'artifact.download_response_zip.binding_postcheck'
    assert evidence.outcome == 'BINDING_POSTCHECK_FAILED'
    assert evidence.started_at == evidence.completed_at
    assert 'original-evidence=absent' in evidence.observations
    assert 'timestamp-scope=postcheck-observation' in evidence.observations
    assert evidence_reason(result) == 'TARGET_UNVERIFIED'
    validate_operation_result('artifact.download_response_zip', result)
    events = sink.drain()
    record, = wrapper.operation_records
    assert events[-1].kind == 'ACTION_FAILED' and events[-1].failure_code != 'NONE'
    assert record['provider_calls'] == 1 and record['postcheck_failed'] is True
    assert all(event.operation_id == OPERATION_ID for event in events)
    with pytest.raises(GuardError):
        call(wrapper, 'download_response_zip', 'forbidden-replacement-id')
    assert len(provider.calls) == 1
    evidence_case(expected={'provider_calls': 1, 'retry_calls': 0, 'original_failure_retained': True,
                            'typed_postcheck_evidence': True, 'original_operation_id': True},
                  actual={'provider_calls': len(provider.calls), 'retry_calls': len(provider.calls)-1,
                          'original_failure_retained': result.failure_kind is original.failure_kind,
                          'original_public_result': public_result_metadata(original),
                          'returned_public_result': public_result_metadata(result),
                          'operation_records': list(wrapper.operation_records),
                          'events': [{'kind': event.kind, 'failure_code': event.failure_code,
                                      'operation_id': event.operation_id} for event in events]},
                  commands=provider.commands, ac=['AC02', 'AC04'], tgt=['TGT-27'], bind=['BIND-03'])
