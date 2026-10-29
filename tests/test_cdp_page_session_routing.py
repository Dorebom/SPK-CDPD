"""Synthetic wire audit for exact-session Page primitives; no live browser."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from chatgpt_web_api.cdp_page import CdpPage
from chatgpt_web_api.target_guard import GuardError


class FakeChannel:
    def __init__(self, owner, identifier):
        self.owner, self.identifier = owner, identifier
        self.listeners = {}
        self.commands = []

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        if callback in self.listeners.get(event, []):
            self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in tuple(self.listeners.get(event, [])):
            callback(value)

    def send(self, method, parameters):
        self.commands.append((method, dict(parameters)))
        self.owner.commands.append({'method': method, 'session_id': self.identifier,
                                    'target_id': 'SYNTHETIC_A' if self is self.owner.target else 'BROWSER',
                                    'binding_id': 'synthetic-binding-a', 'epoch': 'a' * 32,
                                    'dispatch_count': 1})
        if self.owner.on_send:
            self.owner.on_send(self, method, parameters)
        if method == 'Runtime.enable':
            self.owner.context()
        if method == 'Runtime.evaluate':
            return {'result': {'type': 'object', 'value': self.owner.result}}
        if method == 'DOM.describeNode':
            return {'node': {'backendNodeId': 7, 'localName': 'input', 'attributes': ['type', 'file', 'accept', '.zip']}}
        if method == 'Page.captureScreenshot':
            return {'data': base64.b64encode(b'synthetic-png').decode('ascii')}
        if method == 'Page.navigate':
            self.owner.target.emit('Runtime.executionContextsCleared', {})
            self.owner.identity = ('FRAME_A', 'LOADER_B', parameters['url'], 'https://chatgpt.com')
            self.owner.unique = 'unique-context-b'
            self.owner.context()
            self.owner.target.emit('Page.lifecycleEvent', {'frameId': 'FRAME_A', 'loaderId': 'LOADER_B', 'name': 'DOMContentLoaded'})
            return {'frameId': 'FRAME_A', 'loaderId': 'LOADER_B'}
        return {}


class Harness:
    def __init__(self):
        self.denied = False
        self.identity = ('FRAME_A', 'LOADER_A', 'https://chatgpt.com/', 'https://chatgpt.com')
        self.unique = 'unique-context-a'
        self.commands = []
        self.dispatches = []
        self.on_send = None
        self.result = {'ok': True}
        self.target = FakeChannel(self, 'PINNED_TARGET_SESSION')
        self.browser = FakeChannel(self, 'APPROVED_BROWSER_SESSION')
        class ForbiddenRawPage:
            def __getattribute__(self, name):
                raise AssertionError('raw Page primitive accessed')
        self.page = CdpPage(self.target, self.browser, dispatch=self.dispatch, metadata_check=self.metadata,
                            target_id='SYNTHETIC_A', expected_origin='https://chatgpt.com',
                            raw_page=ForbiddenRawPage(), browser_context_id='APPROVED_CONTEXT')
        self.page.initialize()

    def context(self):
        self.target.emit('Runtime.executionContextCreated', {'context': {
            'id': 101, 'uniqueId': self.unique, 'origin': 'https://chatgpt.com',
            'auxData': {'isDefault': True, 'frameId': self.identity[0]},
        }})

    def metadata(self):
        if self.denied:
            raise GuardError('BINDING_REVOKED')
        return self.identity

    def dispatch(self, name, callback, *, mutation, navigation=False):
        if self.denied:
            raise GuardError('BINDING_REVOKED')
        self.dispatches.append({'name': name, 'mutation': mutation, 'navigation': navigation})
        result = callback()
        if self.denied:
            raise GuardError('BINDING_REVOKED')
        return result


def evidence(evidence_case, h, expected, actual, *, tgt=('TGT-19', 'TGT-24'), ac=('AC01', 'AC02')):
    evidence_case(expected=expected, actual=actual, commands=h.commands,
                  ac=list(ac), tgt=list(tgt), bind=['BIND-01', 'BIND-02'])


def count(h, method):
    return sum(m == method for m, _ in h.target.commands)


def test_evaluate_uses_verified_unique_context_and_same_target_session(evidence_case):
    h = Harness()
    value = h.page.evaluate('x => x', {'ok': True})
    assert value == {'ok': True}
    params = next(params for method, params in h.target.commands if method == 'Runtime.evaluate')
    assert params['uniqueContextId'] == 'unique-context-a' and 'contextId' not in params
    assert params['allowUnsafeEvalBlockedByCSP'] is False
    assert 'location.origin' in params['expression']
    assert not h.browser.commands
    evidence(evidence_case, h, {'Runtime.evaluate': 1, 'wrong_session': 0, 'exact_unique_context': True},
             {'Runtime.evaluate': count(h, 'Runtime.evaluate'), 'wrong_session': len(h.browser.commands), 'exact_unique_context': params['uniqueContextId'] == h.unique})


@pytest.mark.parametrize('event,data', [
    ('Runtime.executionContextsCleared', {}),
    ('Runtime.executionContextDestroyed', {'executionContextUniqueId': 'unique-context-a'}),
])
def test_missing_context_refuses_without_default_context_fallback(event, data, evidence_case):
    h = Harness()
    h.target.emit(event, data)
    with pytest.raises(GuardError):
        h.page.evaluate('() => 1')
    assert count(h, 'Runtime.evaluate') == 0
    evidence(evidence_case, h, {'Runtime.evaluate': 0, 'fallback': 0},
             {'Runtime.evaluate': count(h, 'Runtime.evaluate'), 'fallback': 0})


def test_context_lost_during_evaluation_is_not_retried(evidence_case):
    h = Harness()
    def lose(channel, method, params):
        if method == 'Runtime.evaluate':
            channel.emit('Runtime.executionContextsCleared', {})
    h.on_send = lose
    with pytest.raises(GuardError):
        h.page.evaluate('() => 1')
    assert count(h, 'Runtime.evaluate') == 1
    evidence(evidence_case, h, {'Runtime.evaluate': 1, 'retry': 0},
             {'Runtime.evaluate': count(h, 'Runtime.evaluate'), 'retry': count(h, 'Runtime.evaluate')-1}, tgt=('TGT-27',))


@pytest.mark.parametrize('primitive', ['evaluate', 'click', 'goto', 'screenshot', 'upload', 'download'])
def test_cached_cdp_page_has_zero_dispatch_after_invalidation(primitive, tmp_path, evidence_case):
    h = Harness()
    before = len(h.commands)
    h.denied = True
    with pytest.raises(GuardError):
        if primitive == 'evaluate':
            h.page.evaluate('() => 1')
        elif primitive == 'click':
            h.page.locator('[data-synthetic]').click()
        elif primitive == 'goto':
            h.page.goto('https://chatgpt.com/c/synthetic')
        elif primitive == 'screenshot':
            h.page.screenshot(path=str(tmp_path / 'never.png'))
        elif primitive == 'upload':
            with h.page.expect_file_chooser(timeout=10):
                pass
        else:
            with h.page.expect_download(timeout=10):
                pass
    assert len(h.commands) == before
    evidence(evidence_case, h, {'new_dispatch': 0}, {'new_dispatch': len(h.commands)-before}, tgt=('TGT-14', 'TGT-24'))


def test_trusted_click_is_one_press_release_on_exact_target_session(evidence_case):
    h = Harness()
    h.result = {'ok': True, 'x': 5, 'y': 7}
    h.page.locator('[data-synthetic]').click()
    kinds = [params['type'] for method, params in h.target.commands if method == 'Input.dispatchMouseEvent']
    assert kinds == ['mouseMoved', 'mousePressed', 'mouseReleased']
    assert not h.browser.commands
    evidence(evidence_case, h, {'press': 1, 'release': 1, 'wrong_session': 0},
             {'press': kinds.count('mousePressed'), 'release': kinds.count('mouseReleased'), 'wrong_session': len(h.browser.commands)}, tgt=('TGT-27',))


def test_invalidation_after_press_stops_every_remaining_primitive(evidence_case):
    h = Harness()
    h.result = {'ok': True, 'x': 5, 'y': 7}
    def invalidate(channel, method, params):
        if method == 'Input.dispatchMouseEvent' and params['type'] == 'mousePressed':
            h.denied = True
    h.on_send = invalidate
    with pytest.raises(GuardError):
        h.page.locator('[data-synthetic]').click()
    kinds = [params['type'] for method, params in h.target.commands if method == 'Input.dispatchMouseEvent']
    assert kinds == ['mouseMoved', 'mousePressed']
    evidence(evidence_case, h, {'press': 1, 'release_after_invalidation': 0, 'retry': 0},
             {'press': kinds.count('mousePressed'), 'release_after_invalidation': kinds.count('mouseReleased'), 'retry': kinds.count('mousePressed')-1}, tgt=('TGT-27',))


def test_file_chooser_uses_exact_main_frame_backend_node_once(tmp_path, evidence_case):
    h = Harness()
    source = tmp_path / 'synthetic.zip'
    source.write_bytes(b'synthetic')
    with h.page.expect_file_chooser(timeout=10) as wait:
        h.target.emit('Page.fileChooserOpened', {'frameId': 'FRAME_A', 'backendNodeId': 7, 'mode': 'selectSingle'})
    assert wait.value is not None and not wait.value.is_multiple()
    assert wait.value.element.get_attribute('accept') == '.zip'
    wait.value.set_files([str(source)])
    with pytest.raises(GuardError):
        wait.value.set_files([str(source)])
    calls = [params for method, params in h.target.commands if method == 'DOM.setFileInputFiles']
    assert len(calls) == 1 and calls[0]['backendNodeId'] == 7
    assert not h.browser.commands
    evidence(evidence_case, h, {'file_assignment': 1, 'exact_backend_node': 7, 'retry': 0},
             {'file_assignment': len(calls), 'exact_backend_node': calls[0]['backendNodeId'], 'retry': len(calls)-1})


def test_wrong_frame_file_chooser_is_rejected_without_assignment(evidence_case):
    h = Harness()
    with pytest.raises(GuardError) as caught:
        with h.page.expect_file_chooser(timeout=10):
            h.target.emit('Page.fileChooserOpened', {'frameId': 'OTHER_FRAME', 'backendNodeId': 9, 'mode': 'selectSingle'})
    assert caught.value.code == 'TARGET_BINDING_INVALID'
    assert count(h, 'DOM.setFileInputFiles') == 0
    evidence(evidence_case, h, {'file_assignment': 0, 'reason': 'TARGET_BINDING_INVALID'},
             {'file_assignment': count(h, 'DOM.setFileInputFiles'), 'reason': caught.value.code})


def test_cached_chooser_assignment_is_denied_after_navigation(tmp_path, evidence_case):
    h = Harness()
    with h.page.expect_file_chooser(timeout=10) as wait:
        h.target.emit('Page.fileChooserOpened', {'frameId': 'FRAME_A', 'backendNodeId': 7, 'mode': 'selectSingle'})
    h.identity = ('FRAME_A', 'LOADER_REPLACED', 'https://chatgpt.com/c/other', 'https://chatgpt.com')
    with pytest.raises(GuardError):
        wait.value.set_files([str(tmp_path / 'never.zip')])
    assert count(h, 'DOM.setFileInputFiles') == 0
    evidence(evidence_case, h, {'file_assignment': 0}, {'file_assignment': count(h, 'DOM.setFileInputFiles')})


def test_download_correlates_main_frame_guid_without_url_fetch_or_raw_page(tmp_path, evidence_case):
    h = Harness()
    transfer = {}
    def delivery(channel, method, params):
        if method == 'Browser.setDownloadBehavior' and params['behavior'] == 'allowAndName':
            assert params['browserContextId'] == 'APPROVED_CONTEXT'
            transfer['directory'] = Path(params['downloadPath'])
        if method == 'Runtime.evaluate':
            h.browser.emit('Browser.downloadWillBegin', {'frameId': 'FRAME_A', 'guid': 'guid-a',
                                                         'suggestedFilename': 'synthetic.zip', 'url': 'discarded-synthetic-secret'})
            (transfer['directory'] / 'guid-a').write_bytes(b'synthetic-download')
            h.browser.emit('Browser.downloadProgress', {'guid': 'guid-a', 'state': 'completed'})
    h.on_send = delivery
    with h.page.expect_download(timeout=10) as wait:
        h.page.evaluate('() => ({ok:true})')
    assert wait.value.suggested_filename == 'synthetic.zip'
    assert wait.value.failure() is None
    destination = tmp_path / 'delivered.zip'
    wait.value.save_as(str(destination))
    assert destination.read_bytes() == b'synthetic-download'
    assert not any(method.startswith('Network.') or method == 'Page.getResourceContent' for method, _ in h.target.commands + h.browser.commands)
    assert not hasattr(wait.value, 'url')
    evidence(evidence_case, h, {'action_evaluation': 1, 'local_save': 1, 'url_fetch': 0, 'browser_context_pinned': True},
             {'action_evaluation': count(h, 'Runtime.evaluate'), 'local_save': int(destination.exists()), 'url_fetch': 0, 'browser_context_pinned': True})


def test_download_event_from_other_frame_is_rejected(evidence_case):
    h = Harness()
    def delivery(channel, method, params):
        if method == 'Runtime.evaluate':
            h.browser.emit('Browser.downloadWillBegin', {'frameId': 'OTHER_FRAME', 'guid': 'guid-b', 'suggestedFilename': 'synthetic.zip'})
    h.on_send = delivery
    with pytest.raises(GuardError) as caught:
        with h.page.expect_download(timeout=10):
            h.page.evaluate('() => ({ok:true})')
    assert caught.value.code == 'TARGET_BINDING_INVALID'
    assert not any(item['name'] == 'LOCAL_DOWNLOAD_SAVE' for item in h.dispatches)
    evidence(evidence_case, h, {'local_save': 0, 'reason': 'TARGET_BINDING_INVALID'},
             {'local_save': 0, 'reason': caught.value.code})


def test_download_event_before_action_is_not_misattributed(evidence_case):
    h = Harness()
    def old_event(channel, method, params):
        if method == 'Browser.setDownloadBehavior' and params['behavior'] == 'allowAndName':
            h.browser.emit('Browser.downloadWillBegin', {'frameId': 'FRAME_A', 'guid': 'old-guid', 'suggestedFilename': 'synthetic.zip'})
    h.on_send = old_event
    with pytest.raises(GuardError):
        with h.page.expect_download(timeout=10):
            h.page.evaluate('() => ({ok:true})')
    assert count(h, 'Runtime.evaluate') == 0
    evidence(evidence_case, h, {'action_evaluation': 0, 'local_save': 0},
             {'action_evaluation': count(h, 'Runtime.evaluate'), 'local_save': 0})


def test_explicit_same_target_navigation_uses_one_page_command_and_new_verified_context(evidence_case):
    h = Harness()
    h.page.goto('https://chatgpt.com/c/synthetic', timeout=10)
    h.page.evaluate('() => ({ok:true})')
    assert count(h, 'Page.navigate') == 1
    params = next(params for method, params in h.target.commands if method == 'Runtime.evaluate')
    assert params['uniqueContextId'] == 'unique-context-b'
    assert h.page.target_id == 'SYNTHETIC_A'
    evidence(evidence_case, h, {'Page.navigate': 1, 'target_unchanged': True, 'new_context_verified': True, 'rebind': 0},
             {'Page.navigate': count(h, 'Page.navigate'), 'target_unchanged': h.page.target_id == 'SYNTHETIC_A',
              'new_context_verified': params['uniqueContextId'] == h.unique, 'rebind': 0})


def test_navigation_to_unapproved_origin_never_dispatches(evidence_case):
    h = Harness()
    with pytest.raises(GuardError) as caught:
        h.page.goto('https://example.invalid/')
    assert caught.value.code == 'TARGET_ORIGIN_MISMATCH'
    assert count(h, 'Page.navigate') == 0
    evidence(evidence_case, h, {'Page.navigate': 0, 'reason': 'TARGET_ORIGIN_MISMATCH'},
             {'Page.navigate': count(h, 'Page.navigate'), 'reason': caught.value.code})
