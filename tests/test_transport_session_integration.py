"""Real L04 transport/session/CDP Page composition over fake Playwright channels.

Only socket/HTTP/Playwright objects are fakes. Production _PlaywrightTransport,
_PageAssociation, ChatGPTSession, GuardedPageOps and CdpPage execute together.
No real sync_playwright startup, Chrome, HTTP or CDP connection is permitted.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from chatgpt_web_api.session import ChatGPTSession, _PlaywrightTransport
from chatgpt_web_api.target_guard import BindingGuard, ENDPOINT, EXPECTED_ORIGIN, GuardError, PAGE_FILTER
from strict_fakes import FakeAuthority, FakeClock, binding_record, WS_A, WS_B

TARGET = "SYNTHETIC-TARGET-A"
SOCKET = "SYNTHETIC-BROWSER-SOCKET-A"
TARGET_SESSION = "SYNTHETIC-PERSISTENT-TARGET-SESSION-A"
BROWSER_SESSION = "SYNTHETIC-BROWSER-SESSION-A"


class FakeRawPage:
    def __init__(self, wire):
        self.wire = wire
        self.main_frame = SimpleNamespace()

    @property
    def url(self):
        return self.wire.url

    @property
    def main_frame(self):
        return SimpleNamespace(url=self.wire.url)

    @main_frame.setter
    def main_frame(self, _value):
        pass

    def __getattr__(self, name):
        if name in {"evaluate", "goto", "locator", "click", "screenshot", "expect_file_chooser", "expect_download"}:
            def forbidden(*_args, **_kwargs):
                self.wire.raw_dispatches.append(name)
                raise AssertionError("RAW_PLAYWRIGHT_PAGE_PRIMITIVE_FORBIDDEN")
            return forbidden
        raise AttributeError(name)


class Channel:
    def __init__(self, wire, session_id):
        self.wire, self.session_id = wire, session_id
        self.socket_id = SOCKET
        self.listeners = {}
        self.parameters = []

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        if callback in self.listeners.get(event, []):
            self.listeners[event].remove(callback)

    def emit(self, event, value=None):
        for callback in tuple(self.listeners.get(event, [])):
            callback(value or {})

    def detach(self):
        self.wire.detaches.append(self.session_id)

    def send(self, method, params):
        self.parameters.append((method, dict(params)))
        target = self.wire.target_id if self is self.wire.page_channel else params.get("targetId")
        self.wire.audit.append({"method": method, "socket_id": self.socket_id,
                               "session_id": self.session_id, "target_id": target,
                               "binding_id": self.wire.record["binding_id"],
                               "epoch": self.wire.record["target"]["expected_browser_id"],
                               "dispatch_count": 1})
        if self.wire.on_send:
            self.wire.on_send(self, method, params)
        if method in {"Target.setDiscoverTargets", "Target.getTargets"}:
            if self.wire.filter_error:
                raise RuntimeError("SYNTHETIC_FILTER_UNSUPPORTED")
            assert params.get("filter") == PAGE_FILTER
            if method == "Target.getTargets":
                infos = self.wire.target_infos if self.wire.target_infos is not None else [self.wire.info()]
                return {"targetInfos": [dict(info) for info in infos]}
            return {}
        if method == "Target.getTargetInfo":
            if self is self.wire.page_channel:
                assert params == {}
            else:
                assert params == {"targetId": TARGET}
            return {"targetInfo": self.wire.info()}
        if method == "Page.getFrameTree":
            assert self is self.wire.page_channel
            return {"frameTree": {"frame": self.wire.frame()}}
        if method == "Runtime.enable":
            self.wire.context_created()
            return {}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            assert params["uniqueContextId"] == self.wire.context_id
            assert "location.origin" in expression and params["allowUnsafeEvalBlockedByCSP"] is False
            if "document.querySelectorAll(selector).length" in expression:
                value = 1
            elif "SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_ARM_V1" in expression:
                value = {"ok": True}
            elif "SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_COLLECT_V1" in expression:
                value = {"ok": True, "trustedClickEventCount": self.wire.mouse_releases,
                         "clickEventCount": self.wire.mouse_releases, "eventTrace": []}
            elif "element.getBoundingClientRect" in expression:
                value = {"ok": True, "x": 12, "y": 20}
            elif "document.title" in expression:
                value = "Synthetic title"
            else:
                value = {"synthetic_result": 1}
            return {"result": {"type": "object", "value": value}}
        if method == "Input.dispatchMouseEvent":
            if params["type"] == "mouseReleased":
                self.wire.mouse_releases += 1
                if self.wire.emit_chooser:
                    self.emit("Page.fileChooserOpened", {"frameId": self.wire.frame_id,
                                                          "backendNodeId": 7, "mode": "selectMultiple"})
            return {}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 7, "localName": "input",
                             "attributes": ["type", "file", "accept", ".zip"]}}
        if method == "DOM.setFileInputFiles":
            assert params["backendNodeId"] == 7
            return {}
        if method == "Page.navigate":
            assert params["frameId"] == self.wire.frame_id
            self.emit("Page.frameStartedLoading", {"frameId": self.wire.frame_id})
            self.emit("Runtime.executionContextsCleared", {})
            self.wire.url = params["url"]
            self.wire.loader_id = "SYNTHETIC-LOADER-B"
            self.wire.context_id = "SYNTHETIC-CONTEXT-B"
            self.emit("Page.frameNavigated", {"frame": self.wire.frame()})
            self.wire.context_created()
            self.emit("Page.lifecycleEvent", {"frameId": self.wire.frame_id,
                                              "loaderId": self.wire.loader_id, "name": "DOMContentLoaded"})
            if self.wire.emit_stopped:
                self.emit("Page.frameStoppedLoading", {"frameId": self.wire.frame_id})
            return {"frameId": self.wire.frame_id, "loaderId": self.wire.loader_id}
        if method in {"Page.enable", "Page.setLifecycleEventsEnabled", "Page.setInterceptFileChooserDialog"}:
            return {}
        raise AssertionError("UNEXPECTED_SYNTHETIC_COMMAND:" + method)


class Wire:
    def __init__(self, monkeypatch, *, endpoint=ENDPOINT):
        self.record = binding_record(TARGET)
        self.clock = FakeClock()
        self.authority = FakeAuthority(self.record, self.clock)
        self.guard = BindingGuard(self.record, self.authority, clock=self.clock)
        self.url = "https://chatgpt.com/"
        self.target_id = TARGET
        self.frame_id = "SYNTHETIC-FRAME-A"
        self.loader_id = "SYNTHETIC-LOADER-A"
        self.context_id = "SYNTHETIC-CONTEXT-A"
        self.frame_origin = EXPECTED_ORIGIN
        self.target_infos = None
        self.raw_dispatches = []
        self.audit = []
        self.connect_arguments = []
        self.http_requests = []
        self.http_ws = []
        self.http_status = 200
        self.detaches = []
        self.attachments = []
        self.browser_channel_creations = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.browser_close_calls = 0
        self.connected = True
        self.on_send = None
        self.filter_error = False
        self.emit_chooser = False
        self.emit_stopped = True
        self.mouse_releases = 0
        self.browser_channel = Channel(self, BROWSER_SESSION)
        self.page_channel = Channel(self, TARGET_SESSION)
        self.raw_page = FakeRawPage(self)
        self.context = SimpleNamespace(pages=[self.raw_page], new_cdp_session=self.attach)
        self.browser = SimpleNamespace(contexts=[self.context], on=self.browser_on,
                                       new_browser_cdp_session=self.new_browser_channel,
                                       is_connected=lambda: self.connected, close=self.forbidden_browser_close)
        self.browser_listeners = {}
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=self.connect), stop=self.stop)
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: SimpleNamespace(start=self.start)
        sync_api.TimeoutError = TimeoutError
        playwright_module = ModuleType("playwright")
        playwright_module.__path__ = []
        playwright_module.sync_api = sync_api
        monkeypatch.setitem(sys.modules, "playwright", playwright_module)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
        wire = self

        class FakeHTTP:
            def __init__(self, host, port, *, timeout):
                assert (host, port, timeout) == ("127.0.0.1", 9222, 5.0)
                self.sock = None

            def request(self, method, path, headers):
                assert (method, path) == ("GET", "/json/version")
                wire.http_requests.append({"method": "GET.json.version", "endpoint_allowed": True})

            def getresponse(self):
                ws = wire.http_ws.pop(0) if wire.http_ws else WS_A
                payload = json.dumps({"webSocketDebuggerUrl": ws}).encode()
                return SimpleNamespace(status=wire.http_status, read=lambda n: payload[:n])

            def close(self):
                pass

        monkeypatch.setattr("chatgpt_web_api.session.http.client.HTTPConnection", FakeHTTP)
        self.transport = _PlaywrightTransport()
        self.session = ChatGPTSession(endpoint, transport=self.transport, binding_guard=self.guard)

    def start(self):
        self.start_calls += 1
        return self.playwright

    def stop(self):
        self.stop_calls += 1

    def connect(self, endpoint, **kwargs):
        self.connect_arguments.append(endpoint)
        assert endpoint == WS_A and kwargs == {"timeout": 10000}
        return self.browser

    def browser_on(self, event, callback):
        self.browser_listeners.setdefault(event, []).append(callback)

    def browser_emit(self, event):
        for callback in self.browser_listeners.get(event, []):
            callback()

    def forbidden_browser_close(self):
        self.browser_close_calls += 1
        raise AssertionError("BROWSER_CLOSE_FORBIDDEN")

    def new_browser_channel(self):
        self.browser_channel_creations += 1
        return self.browser_channel

    def attach(self, page):
        assert page is self.raw_page
        self.attachments.append(page)
        return self.page_channel

    def info(self):
        return {"targetId": self.target_id, "type": "page", "url": self.url,
                "attached": True, "browserContextId": "SYNTHETIC-BROWSER-CONTEXT-A"}

    def frame(self):
        return {"id": self.frame_id, "loaderId": self.loader_id, "url": self.url,
                "securityOrigin": self.frame_origin}

    def context_created(self):
        self.page_channel.emit("Runtime.executionContextCreated", {"context": {
            "id": 7, "uniqueId": self.context_id, "origin": EXPECTED_ORIGIN,
            "auxData": {"frameId": self.frame_id, "isDefault": True}}})

    def bind(self):
        result = self.session.bind_chatgpt_page()
        assert result.ok, result.reason
        return self.session._bound_page().ops

    @property
    def content(self):
        return [row for row in self.audit if row["method"].startswith(("Runtime.evaluate", "Input.", "DOM.", "Page.navigate"))]


def emit(evidence_case, wire, expected, actual, *, tgt=None):
    evidence_case(expected=expected, actual=actual, commands=wire.audit,
                  ac=["AC01", "AC02", "AC04"],
                  tgt=tgt or ["TGT-01", "TGT-19", "TGT-24"], bind=["BIND-01", "BIND-02"])


def test_real_transport_composition_routes_identity_and_evaluate_to_same_session(monkeypatch, evidence_case):
    """Direct approved WS connection and exact persistent Target session for metadata and evaluation."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    result = bound.evaluate("() => ({synthetic_result: 1})")
    assert result == {"synthetic_result": 1}
    assert wire.connect_arguments == [WS_A] and wire.start_calls == 1
    assert len(wire.attachments) == wire.browser_channel_creations == 1
    assert all(row["socket_id"] == SOCKET for row in wire.audit)
    target_methods = {row["method"] for row in wire.audit if row["session_id"] == TARGET_SESSION}
    assert {"Target.getTargetInfo", "Page.getFrameTree", "Runtime.evaluate"} <= target_methods
    assert all(row["session_id"] == TARGET_SESSION for row in wire.content)
    assert wire.raw_dispatches == []
    emit(evidence_case, wire, {"connect": 1, "target_attach": 1, "raw_page_dispatch": 0, "wrong_session_content": 0},
         {"connect": len(wire.connect_arguments), "target_attach": len(wire.attachments),
          "raw_page_dispatch": len(wire.raw_dispatches), "wrong_session_content": sum(r["session_id"] != TARGET_SESSION for r in wire.content)})


