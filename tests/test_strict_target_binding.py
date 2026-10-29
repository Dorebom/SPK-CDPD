"""L04 scoped synthetic consumer/guard tests; no L06 Worker or live acceptance."""
from __future__ import annotations

from dataclasses import replace

import pytest

from cdpd import TargetBinding
from chatgpt_web_api.models import FailureKind
from chatgpt_web_api.session import ChatGPTSession
from chatgpt_web_api.target_guard import (
    BindingGuard, ENDPOINT, GuardError, PAGE_FILTER, ParsedBinding, derive_browser_epoch,
    frame_snapshot, same_target, single_page,
)
from strict_fakes import (
    BINDING_ID, EPOCH, FakeAuthority, FakeCDPTransport, FakeClock, SimplePage,
    WS_A, WS_B, binding_record, make_bound_session,
)


def bound_session():
    session = make_bound_session()
    result = session.bind_chatgpt_page()
    assert result.ok
    bound = session._bound_page()
    assert bound is not None
    return session, bound


def trace(evidence_case, session, *, expected, actual, ac=("AC01", "AC02"), tgt=(), bind=("BIND-02",)):
    transport = session.fixture_transport
    evidence_case(expected=expected, actual=actual, commands=transport.audit,
                  ac=ac, tgt=tgt, bind=bind)


def test_explicit_same_binding_is_revalidated_without_issue_or_expiry_extension(evidence_case):
    """TGT-01/09/35: current envelope stays identical and focus cannot retarget."""
    session, bound = bound_session()
    transport = session.fixture_transport
    before = session.fixture_authority.current
    for _ in range(3):
        transport._pages[0].raw.focused = not transport._pages[0].raw.focused
        result = session.bind_chatgpt_page()
        assert result.ok
        assert result.page.target_id == "SYNTHETIC-TARGET-A"
    assert session.fixture_authority.current == before
    assert transport.connections == [WS_A]
    assert transport.primitive_commands == []
    assert bound.ops.evaluate("() => true") == {"synthetic": True}
    commands = transport.primitive_commands
    assert len(commands) == 1 and commands[0]["target_id"] == "SYNTHETIC-TARGET-A"
    trace(evidence_case, session,
          expected={"binding_id": BINDING_ID, "epoch": EPOCH, "target_id": "SYNTHETIC-TARGET-A",
                    "connections": 1, "primitive_dispatch": 1, "retarget": 0},
          actual={"binding_id": before.binding_id, "epoch": EPOCH, "target_id": result.page.target_id,
                  "connections": len(transport.connections), "primitive_dispatch": len(commands), "retarget": 0},
          ac=("AC01", "AC02", "AC04"), tgt=("TGT-01", "TGT-09", "TGT-35"), bind=("BIND-01",))


@pytest.mark.parametrize("operation", ["bind", "evaluate_from_absent_bound", "connect"])
def test_unregistered_session_has_no_discovery_connection_or_dispatch(operation, evidence_case):
    """TGT-08/24: a visible single page is never implicit admission."""
    transport = FakeCDPTransport()
    session = ChatGPTSession(transport=transport)
    if operation == "bind":
        result = session.bind_chatgpt_page()
        assert not result.ok and result.failure == FailureKind.SESSION_BINDING_FAILURE
    elif operation == "connect":
        with pytest.raises(GuardError):
            session.connect()
    else:
        assert session._bound_page() is None
    assert transport.connections == [] and transport.audit == []
    evidence_case(expected={"dispatch": 0, "connections": 0, "auto_bind": 0},
                  actual={"dispatch": 0, "connections": len(transport.connections), "auto_bind": 0},
                  commands=transport.audit, ac=["AC01", "AC02", "AC04"],
                  tgt=["TGT-08", "TGT-24"], bind=["BIND-02"])


