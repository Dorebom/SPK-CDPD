"""Explicit test-only Binding authority and an in-memory CDP transport.

The trusted authority is a fixture; production code has no test-origin flag or
weak default. This helper adapts old Page fixtures to fresh identity metadata so
the unchanged feature assertions exercise the actual guarded Session.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
from typing import Any

from chatgpt_web_api.session import ChatGPTSession
from chatgpt_web_api.target_guard import (
    AuthoritySnapshot, BindingGuard, ClockSample, ENDPOINT, EXPECTED_ORIGIN,
    GuardError, PAGE_FILTER, ParsedBinding, derive_browser_epoch,
)

WS_A = "ws://127.0.0.1:9222/devtools/browser/11111111-1111-4111-8111-111111111111"
WS_B = "ws://127.0.0.1:9222/devtools/browser/33333333-3333-4333-8333-333333333333"
BINDING_ID = "22222222-2222-4222-8222-222222222222"
EPOCH = "254095102094427e3ef2c333da294588"
ISSUED = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.utc = ISSUED
        self.monotonic = 1000.0
        self.suspend_epoch = 0
        self.continuity_known = True

    def sample(self):
        return ClockSample(self.utc, self.monotonic, self.suspend_epoch, self.continuity_known)

    def advance(self, seconds, *, wall=None, mono=None):
        self.utc += timedelta(seconds=seconds if wall is None else wall)
        self.monotonic += seconds if mono is None else mono


def binding_record(target_id="SYNTHETIC-TARGET-A", *, epoch=EPOCH, binding_id=BINDING_ID):
    return {
        "schema": "cdpd.chatgpt-target-binding/v1", "binding_id": binding_id,
        "epoch_algorithm": "cdpd.browser-ws-path.sha256-128/v1",
        "target": {"endpoint": ENDPOINT, "target_id": target_id,
                   "expected_origin": EXPECTED_ORIGIN, "expected_browser_id": epoch},
        "issued_at_utc": "2026-10-24T00:00:00Z", "expires_at_utc": "2026-10-24T04:00:00Z",
    }


class FakeAuthority:
    def __init__(self, record, clock=None):
        parsed = ParsedBinding.parse(record)
        self.clock = clock or FakeClock()
        self.current = AuthoritySnapshot(parsed.binding_id, parsed.record_hash, 1)
        self.invalidations = []
        self.snapshot_error = None
        self.on_snapshot = None
        self._operation_token = None
        self._operation_thread = None
        self._operation_depth = 0
        self._claimed_operations = set()

    def snapshot(self):
        if self.on_snapshot:
            self.on_snapshot(self)
        if self.snapshot_error:
            raise self.snapshot_error
        return self.current

    def invalidate(self, reason, *, binding_id=None, generation=None):
        if ((binding_id is not None and binding_id != self.current.binding_id) or
                (generation is not None and generation != self.current.generation)):
            return
        self.invalidations.append(reason)
        self.current = replace(self.current, state="INVALIDATED", revoked=True)

    def set_mode(self, mode, *, binding_id=None, generation=None):
        if ((binding_id is not None and binding_id != self.current.binding_id) or
                (generation is not None and generation != self.current.generation)):
            return
        self.current = replace(self.current, mode=mode)

    def acquire_operation(self, token):
        thread = threading.get_ident()
        if self._operation_depth and (token != self._operation_token or thread != self._operation_thread):
            raise GuardError("OWNER_BUSY")
        self._operation_token, self._operation_thread = token, thread
        self._operation_depth += 1

    def release_operation(self, token):
        if (not self._operation_depth or token != self._operation_token or
                threading.get_ident() != self._operation_thread):
            raise GuardError("OWNER_BUSY")
        self._operation_depth -= 1
        if not self._operation_depth:
            self._operation_token = self._operation_thread = None

    def claim_operation_id(self, method, operation_id):
        if method not in {"open_project_chat", "execute_prompt_and_wait", "download_response_zip"}:
            raise GuardError("BINDING_RECORD_INVALID")
        if type(operation_id) is not str or not operation_id or len(operation_id) > 512:
            raise GuardError("BINDING_RECORD_INVALID")
        if not self._operation_depth or self._operation_thread != threading.get_ident():
            raise GuardError("OWNER_BUSY")
        key = (method, operation_id)
        if key in self._claimed_operations or len(self._claimed_operations) >= 10000:
            raise GuardError("RECONCILIATION_REQUIRED")
        self._claimed_operations.add(key)


class SimplePage:
    """A Page with no real browser or content; only fixed metadata and counters."""
    def __init__(self, target_id="SYNTHETIC-TARGET-A", url="https://chatgpt.com/"):
        self._target_id = target_id
        self.url = url
        self.focused = False
        self.result = {"synthetic": True}

    def target_id(self):
        return self._target_id

    def title(self):
        return "Synthetic Page"

    def evaluate(self, expression, arg=None):
        return self.result

    def goto(self, url, *, timeout_ms):
        self.url = url

    def trusted_click_marked_action(self, selector, *, timeout_ms):
        return {"ok": True, "activationAttempted": True, "trustedClick": True,
                "trustedClickEventCount": 1, "clickEventCount": 1, "locatorCount": 1}

    def upload_files_to_composer(self, file_paths, **kwargs):
        return {"ok": True, "uploaded": len(file_paths)}

    def observe_composer_attachment_transport(self, **kwargs):
        return {"ok": True}

    def download_from_action(self, expression, arg, destination_path, **kwargs):
        return {"ok": True}

    def capture_screenshot(self, destination_path):
        return True


class AuditedPage:
    """Records primitive identity and count, never scripts, data or URL paths."""
    METHODS = {
        "evaluate": "Runtime.evaluate", "goto": "Page.navigate",
        "title": "Page.title", "target_id": "Page.target_id",
        "trusted_click_marked_action": "Input.trustedClick",
        "upload_files_to_composer": "DOM.uploadFiles",
        "observe_composer_attachment_transport": "DOM.observeUploadTransport",
        "download_from_action": "Browser.downloadFromAction",
        "capture_screenshot": "Page.captureScreenshot",
    }

    def __init__(self, raw, transport, target_id):
        self.raw, self.transport, self.id = raw, transport, target_id

    @property
    def url(self):
        return self.raw.url

    def target_id(self):
        # This legacy cached value is a deliberately separate fixture input.
        value = self.raw.target_id()
        return value

    def __getattr__(self, name):
        raw = getattr(self.raw, name)
        if name not in self.METHODS or not callable(raw):
            return raw
        def invoke(*args, **kwargs):
            method = self.METHODS[name]
            row = {"method": method, "target_id": self.id, "scope": "page_primitive"}
            self.transport.audit.append(row)
            if self.transport.before_primitive:
                self.transport.before_primitive(method, self)
            try:
                return raw(*args, **kwargs)
            finally:
                if self.transport.after_primitive:
                    self.transport.after_primitive(method, self)
        return invoke


class FakeCDPTransport:
    """No sockets. Browser and Page metadata share one simulated connection."""
    def __init__(self, pages=None):
        raw_pages = [SimplePage()] if pages is None else list(pages)
        self._pages = []
        self.audit = []
        self.connections = []
        self.connected = False
        self.closed = False
        self.ws = WS_A
        self.discovery_values = []
        self.connect_error = None
        self.send_error = None
        self.discover_error = None
        self.target_infos = None
        self.root_info = None
        self.page_info = None
        self.frame_tree = None
        self.before_send = None
        self.after_send = None
        self.before_primitive = None
        self.after_primitive = None
        self.event_handler = None
        self.dispatcher = None
        self.extra_targets = []
        self.loader_id = "SYNTHETIC-LOADER-A"
        self.frame_id = "SYNTHETIC-FRAME-A"
        for index, raw in enumerate(raw_pages):
            target = raw.target_id() if callable(getattr(raw, "target_id", None)) else None
            if not target:
                target = "SYNTHETIC-TARGET-" + chr(65 + index)
                # Legacy fixtures omitted identity metadata. The test fixture
                # now supplies it explicitly; production code does not infer it.
                raw._target_id = target
            self._pages.append(AuditedPage(raw, self, target))

    def add_page(self, raw):
        target = raw.target_id()
        page = AuditedPage(raw, self, target)
        self._pages.append(page)
        return page

    def discover(self, endpoint):
        self.audit.append({"method": "GET.json.version", "scope": "discovery", "endpoint_allowed": endpoint == ENDPOINT})
        if self.discover_error:
            raise self.discover_error
        if self.discovery_values:
            return self.discovery_values.pop(0)
        return self.ws

    def connect(self, ws_url):
        self.connections.append(ws_url)
        self.audit.append({"method": "CONNECT.browser", "scope": "transport",
                           "epoch": derive_browser_epoch(ws_url)})
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    def set_event_handler(self, callback):
        self.event_handler = callback

    def set_dispatcher(self, callback):
        self.dispatcher = callback

    def initialize_page(self, page):
        assert page in self._pages
        self.audit.append({"method": "INITIALIZE.exact_page_session", "scope": "page_metadata", "target_id": page.id})

    def emit(self, method, params):
        self.audit.append({"method": method, "scope": "event",
                           "target_id": params.get("targetId", (params.get("targetInfo") or {}).get("targetId"))})
        if self.event_handler:
            self.event_handler(method, params)

    def info(self, page):
        return {"targetId": page.id, "type": "page", "url": page.url,
                "attached": True, "canAccessOpener": False}

    def send(self, method, params, *, page=None):
        target = page.id if page is not None else params.get("targetId")
        self.audit.append({"method": method, "scope": "page_metadata" if page is not None else "browser_metadata",
                           "target_id": target})
        if not self.connected:
            raise GuardError("CDP_COMMAND_FAILED")
        if self.before_send:
            self.before_send(method, params, page)
        if self.send_error:
            raise self.send_error
        if method == "Target.getTargets":
            assert params == {"filter": PAGE_FILTER}, "Explicit complete page filter required"
            targets = self.target_infos if self.target_infos is not None else [self.info(p) for p in self._pages]
            result = {"targetInfos": [dict(i) for i in targets] + [dict(i) for i in self.extra_targets]}
        elif method == "Target.getTargetInfo":
            if page is not None:
                assert params == {}, "Page TargetInfo must resolve its own exact attached session"
                result = {"targetInfo": dict(self.page_info) if self.page_info is not None else self.info(page)}
            else:
                matches = [p for p in self._pages if p.id == params.get("targetId")]
                if self.root_info is not None:
                    result = {"targetInfo": dict(self.root_info)}
                elif len(matches) == 1:
                    result = {"targetInfo": self.info(matches[0])}
                else:
                    raise GuardError("TARGET_MISSING")
        elif method == "Page.getFrameTree":
            assert page in self._pages
            result = self.frame_tree if self.frame_tree is not None else {
                "frameTree": {"frame": {"id": self.frame_id, "loaderId": self.loader_id,
                                        "url": page.url, "securityOrigin": EXPECTED_ORIGIN}}}
        elif method in {"Target.setDiscoverTargets", "Page.enable"}:
            result = {}
        else:
            raise AssertionError("UNEXPECTED_CDP_METADATA_COMMAND:" + method)
        if self.after_send:
            self.after_send(method, params, page)
        return result

    def pages(self):
        return tuple(self._pages)

    def is_connected(self):
        return self.connected

    def close(self):
        self.closed = True
        self.connected = False
        self.audit.append({"method": "CLOSE.owned_transport", "scope": "transport"})

    @property
    def primitive_commands(self):
        return [row for row in self.audit if row["scope"] == "page_primitive"]


def make_bound_session(*, transport=None, page=None, cdp_url=ENDPOINT):
    """Explicit synthetic admission of one record; return the real Session.

    Empty legacy page fixtures produce an UNBOUND Session for rejection cases.
    All valid fixtures obtain a fixed record and a live fake current authority.
    """
    if transport is None:
        transport = FakeCDPTransport([page or SimplePage()])
    elif not isinstance(transport, FakeCDPTransport):
        original = transport
        transport = FakeCDPTransport(original._pages)
        transport.connect_error = getattr(original, "connect_error", None)
    if not transport._pages:
        return ChatGPTSession(cdp_url=cdp_url, transport=transport)
    record = binding_record(transport._pages[0].id)
    clock = FakeClock()
    authority = FakeAuthority(record, clock)
    guard = BindingGuard(record, authority, clock=clock)
    session = ChatGPTSession(cdp_url=cdp_url, transport=transport, binding_guard=guard)
    session.fixture_authority = authority
    session.fixture_clock = clock
    session.fixture_transport = transport
    return session


def isolated_page_ops(context, page):
    """Explicit callback for isolated old Page primitive fixtures only.

    These primitive tests do not claim Session identity/lease coverage. The
    guarded consumer tests construct the real Session and FakeCDPTransport.
    """
    from chatgpt_web_api.session import _PlaywrightPageOps
    def dispatch(name, callback, *, mutation, navigation=False):
        return callback()
    return _PlaywrightPageOps(context, page, dispatch=dispatch)