def test_nested_trusted_click_uses_one_input_press_release_without_raw_locator(monkeypatch, evidence_case):
    """Full trusted-click PageOps composes nested guards on one persistent target channel."""
    wire = Wire(monkeypatch)
    result = wire.bind().trusted_click_marked_action("[data-synthetic]", timeout_ms=100)
    kinds = [params["type"] for method, params in wire.page_channel.parameters if method == "Input.dispatchMouseEvent"]
    assert result["ok"] is True and kinds == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert wire.raw_dispatches == [] and all(row["session_id"] == TARGET_SESSION for row in wire.content)
    emit(evidence_case, wire, {"press": 1, "release": 1, "raw_page_dispatch": 0},
         {"press": kinds.count("mousePressed"), "release": kinds.count("mouseReleased"), "raw_page_dispatch": len(wire.raw_dispatches)}, tgt=["TGT-27"])


def test_chooser_dom_assignment_on_identity_verified_persistent_session(monkeypatch, tmp_path, evidence_case):
    """A chooser produced on the pinned frame assigns files once through the same Target channel."""
    wire = Wire(monkeypatch)
    wire.bind()
    proxy = wire.session._raw_ops.proxy
    wire.emit_chooser = True
    path = tmp_path / "synthetic.zip"
    path.write_bytes(b"synthetic file bytes")
    with proxy.expect_file_chooser(timeout=100) as chooser:
        proxy.locator("[data-synthetic-upload]").click()
    assert chooser.value.get_attribute("accept") == ".zip"
    chooser.value.set_files([str(path)])
    assignments = [row for row in wire.audit if row["method"] == "DOM.setFileInputFiles"]
    assert len(assignments) == 1 and assignments[0]["session_id"] == TARGET_SESSION
    assert wire.raw_dispatches == []
    emit(evidence_case, wire, {"DOM.setFileInputFiles": 1, "wrong_session": 0, "raw_page_dispatch": 0},
         {"DOM.setFileInputFiles": len(assignments), "wrong_session": sum(r["session_id"] != TARGET_SESSION for r in assignments),
          "raw_page_dispatch": len(wire.raw_dispatches)})


