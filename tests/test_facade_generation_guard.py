"""BIND-03/TGT-27: actual guarded composer/generation with synthetic CDP only.

Faults occur after exactly one synthetic Send activation. Counts and fixed
metadata are evidence; no prompt, message text, raw exception or endpoint URL is
exported. The default 900 second generation wait is deliberately exercised with
sleep trapped so an observation fault must terminate immediately.
"""
from __future__ import annotations

import pytest

from chatgpt_web_api import api, composer, generation
from chatgpt_web_api.models import ChatIdentity, FailureKind, GenerationState
from chatgpt_web_api.target_guard import GuardError
from strict_fakes import SimplePage, make_bound_session


class SubmissionPage(SimplePage):
    def __init__(self, fault=None):
        super().__init__(url='https://chatgpt.com/g/g-p-synthetic/c/synthetic-conversation')
        self.fault = fault
        self.session = None
        self.clicks = 0
        self.generation_observations = 0
        self.message_observations = 0
        self.cleanup_calls = 0
        self.original_error = RuntimeError('synthetic observation fault')
        self.original_click_result = {
            'ok': True, 'activationAttempted': True, 'trustedClick': True,
            'trustedClickEventCount': 1, 'clickEventCount': 1,
            'locatorCount': 1, 'reason': 'trusted-click-delivered',
        }

    def evaluate(self, expression, arg=None):
        if 'SHIRABE_COMPOSER_OBSERVE_V1' in expression:
            return {'present': True, 'visible': True, 'editable': True,
                    'empty': True, 'textLength': 0, 'candidateCount': 1}
        if 'SHIRABE_PROMPT_INPUT_V1' in expression:
            return {'ok': True, 'inputMatchesPrompt': True}
        if 'SHIRABE_SEND_ACTION_SELECT_V2' in expression:
            if self.fault == 'before_activation':
                self.session.fixture_authority.invalidate('TARGET_BINDING_INVALID')
            return {'ok': True, 'inputMatchesPrompt': True,
                    'sendLogicalActionCount': 1, 'reason': 'send-action-selected'}
        if "removeAttribute('data-shirabe-send-action')" in expression:
            self.cleanup_calls += 1
            if self.fault == 'cleanup':
                raise self.original_error
            return None
        if 'SHIRABE_GENERATION_PROBE_INSTALL_V1' in expression:
            return {'installed': True}
        if 'SHIRABE_GENERATION_OBSERVE_V1' in expression:
            self.generation_observations += 1
            if self.fault == 'generation':
                raise self.original_error
            return {'stopVisible': False, 'streamingVisible': False,
                    'userStopRequested': False, 'stopClickCount': 0}
        if 'SHIRABE_MESSAGE_OBSERVE_V1' in expression:
            self.message_observations += 1
            if ((self.fault == 'post_send_messages' and self.clicks) or
                    (self.fault == 'stale_messages' and self.message_observations >= 5)):
                raise self.original_error
            rows = []
            if self.clicks:
                rows.append({'role': 'user', 'text': 'synthetic', 'rawId': 'synthetic-user-1'})
            if self.generation_observations:
                rows.append({'role': 'assistant', 'text': 'synthetic-response', 'rawId': 'synthetic-assistant-1'})
            return {'url': self.url, 'rows': rows}
        raise AssertionError('UNEXPECTED_SYNTHETIC_EXPRESSION')

    def trusted_click_marked_action(self, selector, *, timeout_ms):
        self.clicks += 1
        if self.fault == 'activation_callback':
            raise self.original_error
        if self.fault == 'activation_postguard':
            self.session.fixture_authority.invalidate('TARGET_BINDING_INVALID')
        return self.original_click_result


def fixture(fault=None):
    page = SubmissionPage(fault)
    session = make_bound_session(page=page)
    page.session = session
    assert session.bind_chatgpt_page().ok
    chat = ChatIdentity(project_key='g-p-synthetic', url=page.url,
                        conversation_id='synthetic-conversation', client_ref='synthetic-ref')
    return page, session, chat


def report(evidence_case, page, session, expected, actual):
    evidence_case(expected=expected, actual=actual,
                  commands=session.fixture_transport.audit,
                  ac=['AC01', 'AC02', 'AC04'], bind=['BIND-03'], tgt=['TGT-27'])


@pytest.mark.parametrize('fault,trusted', [
    ('activation_postguard', True), ('activation_callback', False),
    ('cleanup', True), ('post_send_messages', True),
])
def test_post_activation_fault_preserves_uncertain_send_and_dispatches_no_more(fault, trusted, monkeypatch, evidence_case):
    page, session, chat = fixture(fault)
    sleeps = []
    monkeypatch.setattr(generation.time, 'sleep', lambda seconds: sleeps.append(seconds))
    facade = api.create_facade(session=session)
    result = facade.execute_prompt_and_wait(chat=chat, prompt='synthetic', attachments=())
    assert page.clicks == 1 and result.submission.activation_attempted
    assert result.submission.trusted_click is trusted
    assert not result.completed and not result.submission.web_submit_established
    assert result.failure_kind is FailureKind.RECONCILE_REQUIRED
    assert result.generation_state is GenerationState.UNKNOWN
    assert page.generation_observations == 0 and not sleeps
    before = len(session.fixture_transport.primitive_commands)
    second = facade.execute_prompt_and_wait(chat=chat, prompt='synthetic', attachments=())
    assert not second.completed
    assert len(session.fixture_transport.primitive_commands) == before
    assert page.clicks == 1
    if fault == 'activation_postguard':
        assert page.cleanup_calls == 0
        assert page.original_click_result['activationAttempted'] is True
        assert page.original_click_result['trustedClick'] is True
    if fault == 'activation_callback':
        assert page.original_error.dispatched is True
    report(evidence_case, page, session,
           {'send_calls': 1, 'resend_calls': 0, 'activation_attempted': True,
            'trusted_click': trusted, 'completed': False, 'new_mutation': 0,
            'generation_polls': 0, 'sleep_calls': 0},
           {'send_calls': page.clicks, 'resend_calls': page.clicks-1,
            'activation_attempted': result.submission.activation_attempted,
            'trusted_click': result.submission.trusted_click, 'completed': result.completed,
            'new_mutation': len(session.fixture_transport.primitive_commands)-before,
            'generation_polls': page.generation_observations, 'sleep_calls': len(sleeps)})