@pytest.mark.parametrize("assertion", ["wrong_id", "wrong_url", "contradictory_id_url"])
def test_binding_arguments_are_assertions_and_never_select_another_target(assertion, evidence_case):
    """TGT-20: URL matching never overrides a contradictory pinned ID."""
    session, _ = bound_session()
    options = {"target_id": "SYNTHETIC-TARGET-B"} if assertion == "wrong_id" else {
        "exact_url": "https://chatgpt.com/other"}
    if assertion == "contradictory_id_url":
        options = {"target_id": "SYNTHETIC-TARGET-B", "exact_url": "https://chatgpt.com/"}
    result = session.bind_chatgpt_page(**options)
    assert not result.ok
    assert not session.fixture_transport.primitive_commands
    assert session.fixture_transport.connections == [WS_A]
    trace(evidence_case, session, expected={"accepted": False, "retarget": 0, "primitive_dispatch": 0},
          actual={"accepted": result.ok, "retarget": 0, "primitive_dispatch": 0},
          tgt=("TGT-20",), bind=("BIND-01", "BIND-02"))


@pytest.mark.parametrize("method", ["evaluate", "goto", "click", "upload", "download", "screenshot", "observe_upload"])
def test_cached_page_primitives_all_refuse_after_explicit_revocation(method, evidence_case):
    """TGT-10: every retained PageOps reference denies dispatch after invalidation."""
    session, bound = bound_session()
    authority = session.fixture_authority
    authority.current = replace(authority.current, revoked=True)
    invoke = {
        "evaluate": lambda: bound.ops.evaluate("() => true"),
        "goto": lambda: bound.ops.goto("https://chatgpt.com/next", timeout_ms=1),
        "click": lambda: bound.ops.trusted_click_marked_action("[data-synthetic]", timeout_ms=1),
        "upload": lambda: bound.ops.upload_files_to_composer([], attachment_action_script="", upload_menu_action_script="", timeout_ms=1),
        "download": lambda: bound.ops.download_from_action("", {}, "unused", expected_filename="fixture.zip", timeout_ms=1),
        "screenshot": lambda: bound.ops.capture_screenshot("unused"),
        "observe_upload": lambda: bound.ops.observe_composer_attachment_transport(attachment_action_script="", upload_menu_action_script="", timeout_ms=1),
    }[method]
    with pytest.raises(GuardError):
        invoke()
    assert session.fixture_transport.primitive_commands == []
    trace(evidence_case, session, expected={"primitive": method, "dispatch_after_revocation": 0},
          actual={"primitive": method, "dispatch_after_revocation": len(session.fixture_transport.primitive_commands)},
          tgt=("TGT-10",), bind=("BIND-02",))


@pytest.mark.parametrize("change", ["generation", "binding_id", "record_hash", "superseded", "state_unknown", "authority_unavailable"])
def test_current_generation_and_record_hash_are_checked_on_cached_operations(change, evidence_case):
    """TGT-24/30: old JSON or an inconsistent current registry cannot revive rights."""
    session, bound = bound_session()
    authority = session.fixture_authority
    old = authority.current
    values = {"generation": old.generation + 1, "binding_id": "44444444-4444-4444-8444-444444444444",
              "record_hash": "0" * 64, "superseded": True, "state_unknown": "UNKNOWN"}
    if change == "authority_unavailable":
        authority.snapshot_error = RuntimeError("SYNTHETIC_CURRENT_UNAVAILABLE")
    else:
        authority.current = replace(old, **{"state" if change == "state_unknown" else change: values[change]})
    with pytest.raises(GuardError):
        bound.ops.evaluate("() => true")
    authority.snapshot_error = None
    authority.current = old
    assert not session.bind_chatgpt_page().ok
    with pytest.raises(GuardError):
        bound.ops.evaluate("() => true")
    assert not session.fixture_transport.primitive_commands
    trace(evidence_case, session, expected={"condition": change, "dispatch": 0, "automatic_revival": 0},
          actual={"condition": change, "dispatch": 0, "automatic_revival": 0}, tgt=("TGT-24", "TGT-30"))