@pytest.mark.parametrize("emit_stopped", [True, False])
def test_same_target_navigation_settles_without_rebinding_or_raw_goto(monkeypatch, evidence_case, emit_stopped):
    """Navigation completion revalidates same Target/record and dispatches one Page.navigate."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    original = wire.guard.record.to_bytes()
    wire.emit_stopped = emit_stopped
    bound.goto("https://chatgpt.com/c/synthetic", timeout_ms=100)
    assert wire.session.bind_chatgpt_page().ok
    navigations = [row for row in wire.content if row["method"] == "Page.navigate"]
    assert len(navigations) == 1 and wire.guard.record.to_bytes() == original
    assert wire.guard.state == "BOUND" and wire.raw_dispatches == []
    emit(evidence_case, wire, {"Page.navigate": 1, "new_binding": 0, "raw_page_dispatch": 0},
         {"Page.navigate": len(navigations), "new_binding": int(wire.guard.record.to_bytes() != original),
          "raw_page_dispatch": len(wire.raw_dispatches)}, tgt=["TGT-09", "TGT-12"])


@pytest.mark.parametrize("primitive", ["evaluate", "click", "goto", "upload", "download", "screenshot"])
def test_cached_bound_page_refuses_every_primitive_after_invalidation(monkeypatch, tmp_path, evidence_case, primitive):
    """Previously returned BoundPage cannot dispatch metadata or primitives after terminal invalidation."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    before = len(wire.audit)
    wire.guard.invalidate("BINDING_REVOKED")
    with pytest.raises(GuardError):
        if primitive == "evaluate": bound.evaluate("() => 1")
        elif primitive == "click": bound.trusted_click_marked_action("[data-synthetic]", timeout_ms=100)
        elif primitive == "goto": bound.goto("https://chatgpt.com/c/synthetic", timeout_ms=100)
        elif primitive == "upload": bound.upload_files_to_composer([], attachment_action_script="() => 1", upload_menu_action_script="() => 1", timeout_ms=100)
        elif primitive == "download": bound.download_from_action("() => 1", None, str(tmp_path / "never.zip"), expected_filename="never.zip", timeout_ms=100)
        else: bound.capture_screenshot(str(tmp_path / "never.png"))
    assert len(wire.audit) == before and wire.raw_dispatches == []
    emit(evidence_case, wire, {"new_dispatch": 0, "raw_page_dispatch": 0},
         {"new_dispatch": len(wire.audit)-before, "raw_page_dispatch": len(wire.raw_dispatches)}, tgt=["TGT-10", "TGT-14"])


