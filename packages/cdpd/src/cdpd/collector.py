"""Narrow CDP client. No public arbitrary-CDP or arbitrary-JavaScript endpoint."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .contracts import CaptureRequest, origin

_BROWSER_METHODS = frozenset({"Target.getTargetInfo", "Target.attachToTarget", "Target.detachFromTarget"})
_SESSION_METHODS = frozenset({"Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"})

# The page is never asked for text, input values, names, IDs, URLs or arbitrary attributes.
# The isolated world reduces interference from page monkey-patching. CDP is still privileged.
_STRUCTURE_SCRIPT = r"""(() => {
  const maxNodes = __MAX_NODES__;
  const tags = new Set('html head body div span main section article aside nav header footer form button input textarea select option label a p ul ol li table tbody thead tr th td svg path g img canvas video audio iframe slot h1 h2 h3 h4 h5 h6 br hr dialog details summary fieldset legend'.split(' '));
  const roles = new Set('button textbox checkbox radio switch link menu menuitem list listitem tab tablist tabpanel dialog alert status progressbar combobox option heading navigation main region group presentation none'.split(' '));
  const nodes = []; let truncated = false; let iframeCount = 0; let openShadowCount = 0;
  const stack = document.documentElement ? [[document.documentElement, -1, false]] : [];
  while (stack.length) {
    if (nodes.length >= maxNodes) { truncated = true; break; }
    const [el, parent, shadowBoundary] = stack.pop();
    const name = String(el.localName || '').toLowerCase();
    if (name === 'script' || name === 'style' || name === 'noscript') continue;
    const tag = tags.has(name) ? name : '#other';
    const rawRole = el.getAttribute('role'); const role = roles.has(rawRole) ? rawRole : null;
    const rect = el.getBoundingClientRect(); const style = getComputedStyle(el);
    const index = nodes.length;
    if (tag === 'iframe') iframeCount++;
    nodes.push({index, parent, tag, role, shadowBoundary,
      disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
      hidden: el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true',
      visible: style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && rect.width > 0 && rect.height > 0,
      box: [rect.x, rect.y, rect.width, rect.height].map(x => Number.isFinite(x) ? Math.round(x * 10) / 10 : 0)});
    const children = Array.from(el.children || []);
    for (let i = children.length - 1; i >= 0; i--) stack.push([children[i], index, false]);
    if (el.shadowRoot) {
      openShadowCount++;
      const children = Array.from(el.shadowRoot.children || []);
      for (let i = children.length - 1; i >= 0; i--) stack.push([children[i], index, true]);
    }
  }
  return {schema:'cdpd.structure/v1', nodes, truncated, iframeCount, openShadowCount,
    coverage:{scope:'MAIN_FRAME_AND_OPEN_SHADOW',iframeDocuments:'NOT_CAPTURED',closedShadow:'NOT_OBSERVABLE',accessibilityTree:'NOT_CAPTURED'},
    observation:'STRUCTURE_ONLY_NOT_FUNCTIONAL_VERIFICATION'};
})()"""


class CaptureError(RuntimeError):
    """Only a fixed error code crosses the diagnostics persistence boundary."""
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CapturePolicy:
    """Trusted, local configuration; not derived from page content or client free text."""
    allowed_origins: tuple[str, ...]
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.allowed_origins, tuple):
            raise ValueError("invalid capture policy")
        for item in self.allowed_origins:
            if origin(item) != item:
                raise ValueError("allowed_origins must contain normalized origins")


def _local_endpoint(url: str, *, websocket: bool = False) -> tuple[str, int, str]:
    try:
        p = urlsplit(url)
        if p.scheme != ('ws' if websocket else 'http') or p.hostname not in {'127.0.0.1', '::1'} or p.username or p.password or p.query or p.fragment:
            raise ValueError()
        if not websocket and p.path not in {'', '/'}:
            raise ValueError()
        if websocket and not p.path.startswith('/devtools/browser/'):
            raise ValueError()
        return p.hostname, p.port or 80, p.path
    except ValueError:
        raise CaptureError("ENDPOINT_NOT_ALLOWED") from None


class _Connection:
    def __init__(self, endpoint: str, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self.socket = None
        self.sequence = 0
        self.audit: list[str] = []
        host, port, _ = _local_endpoint(endpoint)
        try:
            # FIX010: transport setup is unnecessary for contract-only Worker
            # imports. Its import time remains inside the original deadline.
            import http.client
            http_conn = http.client.HTTPConnection(host, port, timeout=self.remaining())
            try:
                http_conn.request('GET', '/json/version')
                response = http_conn.getresponse()
                if response.status != 200:
                    raise CaptureError("CDP_DISCOVERY_FAILED")
                data = response.read(65537)
                if len(data) > 65536:
                    raise CaptureError("CDP_DISCOVERY_TOO_LARGE")
                meta = json.loads(data)
            finally:
                http_conn.close()
            ws_url = meta['webSocketDebuggerUrl']
            ws_host, ws_port, ws_path = _local_endpoint(ws_url, websocket=True)
            if (ws_host, ws_port) != (host, port):
                raise CaptureError("ENDPOINT_REDIRECT_REJECTED")
            self.browser_id = hashlib.sha256(ws_path.encode()).hexdigest()[:32]
            import websocket
            self.socket = websocket.create_connection(ws_url, timeout=self.remaining(), suppress_origin=True, http_no_proxy=[host])
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("CDP_CONNECT_FAILED") from None

    def remaining(self) -> float:
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise CaptureError("CAPTURE_DEADLINE")
        return value

    def send(self, method: str, params: dict[str, Any], session: str | None = None) -> dict[str, Any]:
        allowed = _SESSION_METHODS if session else _BROWSER_METHODS
        if method not in allowed:
            raise CaptureError("COMMAND_NOT_ALLOWED")
        if method == 'Runtime.evaluate':
            expression = params.get('expression', '')
            match = re.search(r'const maxNodes = (\d+);', expression)
            if not match or not 1 <= int(match.group(1)) <= 2000 or expression != _STRUCTURE_SCRIPT.replace('__MAX_NODES__', match.group(1)):
                raise CaptureError("SCRIPT_NOT_ALLOWED")
        self.audit.append(method)
        self.sequence += 1
        packet = {'id': self.sequence, 'method': method, 'params': params}
        if session:
            packet['sessionId'] = session
        try:
            self.socket.settimeout(self.remaining())
            self.socket.send(json.dumps(packet))
            while True:
                self.socket.settimeout(self.remaining())
                message = self.socket.recv()
                if len(message) > 2_000_000:
                    raise CaptureError("CDP_RESPONSE_TOO_LARGE")
                result = json.loads(message)
                if result.get('id') != self.sequence:
                    continue  # No raw event persistence; this slice is not a flight recorder.
                if 'error' in result:
                    raise CaptureError("CDP_COMMAND_FAILED")
                return result.get('result', {})
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("CDP_COMMAND_FAILED") from None

    def close(self, session: str | None) -> None:
        try:
            if session and self.socket and self.deadline > time.monotonic():
                self.send('Target.detachFromTarget', {'sessionId': session})
        except Exception:
            pass
        finally:
            if self.socket:
                try:
                    self.socket.close(timeout=0.2)
                except Exception:
                    pass
            # Never Browser.close, Target.closeTarget, Page.navigate, Input.*.


class CDPStructureCollector:
    def __init__(self, policy: CapturePolicy) -> None:
        self.policy = policy

    def collect(self, request: CaptureRequest) -> dict[str, Any]:
        if not self.policy.enabled or request.target.expected_origin not in self.policy.allowed_origins:
            raise CaptureError("POLICY_DENIED")
        connection = _Connection(request.target.endpoint, request.timeout_seconds)
        session = None
        try:
            binding = request.target
            if binding.expected_browser_id is not None and binding.expected_browser_id != connection.browser_id:
                raise CaptureError("BROWSER_IDENTITY_MISMATCH")
            before = connection.send('Target.getTargetInfo', {'targetId': binding.target_id})['targetInfo']
            if before.get('targetId') != binding.target_id or before.get('type') != 'page':
                raise CaptureError("TARGET_BINDING_INVALID")
            if origin(before['url']) != binding.expected_origin:
                raise CaptureError("TARGET_ORIGIN_MISMATCH")
            session = connection.send('Target.attachToTarget', {'targetId': binding.target_id, 'flatten': True})['sessionId']
            frame_before = connection.send('Page.getFrameTree', {}, session)['frameTree']['frame']
            try:
                frame_origin = origin(frame_before['url'])
            except ValueError:
                raise CaptureError('FRAME_ORIGIN_UNVERIFIED') from None
            if frame_origin != binding.expected_origin:
                raise CaptureError("TARGET_ORIGIN_MISMATCH")
            world = connection.send('Page.createIsolatedWorld', {'frameId': frame_before['id'], 'worldName': 'cdpd-structure-v1'}, session)
            expression = _STRUCTURE_SCRIPT.replace('__MAX_NODES__', str(request.max_nodes))
            result = connection.send('Runtime.evaluate', {'expression': expression, 'contextId': world['executionContextId'], 'returnByValue': True,
                                     'awaitPromise': False, 'timeout': max(1, int(connection.remaining()*1000))}, session)
            if result.get('exceptionDetails'):
                raise CaptureError("STRUCTURE_EVALUATION_FAILED")
            structure = result.get('result', {}).get('value')
            if not isinstance(structure, dict) or structure.get('schema') != 'cdpd.structure/v1':
                raise CaptureError("STRUCTURE_INVALID")
            frame_after = connection.send('Page.getFrameTree', {}, session)['frameTree']['frame']
            after = connection.send('Target.getTargetInfo', {'targetId': binding.target_id})['targetInfo']
            if frame_after.get('id') != frame_before.get('id') or frame_after.get('loaderId') != frame_before.get('loaderId') or after.get('url') != before.get('url'):
                raise CaptureError("NAVIGATION_DURING_CAPTURE")
            # No raw URL/title/session ID is placed in evidence.
            structure['binding'] = {'target_id': binding.target_id, 'browser_id': connection.browser_id, 'origin': binding.expected_origin,
                                    'loader_id': frame_before.get('loaderId'), 'binding_strength': 'PINNED_BROWSER' if binding.expected_browser_id else 'CURRENT_CONNECTION_ONLY'}
            structure['command_audit'] = connection.audit.copy()
            return structure
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("CAPTURE_FAILED") from None
        finally:
            connection.close(session)