def test_target_replacement_same_url_is_never_rebound_and_stays_invalid(evidence_case):
    """TGT-04: replacement B at the same URL receives no content command."""
    session, cached = bound_session()
    transport = session.fixture_transport
    original = list(transport._pages)
    transport._pages.clear()
    transport.add_page(SimplePage("SYNTHETIC-TARGET-B"))
    result = session.bind_chatgpt_page()
    assert not result.ok
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    transport._pages = original
    assert not session.bind_chatgpt_page().ok
    assert transport.primitive_commands == [] and len(transport.connections) == 1
    trace(evidence_case, session, expected={"replacement_target_dispatch": 0, "auto_rebind": 0},
          actual={"replacement_target_dispatch": 0, "auto_rebind": 0}, tgt=("TGT-04", "TGT-30"))


@pytest.mark.parametrize("second_origin", ["https://chatgpt.com/", "about:blank"])
def test_fresh_page_count_detects_addition_and_removal_does_not_unlatch(second_origin, evidence_case):
    """TGT-21/30: a cached single-page snapshot does not override fresh metadata."""
    session, cached = bound_session()
    transport = session.fixture_transport
    transport.add_page(SimplePage("SYNTHETIC-TARGET-B", second_origin))
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    transport._pages.pop()
    assert not session.bind_chatgpt_page().ok
    assert not transport.primitive_commands
    trace(evidence_case, session, expected={"dispatch": 0, "automatic_unlatch": 0},
          actual={"dispatch": 0, "automatic_unlatch": 0}, tgt=("TGT-21", "TGT-30"))


def test_target_page_add_event_is_latched_even_if_page_disappears_before_check(evidence_case):
    """TGT-21/30: transient page-set violations stay terminal for the old record."""
    session, cached = bound_session()
    transport = session.fixture_transport
    transport.emit("Target.targetCreated", {"targetInfo": {
        "targetId": "SYNTHETIC-TARGET-B", "type": "page", "url": "https://chatgpt.com/"}})
    transport.emit("Target.targetDestroyed", {"targetId": "SYNTHETIC-TARGET-B"})
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    assert not transport.primitive_commands
    trace(evidence_case, session, expected={"transient_violation_dispatch": 0, "auto_rebind": 0},
          actual={"transient_violation_dispatch": 0, "auto_rebind": 0}, tgt=("TGT-21", "TGT-30"))


def test_known_nonpage_targets_are_never_attached_or_counted_as_pages(evidence_case):
    """TGT-01 L04 part: worker/service_worker/iframe metadata never selects a Page."""
    session = make_bound_session()
    transport = session.fixture_transport
    transport.extra_targets = [{"targetId": "NONPAGE-" + kind, "type": kind, "url": "https://chatgpt.com/"}
                               for kind in ("worker", "service_worker", "iframe")]
    assert session.bind_chatgpt_page().ok
    assert session._bound_page().ops.evaluate("() => true") == {"synthetic": True}
    assert all(not str(row.get("target_id", "")).startswith("NONPAGE-") for row in transport.audit)
    trace(evidence_case, session, expected={"nonpage_attach": 0, "outside_target_dispatch": 0},
          actual={"nonpage_attach": 0, "outside_target_dispatch": 0}, ac=("AC01",), tgt=("TGT-01",), bind=("BIND-01",))


@pytest.mark.parametrize("kind", ["duplicate_id", "subtype", "missing_set", "wrong_type", "filter_unsupported"])
def test_incomplete_or_unsupported_target_inventory_is_never_a_weak_fallback(kind, evidence_case):
    """TGT-21/22: malformed or unsupported filtered inventories fail closed."""
    session = make_bound_session()
    transport = session.fixture_transport
    info = transport.info(transport._pages[0])
    if kind == "duplicate_id":
        transport.target_infos = [info, dict(info)]
    elif kind == "subtype":
        transport.target_infos = [{**info, "subtype": "prerender"}]
    elif kind == "missing_set":
        transport.target_infos = []
    elif kind == "wrong_type":
        transport.target_infos = [{**info, "type": "unexpected"}]
    else:
        def unsupported(method, params, page):
            if method == "Target.getTargets":
                assert params == {"filter": PAGE_FILTER}
                raise GuardError("PROTOCOL_UNSUPPORTED")
        transport.before_send = unsupported
    result = session.bind_chatgpt_page()
    assert not result.ok and not transport.primitive_commands
    assert not any(r["scope"] == "page_metadata" for r in transport.audit)
    trace(evidence_case, session, expected={"condition": kind, "dispatch": 0, "weak_fallback": 0},
          actual={"condition": kind, "dispatch": 0, "weak_fallback": 0}, tgt=("TGT-21", "TGT-22"))