def test_known_cdp_disconnect_latches_and_never_reconnects(monkeypatch, evidence_case):
    """Known established browser disconnect poisons cached reference and connection reuse."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    before = len(wire.audit)
    wire.connected = False
    wire.browser_emit("disconnected")
    with pytest.raises(GuardError): bound.evaluate("() => 1")
    assert not wire.session.bind_chatgpt_page().ok
    assert len(wire.audit) == before and len(wire.connect_arguments) == 1
    emit(evidence_case, wire, {"new_dispatch": 0, "reconnect": 0},
         {"new_dispatch": len(wire.audit)-before, "reconnect": len(wire.connect_arguments)-1}, tgt=["TGT-14", "TGT-36"])


@pytest.mark.parametrize("change", ["target_id", "frame_origin", "frame_id", "replacement_page", "duplicate_id"])
def test_fresh_metadata_and_page_correspondence_refuse_changed_identity(monkeypatch, evidence_case, change):
    """A changed identity/frame/Page association cannot reach Runtime.evaluate or another target."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    before = len(wire.content)
    if change == "target_id": wire.target_id = "SYNTHETIC-TARGET-B"
    elif change == "frame_origin": wire.frame_origin = "https://example.invalid"
    elif change == "frame_id": wire.frame_id = "SYNTHETIC-FRAME-B"
    elif change == "replacement_page": wire.context.pages = [FakeRawPage(wire)]
    else: wire.target_infos = [wire.info(), wire.info()]
    with pytest.raises(GuardError): bound.evaluate("() => 1")
    assert len(wire.content) == before and len(wire.attachments) == 1 and wire.raw_dispatches == []
    emit(evidence_case, wire, {"new_content": 0, "new_target_attach": 0, "raw_page_dispatch": 0},
         {"new_content": len(wire.content)-before, "new_target_attach": len(wire.attachments)-1,
          "raw_page_dispatch": len(wire.raw_dispatches)}, tgt=["TGT-04", "TGT-06", "TGT-21"])


