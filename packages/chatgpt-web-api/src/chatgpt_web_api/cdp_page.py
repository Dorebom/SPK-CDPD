"""Page primitives pinned to the same CDP Target session used for identity checks.

This private adapter intentionally does not use Playwright Page/Locator action
methods. Browser-level download control uses the already bound browser channel;
DOM, Runtime, Input and Page commands use the one persistent Target channel.
Every dispatch is delegated to the owning session's pre/post guard. A sequence
of CDP checks is not an atomic browser transaction.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Callable

from .target_guard import GuardError, normalize_origin


def _timeout() -> BaseException:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        return PlaywrightTimeoutError('CDP_EVENT_TIMEOUT')
    except ImportError:
        return TimeoutError('CDP_EVENT_TIMEOUT')


def _milliseconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('INVALID_TIMEOUT')
    return max(1, int(value))


class CdpPage:
    """Constrained internal Page surface over one exact CDPSession."""

    def __init__(self, cdp: Any, browser_cdp: Any, *, dispatch: Callable[..., Any],
                 metadata_check: Callable[[], tuple[str, str, str, str]], target_id: str,
                 expected_origin: str, raw_page: Any = None,
                 browser_context_id: str | None = None,
                 navigation_complete: Callable[[], None] | None = None) -> None:
        self._cdp = cdp
        self._browser_cdp = browser_cdp
        self._dispatch = dispatch
        self._metadata_check = metadata_check
        self.target_id = target_id
        self.expected_origin = expected_origin
        self.browser_context_id = browser_context_id
        self._navigation_complete = navigation_complete
        # raw_page is deliberately never retained or used for browser actions.
        self._contexts: dict[str, dict[str, Any]] = {}
        self._context_error: str | None = None
        self._last_identity: tuple[str, str, str, str] | None = None
        self._lifecycle: set[tuple[str, str, str]] = set()
        self._initialised = False
        self._download_wait: _DownloadWait | None = None
        cdp.on('Runtime.executionContextCreated', self._context_created)
        cdp.on('Runtime.executionContextDestroyed', self._context_destroyed)
        cdp.on('Runtime.executionContextsCleared', self._contexts_cleared)
        cdp.on('Page.lifecycleEvent', self._lifecycle_event)

    def initialize(self) -> None:
        """Call only after the owner has fixed Page/Target/session correspondence."""
        if self._initialised:
            return
        self._fresh_identity()
        self._send('Page.enable', {}, mutation=False)
        self._send('Page.setLifecycleEventsEnabled', {'enabled': True}, mutation=False)
        self._send('Runtime.enable', {}, mutation=False)
        self._context()
        self._initialised = True

    def _context_created(self, event: dict[str, Any]) -> None:
        context = event.get('context') if isinstance(event, dict) else None
        if not isinstance(context, dict):
            self._context_error = 'TARGET_UNVERIFIED'
            return
        aux = context.get('auxData')
        if not isinstance(aux, dict):
            self._context_error = 'TARGET_UNVERIFIED'
            return
        if aux.get('isDefault') is not True:
            return
        unique, frame, identifier = context.get('uniqueId'), aux.get('frameId'), context.get('id')
        if not isinstance(unique, str) or not unique or not isinstance(frame, str) or not frame or type(identifier) is not int:
            self._context_error = 'TARGET_UNVERIFIED'
            return
        if unique in self._contexts or len(self._contexts) >= 256:
            self._context_error = 'TARGET_UNVERIFIED'
            return
        self._contexts[unique] = {'frame': frame, 'id': identifier, 'origin': context.get('origin')}

    def _context_destroyed(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            self._context_error = 'TARGET_UNVERIFIED'
            return
        unique = event.get('executionContextUniqueId')
        identifier = event.get('executionContextId')
        for key, value in tuple(self._contexts.items()):
            if key == unique or (type(identifier) is int and value['id'] == identifier):
                del self._contexts[key]

    def _contexts_cleared(self, _event: Any = None) -> None:
        self._contexts.clear()

    def _lifecycle_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        frame, loader, name = event.get('frameId'), event.get('loaderId'), event.get('name')
        if all(isinstance(value, str) and value for value in (frame, loader, name)):
            if len(self._lifecycle) >= 256:
                self._lifecycle.clear()
            self._lifecycle.add((frame, loader, name))

    def _fresh_identity(self) -> tuple[str, str, str, str]:
        identity = self._metadata_check()
        if not isinstance(identity, tuple) or len(identity) != 4 or not all(isinstance(value, str) and value for value in identity):
            raise GuardError('TARGET_UNVERIFIED')
        if identity[3] != self.expected_origin or normalize_origin(identity[2]) != self.expected_origin:
            raise GuardError('TARGET_ORIGIN_MISMATCH')
        self._last_identity = identity
        return identity

    def _context(self) -> tuple[tuple[str, str, str, str], str]:
        identity = self._fresh_identity()
        if self._context_error:
            raise GuardError(self._context_error)
        candidates = [(key, value) for key, value in self._contexts.items() if value['frame'] == identity[0]]
        if len(candidates) != 1:
            raise GuardError('TARGET_UNVERIFIED')
        unique, context = candidates[0]
        if context['origin'] != self.expected_origin:
            raise GuardError('TARGET_ORIGIN_MISMATCH')
        return identity, unique

    def _send(self, method: str, params: dict[str, Any], *, mutation: bool,
              navigation: bool = False, browser: bool = False) -> Any:
        channel = self._browser_cdp if browser else self._cdp
        return self._dispatch(method, lambda: channel.send(method, params),
                              mutation=mutation, navigation=navigation)

    def _check_snapshot(self, snapshot: tuple[tuple[str, str, str, str], str]) -> None:
        if self._context() != snapshot:
            raise GuardError('TARGET_UNVERIFIED')

    def _wait(self, predicate: Callable[[], bool], timeout: int) -> None:
        deadline = time.monotonic() + _milliseconds(timeout) / 1000
        while not predicate():
            if time.monotonic() >= deadline:
                raise _timeout()
            # Fresh metadata pumps the same sync CDP channel and processes its
            # events. Only observation is repeated; no mutation is replayed.
            self._fresh_identity()
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    @property
    def url(self) -> str:
        return self._fresh_identity()[2]

    def title(self) -> str:
        return str(self._evaluate('() => document.title', None, mutation=False) or '')

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        # Existing Web scripts may write markers/listeners or dispatch an action;
        # unknown script intent therefore requires mutation authority.
        return self._evaluate(expression, arg, mutation=True)

    def _evaluate(self, expression: str, arg: Any, *, mutation: bool) -> Any:
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError('INVALID_EXPRESSION')
        snapshot = self._context()
        encoded_arg = json.dumps(arg, ensure_ascii=True, allow_nan=False)
        encoded_origin = json.dumps(self.expected_origin)
        invoke = f'__operation({encoded_arg})' if arg is not None else '__operation()'
        wrapped = (
            '(() => { if (location.origin !== ' + encoded_origin + ") throw new Error('TARGET_ORIGIN_MISMATCH'); "
            'const __operation = (' + expression + '); return typeof __operation === "function" ? '
            + invoke + ' : __operation; })()'
        )
        if self._download_wait is not None:
            self._download_wait.before_action()
        result = self._send('Runtime.evaluate', {
            'expression': wrapped, 'uniqueContextId': snapshot[1],
            'returnByValue': True, 'awaitPromise': True, 'timeout': 10000,
            'allowUnsafeEvalBlockedByCSP': False,
        }, mutation=mutation)
        self._check_snapshot(snapshot)
        if not isinstance(result, dict) or result.get('exceptionDetails'):
            raise GuardError('CDP_COMMAND_FAILED')
        remote = result.get('result')
        if not isinstance(remote, dict) or 'unserializableValue' in remote:
            raise GuardError('CDP_COMMAND_FAILED')
        if remote.get('type') == 'undefined' or remote.get('subtype') == 'null':
            return None
        if 'value' not in remote:
            raise GuardError('CDP_COMMAND_FAILED')
        return remote['value']

    def locator(self, selector: str) -> '_Locator':
        if not isinstance(selector, str) or not selector:
            raise ValueError('INVALID_SELECTOR')
        return _Locator(self, selector)

    def goto(self, url: str, *, wait_until: str = 'domcontentloaded', timeout: int = 30000) -> None:
        if normalize_origin(url) != self.expected_origin:
            raise GuardError('TARGET_ORIGIN_MISMATCH')
        if wait_until not in ('domcontentloaded', 'load'):
            raise ValueError('UNSUPPORTED_NAVIGATION_WAIT')
        timeout = _milliseconds(timeout)
        before = self._fresh_identity()
        response = self._send('Page.navigate', {'url': url, 'frameId': before[0]}, mutation=True, navigation=True)
        if not isinstance(response, dict) or response.get('errorText') or response.get('isDownload'):
            raise GuardError('CDP_COMMAND_FAILED')
        if response.get('frameId') != before[0]:
            raise GuardError('TARGET_BINDING_INVALID')
        loader = response.get('loaderId')
        if loader is None:  # same-document navigation does not create a loader
            self._context()
            if self._navigation_complete is not None:
                self._navigation_complete()
            return
        if not isinstance(loader, str) or not loader:
            raise GuardError('TARGET_UNVERIFIED')
        event_name = 'DOMContentLoaded' if wait_until == 'domcontentloaded' else 'load'
        self._wait(lambda: (before[0], loader, event_name) in self._lifecycle, timeout)
        after, _unique = self._context()
        if after[0] != before[0] or after[1] != loader:
            raise GuardError('TARGET_UNVERIFIED')
        if self._navigation_complete is not None:
            self._navigation_complete()

    def screenshot(self, *, path: str, full_page: bool = False) -> bytes:
        if full_page:
            raise ValueError('FULL_PAGE_CAPTURE_UNSUPPORTED')
        snapshot = self._context()
        response = self._send('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False}, mutation=False)
        self._check_snapshot(snapshot)
        try:
            data = base64.b64decode(response['data'], validate=True)
        except (KeyError, ValueError, TypeError):
            raise GuardError('CDP_COMMAND_FAILED') from None
        self._dispatch('LOCAL_SCREENSHOT_SAVE', lambda: Path(path).write_bytes(data), mutation=False)
        return data

    def expect_file_chooser(self, *, timeout: int = 30000) -> '_ChooserWait':
        return _ChooserWait(self, _milliseconds(timeout))

    def expect_download(self, *, timeout: int = 30000) -> '_DownloadWait':
        return _DownloadWait(self, _milliseconds(timeout))


class _Locator:
    def __init__(self, page: CdpPage, selector: str) -> None:
        self._page, self._selector = page, selector

    def count(self) -> int:
        value = self._page._evaluate('selector => document.querySelectorAll(selector).length', self._selector, mutation=False)
        if type(value) is not int or value < 0:
            raise GuardError('CDP_COMMAND_FAILED')
        return value

    def click(self, *, timeout: int = 30000) -> None:
        _milliseconds(timeout)
        snapshot = self._page._context()
        position = self._page._evaluate(r'''selector => {
            const nodes = document.querySelectorAll(selector);
            if (nodes.length !== 1) return {ok:false};
            const element = nodes[0];
            if (element.disabled || element.getAttribute('aria-disabled') === 'true') return {ok:false};
            element.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
            const hit = document.elementFromPoint(x, y);
            return {ok:rect.width>0 && rect.height>0 && style.visibility!=='hidden'
                       && style.display!=='none' && !!hit && (hit===element || element.contains(hit)),x,y};
        }''', self._selector, mutation=True)
        if not isinstance(position, dict) or position.get('ok') is not True:
            raise RuntimeError('NOT_INTERACTABLE')
        x, y = position.get('x'), position.get('y')
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (x, y)):
            raise GuardError('CDP_COMMAND_FAILED')
        for kind, buttons in [('mouseMoved', 0), ('mousePressed', 1), ('mouseReleased', 0)]:
            self._page._check_snapshot(snapshot)
            self._page._send('Input.dispatchMouseEvent', {
                'type': kind, 'x': x, 'y': y, 'button': 'left' if kind != 'mouseMoved' else 'none',
                'buttons': buttons, 'clickCount': 1 if kind != 'mouseMoved' else 0,
            }, mutation=True)


class _ChooserWait:
    def __init__(self, page: CdpPage, timeout: int) -> None:
        self._page, self._timeout = page, timeout
        self._snapshot = page._context()
        self.value: _Chooser | None = None
        self._error: str | None = None

    def _event(self, event: dict[str, Any]) -> None:
        if self.value is not None:
            self._error = 'TARGET_UNVERIFIED'
            return
        if not isinstance(event, dict) or event.get('frameId') != self._snapshot[0][0]:
            self._error = 'TARGET_BINDING_INVALID'
            return
        node, mode = event.get('backendNodeId'), event.get('mode')
        if type(node) is not int or node < 1 or mode not in ('selectSingle', 'selectMultiple'):
            self._error = 'TARGET_UNVERIFIED'
            return
        self.value = _Chooser(self._page, self._snapshot, node, mode == 'selectMultiple')

    def __enter__(self) -> '_ChooserWait':
        self._page._cdp.on('Page.fileChooserOpened', self._event)
        try:
            self._page._send('Page.setInterceptFileChooserDialog', {'enabled': True}, mutation=True)
        except BaseException:
            self._page._cdp.remove_listener('Page.fileChooserOpened', self._event)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is None:
                self._page._wait(lambda: self.value is not None or self._error is not None, self._timeout)
                if self._error:
                    raise GuardError(self._error)
                self._page._check_snapshot(self._snapshot)
        finally:
            self._page._cdp.remove_listener('Page.fileChooserOpened', self._event)
            try:
                # Guard refusal means dispatch=0, including cleanup commands.
                self._page._send('Page.setInterceptFileChooserDialog', {'enabled': False}, mutation=True)
            except Exception:
                pass
        return False


class _Chooser:
    def __init__(self, page: CdpPage, snapshot: Any, node: int, multiple: bool) -> None:
        self._page, self._snapshot, self._node, self._multiple = page, snapshot, node, multiple
        self.element = self
        self._assigned = False

    def is_multiple(self) -> bool:
        self._page._check_snapshot(self._snapshot)
        return self._multiple

    def get_attribute(self, name: str) -> str | None:
        self._page._check_snapshot(self._snapshot)
        result = self._page._send('DOM.describeNode', {'backendNodeId': self._node, 'depth': 0}, mutation=False)
        self._page._check_snapshot(self._snapshot)
        node = result.get('node', {}) if isinstance(result, dict) else {}
        if node.get('backendNodeId') != self._node or node.get('localName') != 'input':
            raise GuardError('TARGET_UNVERIFIED')
        attributes = node.get('attributes')
        if not isinstance(attributes, list) or len(attributes) % 2 or not all(isinstance(x, str) for x in attributes):
            raise GuardError('TARGET_UNVERIFIED')
        attrs = dict(zip(attributes[::2], attributes[1::2]))
        if attrs.get('type', '').lower() != 'file':
            raise GuardError('TARGET_UNVERIFIED')
        return attrs.get(name)

    def set_files(self, files: list[str]) -> None:
        self._page._check_snapshot(self._snapshot)
        if self._assigned or not isinstance(files, (list, tuple)) or (not self._multiple and len(files) > 1):
            raise GuardError('RECONCILIATION_REQUIRED')
        paths = [str(Path(path).expanduser().resolve()) for path in files]
        self._assigned = True  # Even an uncertain command is never replayed.
        self._page._send('DOM.setFileInputFiles', {'files': paths, 'backendNodeId': self._node}, mutation=True)
        self._page._check_snapshot(self._snapshot)


class _DownloadWait:
    def __init__(self, page: CdpPage, timeout: int) -> None:
        self._page, self._timeout = page, timeout
        self._snapshot = page._context()
        self._directory = tempfile.TemporaryDirectory(prefix='cdpd-l04-download-')
        self._error: str | None = None
        self._action_started = False
        self.value: _Download | None = None
        self._closed = False

    def before_action(self) -> None:
        if self._error:
            raise GuardError(self._error)
        self._action_started = True

    def _begin(self, event: dict[str, Any]) -> None:
        if not self._action_started or self.value is not None:
            self._error = 'TARGET_UNVERIFIED'
            return
        if not isinstance(event, dict) or event.get('frameId') != self._snapshot[0][0]:
            self._error = 'TARGET_BINDING_INVALID'
            return
        guid, filename = event.get('guid'), event.get('suggestedFilename')
        if not isinstance(guid, str) or re.fullmatch(r'[A-Za-z0-9-]{1,128}', guid) is None or not isinstance(filename, str):
            self._error = 'TARGET_UNVERIFIED'
            return
        # Deliberately do not retain event.url (may contain credentials/query).
        self.value = _Download(self, guid, filename)

    def _progress(self, event: dict[str, Any]) -> None:
        if self.value is None or not isinstance(event, dict) or event.get('guid') != self.value._guid:
            return
        state = event.get('state')
        if state not in ('inProgress', 'completed', 'canceled'):
            self._error = 'TARGET_UNVERIFIED'
            return
        self.value._state = state

    def __enter__(self) -> '_DownloadWait':
        if self._page._download_wait is not None:
            raise GuardError('RECONCILIATION_REQUIRED')
        self._page._download_wait = self
        self._page._browser_cdp.on('Browser.downloadWillBegin', self._begin)
        self._page._browser_cdp.on('Browser.downloadProgress', self._progress)
        params: dict[str, Any] = {'behavior': 'allowAndName', 'downloadPath': self._directory.name, 'eventsEnabled': True}
        if self._page.browser_context_id is not None:
            params['browserContextId'] = self._page.browser_context_id
        try:
            self._page._send('Browser.setDownloadBehavior', params, mutation=True, browser=True)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._page._download_wait = None
        if exc_type is not None:
            self.close()
            return False
        try:
            self._page._wait(lambda: self.value is not None or self._error is not None, self._timeout)
            if self._error:
                raise GuardError(self._error)
            self._page._check_snapshot(self._snapshot)
        except BaseException:
            self.close()
            raise
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._page._browser_cdp.remove_listener('Browser.downloadWillBegin', self._begin)
        self._page._browser_cdp.remove_listener('Browser.downloadProgress', self._progress)
        if self._page._download_wait is self:
            self._page._download_wait = None
        try:
            params: dict[str, Any] = {'behavior': 'default', 'eventsEnabled': False}
            if self._page.browser_context_id is not None:
                params['browserContextId'] = self._page.browser_context_id
            self._page._send('Browser.setDownloadBehavior', params, mutation=True, browser=True)
        except Exception:
            pass


class _Download:
    def __init__(self, owner: _DownloadWait, guid: str, suggested_filename: str) -> None:
        self._owner, self._guid = owner, guid
        self.suggested_filename = suggested_filename
        self._state = 'inProgress'
        self._saved = False

    def _check(self) -> None:
        if self._owner._error:
            raise GuardError(self._owner._error)
        self._owner._page._check_snapshot(self._owner._snapshot)

    def failure(self) -> str | None:
        self._check()
        self._owner._page._wait(lambda: self._state in ('completed', 'canceled') or self._owner._error is not None,
                                self._owner._timeout)
        self._check()
        self._owner.close()
        return 'canceled' if self._state == 'canceled' else None

    def save_as(self, destination_path: str) -> None:
        self._check()
        if self._saved or self._state != 'completed':
            raise GuardError('RECONCILIATION_REQUIRED')
        source = Path(self._owner._directory.name) / self._guid
        if not source.is_file() or source.is_symlink():
            raise GuardError('CDP_COMMAND_FAILED')
        self._saved = True
        self._owner._page._dispatch('LOCAL_DOWNLOAD_SAVE',
                                    lambda: shutil.copyfile(source, Path(destination_path)), mutation=True)
        self._check()
        self._owner._directory.cleanup()

    def cancel(self) -> None:
        self._check()
        params: dict[str, Any] = {'guid': self._guid}
        if self._owner._page.browser_context_id is not None:
            params['browserContextId'] = self._owner._page.browser_context_id
        self._owner._page._send('Browser.cancelDownload', params, mutation=True, browser=True)
        self._owner.close()