@pytest.mark.parametrize("url", ["http://chatgpt.com/", "https://www.chatgpt.com/", "https://chatgpt.com:444/", "https://example.org/", "about:blank"])
def test_target_origin_drift_never_navigates_back_or_uses_another_page(url, evidence_case):
    """TGT-06: all unsupported Origin forms deny content before dispatch."""
    session, cached = bound_session()
    transport = session.fixture_transport
    transport._pages[0].raw.url = url
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    assert not transport.primitive_commands
    trace(evidence_case, session, expected={"origin_valid": False, "dispatch": 0, "automatic_navigation": 0},
          actual={"origin_valid": False, "dispatch": 0, "automatic_navigation": 0}, tgt=("TGT-06",))


@pytest.mark.parametrize("mismatch", ["root_target", "page_target", "main_frame_origin", "main_frame_parent", "main_frame_loader", "main_frame_url"])
def test_exact_target_page_and_main_frame_mapping_is_mandatory(mismatch, evidence_case):
    """TGT-01/06/24: exact attached session metadata must agree before content."""
    session = make_bound_session()
    transport = session.fixture_transport
    info = transport.info(transport._pages[0])
    if mismatch == "root_target":
        transport.root_info = {**info, "targetId": "SYNTHETIC-TARGET-B"}
    elif mismatch == "page_target":
        transport.page_info = {**info, "targetId": "SYNTHETIC-TARGET-B"}
    else:
        frame = {"id": "FRAME-A", "loaderId": "LOADER-A", "url": "https://chatgpt.com/", "securityOrigin": "https://chatgpt.com"}
        if mismatch == "main_frame_origin":
            frame["securityOrigin"] = "https://example.org"
        elif mismatch == "main_frame_parent":
            frame["parentId"] = "OTHER-FRAME"
        elif mismatch == "main_frame_loader":
            del frame["loaderId"]
        else:
            frame["url"] = "https://chatgpt.com/other"
        transport.frame_tree = {"frameTree": {"frame": frame}}
    result = session.bind_chatgpt_page()
    assert not result.ok and not transport.primitive_commands
    trace(evidence_case, session, expected={"mismatch": mismatch, "content_dispatch": 0},
          actual={"mismatch": mismatch, "content_dispatch": 0}, tgt=("TGT-01", "TGT-06", "TGT-24"))


@pytest.mark.parametrize("stage", ["initial_epoch", "post_connect_epoch", "later_epoch"])
def test_epoch_change_uses_only_approved_websocket_and_never_reconnects(stage, evidence_case):
    """TGT-05/19: HTTP is never handed to the connection API; epoch changes stop."""
    session = make_bound_session()
    transport = session.fixture_transport
    if stage == "initial_epoch":
        transport.ws = WS_B
    elif stage == "post_connect_epoch":
        transport.discovery_values = [WS_A, WS_B]
    else:
        assert session.bind_chatgpt_page().ok
        transport.ws = WS_B
    result = session.bind_chatgpt_page()
    assert not result.ok and result.failure == FailureKind.TARGET_IDENTITY_MISMATCH
    assert all(ws == WS_A for ws in transport.connections)
    assert len(transport.connections) == (0 if stage == "initial_epoch" else 1)
    assert not transport.primitive_commands
    trace(evidence_case, session, expected={"stage": stage, "replacement_epoch_connections": 0, "content_dispatch": 0},
          actual={"stage": stage, "replacement_epoch_connections": sum(ws == WS_B for ws in transport.connections), "content_dispatch": 0},
          tgt=("TGT-05", "TGT-19"), bind=("BIND-01", "BIND-02"))