def test_epoch_change_after_direct_connect_never_discovers_or_connects_alternative(monkeypatch, evidence_case):
    """Postconnect discovery mismatch denies before target work; WS B is never connected."""
    wire = Wire(monkeypatch)
    wire.http_ws = [WS_A, WS_B]
    result = wire.session.bind_chatgpt_page()
    assert not result.ok and result.reason == "BROWSER_IDENTITY_MISMATCH"
    assert wire.connect_arguments == [WS_A] and not wire.audit and wire.raw_dispatches == []
    emit(evidence_case, wire, {"reason": "BROWSER_IDENTITY_MISMATCH", "alternative_connect": 0, "target_command": 0},
         {"reason": result.reason, "alternative_connect": int(WS_B in wire.connect_arguments), "target_command": len(wire.audit)}, tgt=["TGT-05", "TGT-19"])


def test_endpoint_rejected_before_http_or_playwright_start(monkeypatch, evidence_case):
    """Unapproved endpoint is refused before any HTTP connection or Playwright start."""
    wire = Wire(monkeypatch, endpoint="http://localhost:9222")
    result = wire.session.bind_chatgpt_page()
    assert not result.ok and result.reason == "ENDPOINT_NOT_ALLOWED"
    assert not wire.http_requests and wire.start_calls == 0
    emit(evidence_case, wire, {"reason": "ENDPOINT_NOT_ALLOWED", "HTTP": 0, "Playwright_start": 0},
         {"reason": result.reason, "HTTP": len(wire.http_requests), "Playwright_start": wire.start_calls}, tgt=["TGT-19", "TGT-24"])