def test_guard_rejection_before_activation_keeps_send_zero(monkeypatch, evidence_case):
    page, session, chat = fixture('before_activation')
    sleeps = []
    monkeypatch.setattr(generation.time, 'sleep', lambda seconds: sleeps.append(seconds))
    result = api.create_facade(session=session).execute_prompt_and_wait(chat=chat, prompt='synthetic', attachments=())
    assert page.clicks == 0 and not result.submission.activation_attempted
    assert not result.completed and page.generation_observations == 0 and not sleeps
    report(evidence_case, page, session,
           {'send_calls': 0, 'activation_attempted': False, 'generation_polls': 0},
           {'send_calls': page.clicks, 'activation_attempted': result.submission.activation_attempted,
            'generation_polls': page.generation_observations})


def test_generation_observation_unknown_stops_immediately_and_preserves_submission(monkeypatch, evidence_case):
    page, session, chat = fixture('generation')
    captured = []
    original_submit = generation.submit_prompt
    def capture_submission(*args, **kwargs):
        value = original_submit(*args, **kwargs)
        captured.append(value)
        return value
    monkeypatch.setattr(generation, 'submit_prompt', capture_submission)
    sleeps = []
    monkeypatch.setattr(generation.time, 'sleep', lambda seconds: sleeps.append(seconds))
    result = generation.submit_prompt_and_wait_for_response(session, chat, 'synthetic')
    assert len(captured) == 1 and result.submission is captured[0] and result.submission.ok
    assert result.submission.evidence.details['activation_attempted']
    assert result.generation_state is GenerationState.UNKNOWN and not result.completed
    assert result.failure is FailureKind.RECONCILE_REQUIRED
    assert page.clicks == 1 and page.generation_observations == 1 and not sleeps
    with pytest.raises(GuardError):
        session.check_operation(mutation=True)
    report(evidence_case, page, session,
           {'send_calls': 1, 'original_submission': True, 'generation_polls': 1,
            'completed': False, 'sleep_calls': 0, 'new_mutation': 0},
           {'send_calls': page.clicks, 'original_submission': result.submission is captured[0],
            'generation_polls': page.generation_observations, 'completed': result.completed,
            'sleep_calls': len(sleeps), 'new_mutation': 0})


def test_failed_message_observation_cannot_reuse_stale_snapshot_as_completion(monkeypatch, evidence_case):
    page, session, chat = fixture('stale_messages')
    sleeps = []
    monkeypatch.setattr(generation.time, 'sleep', lambda seconds: sleeps.append(seconds))
    result = generation.submit_prompt_and_wait_for_response(
        session, chat, 'synthetic', stable_samples_required=2,
    )
    assert page.clicks == 1 and page.generation_observations == 2
    assert result.response is not None and not result.completed
    assert result.generation_state is GenerationState.UNKNOWN
    assert result.failure is FailureKind.RECONCILE_REQUIRED and len(sleeps) == 1
    with pytest.raises(GuardError):
        session.check_operation(mutation=True)
    report(evidence_case, page, session,
           {'send_calls': 1, 'generation_polls': 2, 'completed': False,
            'partial_response_retained': True, 'stable_samples': 1, 'sleep_calls': 1},
           {'send_calls': page.clicks, 'generation_polls': page.generation_observations,
            'completed': result.completed, 'partial_response_retained': result.response is not None,
            'stable_samples': result.evidence.details['stable_samples'], 'sleep_calls': len(sleeps)})


def test_unexpected_post_submission_exception_is_preserved_without_resend(monkeypatch, evidence_case):
    page, session, chat = fixture()
    original = RuntimeError('synthetic generation observer exception')
    def raising(_session):
        raise original
    monkeypatch.setattr(generation, 'observe_generation', raising)
    with pytest.raises(RuntimeError) as caught:
        api.create_facade(session=session).execute_prompt_and_wait(chat=chat, prompt='synthetic', attachments=())
    assert caught.value is original and page.clicks == 1
    with pytest.raises(GuardError):
        session.check_operation(mutation=True)
    report(evidence_case, page, session,
           {'send_calls': 1, 'original_exception': True, 'resend_calls': 0, 'new_mutation': 0},
           {'send_calls': page.clicks, 'original_exception': caught.value is original,
            'resend_calls': page.clicks-1, 'new_mutation': 0})