@pytest.mark.parametrize("endpoint", ["http://localhost:9222", "http://127.0.0.1:9223", "http://127.0.0.1:9222/"])
def test_endpoint_rejection_precedes_every_network_like_fixture_command(endpoint, evidence_case):
    """TGT-17 L04 consumer part: exact endpoint validation precedes discovery."""
    session = make_bound_session(cdp_url=endpoint)
    result = session.bind_chatgpt_page()
    assert not result.ok and not session.fixture_transport.audit
    trace(evidence_case, session, expected={"discovery": 0, "connections": 0},
          actual={"discovery": 0, "connections": 0}, tgt=("TGT-17",), bind=("BIND-01",))


def test_cached_raw_target_id_is_not_used_instead_of_attached_metadata(evidence_case):
    """TGT-24: stale cached IDs cannot replace fresh exact TargetInfo."""
    session = make_bound_session()
    transport = session.fixture_transport
    transport._pages[0].raw._target_id = "STALE-CACHED-ID"
    result = session.bind_chatgpt_page()
    assert result.ok and result.page.target_id == "SYNTHETIC-TARGET-A"
    assert session._bound_page().ops.evaluate("() => true") == {"synthetic": True}
    assert all(x["target_id"] == "SYNTHETIC-TARGET-A" for x in transport.primitive_commands)
    trace(evidence_case, session, expected={"actual_target": "SYNTHETIC-TARGET-A", "stale_target_dispatch": 0},
          actual={"actual_target": result.page.target_id, "stale_target_dispatch": 0},
          ac=("AC01",), tgt=("TGT-24",), bind=("BIND-01",))


@pytest.mark.parametrize("condition", ["expiry_equal", "expiry_after", "wall_back", "wall_forward", "mono_back", "suspend", "unknown_continuity"])
def test_lease_and_clock_failures_poison_cached_references_without_renewal(condition, evidence_case):
    """TGT-11/25: first UTC/monotonic deadline, discontinuity or resume ends rights."""
    session, cached = bound_session()
    clock = session.fixture_clock
    if condition == "expiry_equal":
        clock.advance(14400)
    elif condition == "expiry_after":
        clock.advance(14401)
    elif condition == "wall_back":
        clock.advance(0, wall=-6, mono=0)
    elif condition == "wall_forward":
        clock.advance(0, wall=6, mono=0)
    elif condition == "mono_back":
        clock.monotonic -= 1
    elif condition == "suspend":
        clock.suspend_epoch += 1
    else:
        clock.continuity_known = False
    with pytest.raises(GuardError) as caught:
        cached.ops.evaluate("() => true")
    expected_code = "BINDING_EXPIRED" if condition.startswith("expiry") else "CLOCK_DISCONTINUITY"
    assert caught.value.code == expected_code
    assert not session.bind_chatgpt_page().ok
    assert not session.fixture_transport.primitive_commands
    trace(evidence_case, session, expected={"reason": expected_code, "dispatch": 0, "renewal": 0},
          actual={"reason": caught.value.code, "dispatch": 0, "renewal": 0}, tgt=("TGT-11", "TGT-25"))


def test_last_instant_before_expiry_passes_but_monotonic_deadline_is_exclusive(evidence_case):
    """TGT-11: expiry equality is denied even when UTC remains just before it."""
    session, cached = bound_session()
    clock = session.fixture_clock
    clock.advance(14399)
    assert cached.ops.evaluate("() => true") == {"synthetic": True}
    clock.advance(0, wall=0, mono=1)
    with pytest.raises(GuardError) as caught:
        cached.ops.evaluate("() => true")
    assert caught.value.code == "BINDING_EXPIRED"
    assert len(session.fixture_transport.primitive_commands) == 1
    trace(evidence_case, session, expected={"before_deadline_dispatch": 1, "at_deadline_dispatch": 0},
          actual={"before_deadline_dispatch": 1, "at_deadline_dispatch": 0}, tgt=("TGT-11", "TGT-25"))