def test_discovery_redirect_refused_without_following_or_connecting(monkeypatch, evidence_case):
    """Discovery HTTP redirect never becomes a WebSocket connection or follow-up request."""
    wire = Wire(monkeypatch)
    wire.http_status = 302
    result = wire.session.bind_chatgpt_page()
    assert not result.ok and len(wire.http_requests) == 1 and not wire.connect_arguments
    emit(evidence_case, wire, {"HTTP": 1, "connect": 0},
         {"HTTP": len(wire.http_requests), "connect": len(wire.connect_arguments)}, tgt=["TGT-19"])


def test_unsupported_explicit_page_filter_has_no_weak_fallback(monkeypatch, evidence_case):
    """Unsupported filter fails closed; no unfiltered Target command or page primitive follows."""
    wire = Wire(monkeypatch)
    wire.filter_error = True
    result = wire.session.bind_chatgpt_page()
    assert not result.ok and result.reason == "PROTOCOL_UNSUPPORTED"
    filters = [params for method, params in wire.browser_channel.parameters if method in {"Target.setDiscoverTargets", "Target.getTargets"}]
    assert filters and all(params.get("filter") == PAGE_FILTER for params in filters)
    assert not wire.content and not wire.attachments
    emit(evidence_case, wire, {"reason": "PROTOCOL_UNSUPPORTED", "weak_fallback": 0, "content": 0},
         {"reason": result.reason, "weak_fallback": sum(params.get("filter") != PAGE_FILTER for params in filters), "content": len(wire.content)}, tgt=["TGT-21", "TGT-24"])


def test_owned_session_cleanup_never_closes_browser(monkeypatch, evidence_case):
    """Close detaches owned browser/target channels without Browser.close or UI operations."""
    wire = Wire(monkeypatch)
    wire.bind()
    wire.session.close()
    assert set(wire.detaches) == {TARGET_SESSION, BROWSER_SESSION}
    assert wire.stop_calls == 1 and wire.browser_close_calls == 0
    emit(evidence_case, wire, {"owned_detaches": 2, "Browser.close": 0},
         {"owned_detaches": len(wire.detaches), "Browser.close": wire.browser_close_calls}, tgt=["TGT-36"])


def test_actual_target_event_listener_latches_page_addition_after_it_disappears(monkeypatch, evidence_case):
    """Real transport event callbacks retain a page-set violation after the page disappears."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    before = len(wire.audit)
    extra = {"targetId": "SYNTHETIC-TARGET-B", "type": "page", "url": "https://chatgpt.com/"}
    wire.browser_channel.emit("Target.targetCreated", {"targetInfo": extra})
    wire.browser_channel.emit("Target.targetDestroyed", {"targetId": extra["targetId"]})
    with pytest.raises(GuardError) as error: bound.evaluate("() => 1")
    assert error.value.code == "SINGLE_PAGE_VIOLATION" and len(wire.audit) == before
    assert not wire.session.bind_chatgpt_page().ok
    emit(evidence_case, wire, {"reason": "SINGLE_PAGE_VIOLATION", "new_dispatch": 0, "rebind": 0},
         {"reason": error.value.code, "new_dispatch": len(wire.audit)-before, "rebind": 0}, tgt=["TGT-21", "TGT-30"])


def test_context_lost_after_runtime_dispatch_preserves_once_only_uncertainty(monkeypatch, evidence_case):
    """Context loss after actual evaluation dispatch cannot trigger a second evaluation."""
    wire = Wire(monkeypatch)
    bound = wire.bind()

    def lose(channel, method, params):
        if method == "Runtime.evaluate":
            channel.emit("Runtime.executionContextsCleared", {})

    wire.on_send = lose
    with pytest.raises(GuardError) as error: bound.evaluate("() => 1")
    count = sum(row["method"] == "Runtime.evaluate" for row in wire.content)
    assert count == 1 and wire.guard.mode == "RECONCILIATION_REQUIRED"
    before = len(wire.audit)
    with pytest.raises(GuardError): bound.evaluate("() => 1")
    assert len(wire.audit) == before
    emit(evidence_case, wire, {"Runtime.evaluate": 1, "retry": 0, "mode": "RECONCILIATION_REQUIRED"},
         {"Runtime.evaluate": count, "retry": 0, "mode": wire.guard.mode}, tgt=["TGT-27"])


def test_spa_url_change_on_mouse_release_keeps_same_binding_and_single_activation(monkeypatch, evidence_case):
    """A settled same-document route change during activation succeeds on the same Binding."""
    wire = Wire(monkeypatch)
    bound = wire.bind()
    original = wire.guard.record.to_bytes()
    changes = []

    def navigate(channel, method, params):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            changes.append(True)
            wire.url = "https://chatgpt.com/c/SYNTHETIC-CONVERSATION-A"
            channel.emit("Page.navigatedWithinDocument", {"frameId": wire.frame_id, "url": wire.url})

    wire.on_send = navigate
    result = bound.trusted_click_marked_action('[data-shirabe-send-action="SYNTHETIC-SEND"]', timeout_ms=100)
    kinds = [params["type"] for method, params in wire.page_channel.parameters if method == "Input.dispatchMouseEvent"]
    assert len(changes) == 1 and result["ok"] is True and result["activationAttempted"] is True
    assert result["trustedClick"] is True and wire.guard.state == "BOUND" and wire.guard.mode == "NORMAL"
    assert wire.guard.record.to_bytes() == original and wire.raw_dispatches == []
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]
    expected = {"operation_succeeded": True, "activation_attempted": True, "trusted_click": True,
                "press": 1, "release": 1, "replay": 0, "new_binding": 0, "raw_page_dispatch": 0,
                "state": "BOUND", "mode": "NORMAL"}
    actual = {"operation_succeeded": result["ok"], "activation_attempted": result["activationAttempted"],
              "trusted_click": result["trustedClick"], "press": kinds.count("mousePressed"),
              "release": kinds.count("mouseReleased"), "replay": max(0, kinds.count("mouseReleased") - 1),
              "new_binding": int(wire.guard.record.to_bytes() != original),
              "raw_page_dispatch": len(wire.raw_dispatches), "state": wire.guard.state, "mode": wire.guard.mode}
    emit(evidence_case, wire, expected, actual, tgt=["TGT-12", "TGT-27"])


def test_spa_url_change_during_click_diagnostic_keeps_activation_without_success_claim(monkeypatch, evidence_case):
    """Post-activation diagnostic URL race preserves attempted activation and never clicks again.

    This case ends at the trusted-click primitive boundary. Its failure result
    is classified by composer.submit_prompt as uncertain submission; the
    primitive itself does not claim a completed Facade submission.
    """
    wire = Wire(monkeypatch)
    bound = wire.bind()
    changes = []

    def navigate(channel, method, params):
        if method == "Runtime.evaluate" and "SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_COLLECT_V1" in params.get("expression", ""):
            changes.append(True)
            wire.url = "https://chatgpt.com/c/SYNTHETIC-CONVERSATION-A"
            channel.emit("Page.navigatedWithinDocument", {"frameId": wire.frame_id, "url": wire.url})

    wire.on_send = navigate
    result = bound.trusted_click_marked_action('[data-shirabe-send-action="SYNTHETIC-SEND"]', timeout_ms=100)
    kinds = [params["type"] for method, params in wire.page_channel.parameters if method == "Input.dispatchMouseEvent"]
    collects = sum(method == "Runtime.evaluate" and "SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_COLLECT_V1" in params.get("expression", "")
                   for method, params in wire.page_channel.parameters)
    assert len(changes) == 1 and result["ok"] is False and result["activationAttempted"] is True
    assert result["trustedClick"] is False and result["reason"] == "trusted-click-event-not-observed"
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"] and collects == 1 and wire.raw_dispatches == []
    expected = {"result_boundary": "TRUSTED_CLICK_PRIMITIVE", "operation_succeeded": False,
                "activation_attempted": True, "trusted_click": False, "press": 1, "release": 1,
                "diagnostic_collect": 1, "replay": 0, "raw_page_dispatch": 0}
    actual = {"result_boundary": "TRUSTED_CLICK_PRIMITIVE", "operation_succeeded": result["ok"],
              "activation_attempted": result["activationAttempted"], "trusted_click": result["trustedClick"],
              "press": kinds.count("mousePressed"), "release": kinds.count("mouseReleased"),
              "diagnostic_collect": collects, "replay": max(0, kinds.count("mouseReleased") - 1),
              "raw_page_dispatch": len(wire.raw_dispatches)}
    emit(evidence_case, wire, expected, actual, tgt=["TGT-12", "TGT-27"])


def test_spa_url_change_during_postsend_observation_invalidates_without_resending(monkeypatch, evidence_case):
    """An observation URL race after one activation is terminal with reconciliation and no resend."""
    from chatgpt_web_api.messages import _MESSAGE_OBSERVE_SCRIPT

    wire = Wire(monkeypatch)
    bound = wire.bind()
    result = bound.trusted_click_marked_action('[data-shirabe-send-action="SYNTHETIC-SEND"]', timeout_ms=100)
    assert result["ok"] is True and wire.mouse_releases == 1
    before_evaluations = sum(row["method"] == "Runtime.evaluate" for row in wire.content)
    changes = []

    def navigate(channel, method, params):
        if method == "Runtime.evaluate":
            changes.append(True)
            wire.url = "https://chatgpt.com/c/SYNTHETIC-CONVERSATION-A"
            channel.emit("Page.navigatedWithinDocument", {"frameId": wire.frame_id, "url": wire.url})

    wire.on_send = navigate
    with pytest.raises(GuardError) as error:
        bound.evaluate(_MESSAGE_OBSERVE_SCRIPT)
    assert error.value.code == "TARGET_UNVERIFIED" and error.value.dispatched is True
    assert len(changes) == 1 and wire.guard.state == "INVALIDATED" and wire.guard.mode == "RECONCILIATION_REQUIRED"
    assert wire.mouse_releases == 1 and wire.raw_dispatches == []
    before_denied = len(wire.audit)
    with pytest.raises(GuardError):
        bound.evaluate(_MESSAGE_OBSERVE_SCRIPT)
    assert len(wire.audit) == before_denied
    evaluations = sum(row["method"] == "Runtime.evaluate" for row in wire.content) - before_evaluations
    expected = {"reason": "TARGET_UNVERIFIED", "observation_evaluations": 1, "activation_release": 1,
                "replay": 0, "post_invalidation_dispatch": 0, "raw_page_dispatch": 0,
                "state": "INVALIDATED", "mode": "RECONCILIATION_REQUIRED"}
    actual = {"reason": error.value.code, "observation_evaluations": evaluations,
              "activation_release": wire.mouse_releases, "replay": max(0, wire.mouse_releases - 1),
              "post_invalidation_dispatch": len(wire.audit) - before_denied,
              "raw_page_dispatch": len(wire.raw_dispatches), "state": wire.guard.state, "mode": wire.guard.mode}
    emit(evidence_case, wire, expected, actual, tgt=["TGT-12", "TGT-27"])