@pytest.mark.parametrize("mode", ["DIAGNOSTIC_ONLY", "RECONCILIATION_REQUIRED"])
def test_overlay_modes_deny_new_mutation_even_while_identity_is_valid(mode, evidence_case):
    """TGT-27: identity BOUND cannot authorize mutation in restricted overlays."""
    session, cached = bound_session()
    session.fixture_authority.set_mode(mode)
    with pytest.raises(GuardError):
        cached.ops.trusted_click_marked_action("[data-synthetic]", timeout_ms=1)
    assert not session.fixture_transport.primitive_commands
    trace(evidence_case, session, expected={"mode": mode, "mutation_dispatch": 0},
          actual={"mode": mode, "mutation_dispatch": 0}, tgt=("TGT-27",), bind=("BIND-03",))


def test_same_target_same_origin_navigation_settles_with_original_binding(evidence_case):
    """TGT-12: authorized same-target navigation changes no Binding generation."""
    session, cached = bound_session()
    current = session.fixture_authority.current
    cached.ops.goto("https://chatgpt.com/next", timeout_ms=1)
    result = session.bind_chatgpt_page()
    assert result.ok and result.page.target_id == "SYNTHETIC-TARGET-A"
    assert session.fixture_authority.current == current
    commands = session.fixture_transport.primitive_commands
    assert len(commands) == 1 and commands[0]["method"] == "Page.navigate"
    trace(evidence_case, session, expected={"navigation_dispatch": 1, "binding_id": BINDING_ID, "rebind": 0},
          actual={"navigation_dispatch": 1, "binding_id": current.binding_id, "rebind": 0},
          tgt=("TGT-12",), bind=("BIND-01",))


def test_pre_and_post_guards_detect_generation_change_during_one_dispatch(evidence_case):
    """TGT-27: post-dispatch uncertainty never repeats the primitive."""
    session, cached = bound_session()
    transport = session.fixture_transport
    def revoke_after(method, page):
        session.fixture_authority.current = replace(session.fixture_authority.current, revoked=True)
    transport.after_primitive = revoke_after
    try:
        cached.ops.trusted_click_marked_action("[data-synthetic]", timeout_ms=1)
    except GuardError:
        pass
    with pytest.raises(GuardError):
        cached.ops.trusted_click_marked_action("[data-synthetic]", timeout_ms=1)
    assert len(transport.primitive_commands) == 1
    trace(evidence_case, session, expected={"initial_dispatch": 1, "automatic_retry": 0, "later_dispatch": 0},
          actual={"initial_dispatch": 1, "automatic_retry": 0, "later_dispatch": 0},
          tgt=("TGT-27",), bind=("BIND-03",))


def test_known_connection_loss_cannot_reconnect_or_rebind(evidence_case):
    """TGT-14/36: operational-channel loss is terminal for the old record."""
    session, cached = bound_session()
    transport = session.fixture_transport
    transport.connected = False
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    transport.connected = True
    assert not session.bind_chatgpt_page().ok
    assert transport.connections == [WS_A] and not transport.primitive_commands
    trace(evidence_case, session, expected={"reconnect": 0, "dispatch": 0},
          actual={"reconnect": len(transport.connections) - 1, "dispatch": 0}, tgt=("TGT-14", "TGT-36"))


@pytest.mark.parametrize("event", ["CDP.disconnected", "Inspector.detached", "Target.detachedFromTarget"])
def test_operational_disconnect_events_latch_even_if_transport_looks_connected(event, evidence_case):
    """TGT-14/36: a known operational disconnect cannot be hidden by a live flag."""
    session, cached = bound_session()
    transport = session.fixture_transport
    transport.emit(event, {})
    assert transport.is_connected()
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    assert not transport.primitive_commands and transport.connections == [WS_A]
    trace(evidence_case, session, expected={"event": event, "dispatch": 0, "reconnect": 0},
          actual={"event": event, "dispatch": 0, "reconnect": 0}, tgt=("TGT-14", "TGT-36"))


def test_offorigin_navigation_is_rejected_before_dispatch(evidence_case):
    """TGT-06: an operation cannot navigate the fixed page outside its Origin."""
    session, cached = bound_session()
    with pytest.raises(GuardError):
        cached.ops.goto("https://example.org/", timeout_ms=1)
    assert not session.fixture_transport.primitive_commands
    trace(evidence_case, session, expected={"navigation_dispatch": 0}, actual={"navigation_dispatch": 0}, tgt=("TGT-06",))


def test_replacement_page_object_cannot_reauthorize_a_retained_page_reference(evidence_case):
    """TGT-10/24: Page object correspondence is rechecked despite matching ID/URL."""
    session, cached = bound_session()
    transport = session.fixture_transport
    transport._pages.clear()
    transport.add_page(SimplePage("SYNTHETIC-TARGET-A"))
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    assert not transport.primitive_commands
    trace(evidence_case, session, expected={"old_reference_dispatch": 0, "new_page_dispatch": 0},
          actual={"old_reference_dispatch": 0, "new_page_dispatch": 0}, tgt=("TGT-10", "TGT-24"))


def test_normal_close_only_releases_owned_transport_and_poisoned_cached_page(evidence_case):
    """TGT-36: Session release does not close a browser or operate a target."""
    session, cached = bound_session()
    transport = session.fixture_transport
    session.close()
    with pytest.raises(GuardError):
        cached.ops.evaluate("() => true")
    assert transport.closed and not transport.primitive_commands
    assert all(r["method"] not in {"Browser.close", "Target.closeTarget", "Target.activateTarget"} for r in transport.audit)
    trace(evidence_case, session, expected={"own_transport_closed": True, "browser_close": 0, "cached_dispatch": 0},
          actual={"own_transport_closed": transport.closed, "browser_close": 0, "cached_dispatch": 0},
          tgt=("TGT-36",))


def test_same_envelope_is_consumed_by_web_and_independent_synthetic_cdp_consumer(evidence_case):
    """BIND-04: an independent synthetic consumer verifies the same envelope.

    This is L04 envelope/identity correspondence only. It implements no L06
    Worker, capture, persistence, Watchdog or post-crash acceptance.
    """
    session, cached = bound_session()
    record = binding_record()
    parsed = ParsedBinding.parse(record)
    projection = TargetBinding.from_dict(record["target"])
    with pytest.raises((TypeError, ValueError)):
        TargetBinding.from_dict(record)
    other = FakeCDPTransport()
    independent_guard = BindingGuard(record, session.fixture_authority, clock=session.fixture_clock)
    independent_guard.check()
    ws = other.discover(projection.endpoint)
    independent_guard.assert_ws(ws)
    other.connect(ws)
    selected = single_page(other.send("Target.getTargets", {"filter": PAGE_FILTER})["targetInfos"])
    assert selected["targetId"] == projection.target_id
    same_target(other.send("Target.getTargetInfo", {"targetId": projection.target_id})["targetInfo"], selected)
    page = other.pages()[0]
    same_target(other.send("Target.getTargetInfo", {}, page=page)["targetInfo"], selected)
    frame_snapshot(other.send("Page.getFrameTree", {}, page=page), selected)
    independent_guard.check()
    assert parsed.record_hash == session.fixture_authority.current.record_hash
    assert cached.ops.evaluate("() => true") == {"synthetic": True}
    web = {"endpoint": ENDPOINT, "epoch": EPOCH, "target_id": cached.identity.target_id,
           "origin": "https://chatgpt.com", "binding_id": BINDING_ID, "record_hash": parsed.record_hash}
    diagnostic = {"endpoint": projection.endpoint, "epoch": derive_browser_epoch(ws),
                  "target_id": selected["targetId"], "origin": projection.expected_origin,
                  "binding_id": independent_guard.record.binding_id, "record_hash": independent_guard.record.record_hash}
    assert web == diagnostic
    assert other.primitive_commands == []
    evidence_case(expected={"same_envelope": True, "outside_target_dispatch": 0, "diagnostic_mutation": 0},
                  actual={"same_envelope": web == diagnostic, "web": web, "independent_consumer": diagnostic,
                          "outside_target_dispatch": 0, "diagnostic_mutation": 0,
                          "scope": "L04_SYNTHETIC_IDENTITY_CONSUMER_NOT_L06_WORKER"},
                  commands=session.fixture_transport.audit + other.audit,
                  ac=["AC01", "AC04"], tgt=["TGT-01", "TGT-23", "TGT-24"], bind=["BIND-01", "BIND-04"])
