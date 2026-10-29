"""L04 synthetic lease/registry/strict-record cases; no network or browser."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import threading
import uuid

import pytest

from chatgpt_web_api.target_guard import (
    AuthoritySnapshot, BindingGuard, ClockSample, GuardError, ParsedBinding,
    canonical_bytes, derive_browser_epoch, frame_snapshot, normalize_origin,
    same_target, single_page, strict_json_loads, validate_endpoint,
)
from cdpd_chatgpt_adapter.l03_binding import L03BindingBridge

BASE = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)
EPOCH = "254095102094427e3ef2c333da294588"
ID = "22222222-2222-4222-8222-222222222222"


def record(binding_id=ID):
    return ParsedBinding(binding_id, "SYNTHETIC-TARGET-A", EPOCH,
                         BASE.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         (BASE + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"))


class FakeClock:
    def __init__(self):
        self.value = ClockSample(BASE, 1000.0, 0)

    def sample(self):
        return self.value

    def advance(self, utc, mono=None):
        self.value = replace(self.value, utc=self.value.utc + timedelta(seconds=utc),
                             monotonic=self.value.monotonic + (utc if mono is None else mono))


class FakeAuthority:
    def __init__(self, rec):
        self.current = AuthoritySnapshot(rec.binding_id, rec.record_hash, 1)
        self.calls = []

    def snapshot(self):
        return self.current

    def invalidate(self, reason, *, binding_id=None, generation=None):
        self.calls.append(("invalidate", reason, binding_id, generation))
        if binding_id in (None, self.current.binding_id) and generation in (None, self.current.generation):
            self.current = replace(self.current, revoked=True)

    def set_mode(self, mode, *, binding_id=None, generation=None):
        self.calls.append(("mode", mode, binding_id, generation))
        if binding_id not in (None, self.current.binding_id) or generation not in (None, self.current.generation):
            raise GuardError("BINDING_REVOKED")
        self.current = replace(self.current, mode=mode)


def fixture_guard():
    rec, clock = record(), FakeClock()
    authority = FakeAuthority(rec)
    return BindingGuard(rec, authority, clock=clock), authority, clock


def emit(evidence_case, *, expected, actual, tgt=None, ac=None, bind=None):
    evidence_case(expected=expected, actual=actual, commands=[],
                  ac=ac or ["AC01", "AC02"], tgt=tgt or ["TGT-11", "TGT-25"], bind=bind or ["BIND-02"])


def test_record_hash_matches_l03_canonical_bytes(evidence_case):
    """L03 envelope canonical hash and all identity/lease fields remain unchanged."""
    rec = record()
    raw = canonical_bytes(rec.to_dict())
    got = ParsedBinding.parse(raw)
    assert got == rec and got.record_hash == hashlib.sha256(raw).hexdigest()
    assert raw.endswith(b"\n")
    emit(evidence_case, expected={"same_record": True, "same_hash": True},
         actual={"same_record": got == rec, "same_hash": got.record_hash == rec.record_hash,
                 "binding_id": rec.binding_id, "epoch": rec.expected_browser_id, "record_hash": rec.record_hash},
         tgt=["TGT-01", "TGT-23"], bind=["BIND-01", "BIND-04"])


@pytest.mark.parametrize("change", ["extra", "missing_epoch", "null_epoch", "wrong_endpoint", "wrong_origin", "ttl", "date", "uuid", "id_control"])
def test_malformed_envelopes_refused(change, evidence_case):
    """Unknown/missing/invalid record fields are denied before any browser command."""
    data = record().to_dict()
    if change == "extra": data["approved"] = True
    elif change == "missing_epoch": data["target"].pop("expected_browser_id")
    elif change == "null_epoch": data["target"]["expected_browser_id"] = None
    elif change == "wrong_endpoint": data["target"]["endpoint"] = "http://localhost:9222"
    elif change == "wrong_origin": data["target"]["expected_origin"] = "https://example.invalid"
    elif change == "ttl": data["expires_at_utc"] = "2026-10-24T04:00:01Z"
    elif change == "date": data["issued_at_utc"] = "2026-02-30T00:00:00Z"
    elif change == "uuid": data["binding_id"] = "00000000-0000-0000-0000-000000000000"
    elif change == "id_control": data["target"]["target_id"] = "A\nB"
    with pytest.raises(GuardError) as error:
        ParsedBinding.parse(data)
    assert error.value.code == "BINDING_RECORD_INVALID"
    emit(evidence_case, expected={"reason": "BINDING_RECORD_INVALID", "dispatch": 0},
         actual={"reason": error.value.code, "dispatch": 0}, tgt=["TGT-24"], ac=["AC01", "AC02", "AC04"])


@pytest.mark.parametrize("raw", ['{"schema":1,"schema":2}', '{"x":NaN}', '{"x":1e9999}', ' ' * 65537])
def test_strict_json_parser_rejects_ambiguous_or_oversize(raw, evidence_case):
    """Duplicate keys, nonfinite numbers and oversize envelopes are rejected."""
    with pytest.raises(GuardError) as error:
        strict_json_loads(raw)
    assert error.value.code == "BINDING_RECORD_INVALID"
    emit(evidence_case, expected={"reason": "BINDING_RECORD_INVALID"}, actual={"reason": error.value.code}, tgt=["TGT-24"])


@pytest.mark.parametrize("delta,allowed", [(14399.999, True), (14400.0, False), (14400.001, False)])
def test_expiry_exclusive(delta, allowed, evidence_case):
    """Only now strictly before expiry is usable; equality and later are terminal."""
    guard, authority, clock = fixture_guard()
    clock.advance(delta)
    if allowed:
        guard.check(mutation=True)
        actual = "BOUND"
    else:
        with pytest.raises(GuardError) as error: guard.check(mutation=True)
        actual = error.value.code
        assert actual == "BINDING_EXPIRED" and guard.state == "EXPIRED"
    emit(evidence_case, expected={"state_or_reason": "BOUND" if allowed else "BINDING_EXPIRED"},
         actual={"state_or_reason": actual, "expires_unchanged": guard.record.expires_at_utc == record().expires_at_utc}, tgt=["TGT-11"])


@pytest.mark.parametrize("utc,mono", [(14399, 14400), (14400, 14399)])
def test_earlier_utc_or_monotonic_deadline_wins(utc, mono, evidence_case):
    """An earlier monotonic or UTC deadline cannot be extended by the other clock."""
    guard, _, clock = fixture_guard()
    clock.advance(utc, mono)
    with pytest.raises(GuardError) as error: guard.check()
    assert error.value.code == "BINDING_EXPIRED"
    emit(evidence_case, expected={"reason": "BINDING_EXPIRED"}, actual={"reason": error.value.code}, tgt=["TGT-11", "TGT-25"])


@pytest.mark.parametrize("drift,allowed", [(5.0, True), (-5.0, True), (5.001, False), (-5.001, False)])
def test_clock_drift_boundary(drift, allowed, evidence_case):
    """Absolute UTC/monotonic drift over five seconds invalidates the generation."""
    guard, _, clock = fixture_guard()
    clock.advance(100 + drift, 100)
    if allowed:
        guard.check()
        actual = "BOUND"
    else:
        with pytest.raises(GuardError) as error: guard.check()
        actual = error.value.code
        assert actual == "CLOCK_DISCONTINUITY"
    emit(evidence_case, expected={"state_or_reason": "BOUND" if allowed else "CLOCK_DISCONTINUITY"},
         actual={"state_or_reason": actual}, tgt=["TGT-25"])


@pytest.mark.parametrize("change", ["suspend", "unknown", "mono_back", "nan", "utc_naive"])
def test_unknown_or_suspended_clock_is_terminal(change, evidence_case):
    """Sleep/resume, unknown continuity, invalid and backward clocks cannot recover."""
    guard, _, clock = fixture_guard()
    if change == "suspend": clock.value = replace(clock.value, suspend_epoch=1)
    elif change == "unknown": clock.value = replace(clock.value, continuity_known=False)
    elif change == "mono_back": clock.value = replace(clock.value, monotonic=999.0)
    elif change == "nan": clock.value = replace(clock.value, monotonic=float("nan"))
    elif change == "utc_naive": clock.value = replace(clock.value, utc=BASE.replace(tzinfo=None))
    with pytest.raises(GuardError) as error: guard.check()
    assert error.value.code == "CLOCK_DISCONTINUITY"
    clock.value = ClockSample(BASE, 1000.0, 0)
    with pytest.raises(GuardError) as again: guard.check()
    assert again.value.code == "CLOCK_DISCONTINUITY"
    emit(evidence_case, expected={"reason": "CLOCK_DISCONTINUITY", "resurrection": False},
         actual={"reason": error.value.code, "resurrection": False}, tgt=["TGT-25", "TGT-30"])


@pytest.mark.parametrize("change,reason", [("generation", "BINDING_REVOKED"), ("binding_id", "BINDING_REVOKED"),
                                           ("hash", "BINDING_RECORD_INVALID"), ("revoked", "BINDING_REVOKED"),
                                           ("superseded", "BINDING_REVOKED"), ("unknown", "CLOCK_DISCONTINUITY")])
def test_authoritative_current_generation_required(change, reason, evidence_case):
    """Current ID/hash/generation/revocation and continuity must all agree."""
    guard, authority, _ = fixture_guard()
    fields = {"generation": {"generation": 2}, "binding_id": {"binding_id": "33333333-3333-4333-8333-333333333333"},
              "hash": {"record_hash": "0" * 64}, "revoked": {"revoked": True},
              "superseded": {"superseded": True}, "unknown": {"continuity_known": False}}
    authority.current = replace(authority.current, **fields[change])
    with pytest.raises(GuardError) as error: guard.check()
    assert error.value.code == reason
    emit(evidence_case, expected={"reason": reason, "dispatch": 0}, actual={"reason": error.value.code, "dispatch": 0}, tgt=["TGT-10", "TGT-24"])


@pytest.mark.parametrize("mode,reason", [("DIAGNOSTIC_ONLY", "POLICY_DENIED"), ("RECONCILIATION_REQUIRED", "RECONCILIATION_REQUIRED")])
def test_overlays_forbid_new_mutation(mode, reason, evidence_case):
    """Diagnostic or uncertain owner permits validation but no new mutation."""
    guard, authority, _ = fixture_guard()
    if mode == "DIAGNOSTIC_ONLY": guard.set_diagnostic_only()
    else: guard.require_reconciliation()
    guard.check(mutation=False)
    with pytest.raises(GuardError) as error: guard.check(mutation=True)
    assert error.value.code == reason and authority.current.mode == mode
    emit(evidence_case, expected={"reason": reason, "mutation_dispatch": 0},
         actual={"reason": error.value.code, "mutation_dispatch": 0, "mode": authority.current.mode},
         tgt=["TGT-27", "TGT-36"], bind=["BIND-03"])


def test_revalidation_does_not_renew_and_navigation_keeps_record(evidence_case):
    """Same-target settled navigation only restores document state, not the lease."""
    guard, _, clock = fixture_guard()
    before = guard.record.to_bytes()
    clock.advance(10)
    for _ in range(4): guard.check()
    guard.begin_navigation()
    with pytest.raises(GuardError) as error: guard.check(mutation=True)
    assert error.value.code == "WORK_NOT_SETTLED"
    guard.check(allow_navigating=True)
    guard.navigation_settled()
    assert guard.record.to_bytes() == before and guard.state == "BOUND"
    emit(evidence_case, expected={"record_unchanged": True, "state": "BOUND", "new_binding": 0},
         actual={"record_unchanged": guard.record.to_bytes() == before, "state": guard.state, "new_binding": 0},
         tgt=["TGT-09", "TGT-12"])


def test_fixed_reason_mapping_has_no_target_unbound_enum(evidence_case):
    """Approved profile reason mapping uses existing Web API failure enum values."""
    expected = {"BINDING_REQUIRED": "SESSION_BINDING_FAILURE", "BINDING_RECORD_INVALID": "SESSION_BINDING_FAILURE",
                "BROWSER_IDENTITY_MISMATCH": "TARGET_IDENTITY_MISMATCH", "TARGET_BINDING_INVALID": "TARGET_IDENTITY_MISMATCH",
                "TARGET_ORIGIN_MISMATCH": "TARGET_IDENTITY_MISMATCH", "TARGET_AMBIGUOUS": "AMBIGUOUS_OBSERVATION",
                "SINGLE_PAGE_VIOLATION": "AMBIGUOUS_OBSERVATION", "BINDING_EXPIRED": "SESSION_BINDING_FAILURE"}
    actual = {reason: GuardError(reason).failure_kind.value for reason in expected}
    assert actual == expected
    emit(evidence_case, expected=expected, actual=actual, tgt=["TGT-08", "TGT-24"], ac=["AC02", "AC03", "AC04"])


class FakeOwnerRecord:
    def __init__(self, rec): self.rec = rec
    def to_bytes(self): return self.rec.to_bytes()


class FakeOwner:
    """Fixture for the unchanged L03's exposed memory/lease boundary, not CDP."""
    def __init__(self, clock):
        self.clock = clock
        self._lock = threading.Lock()
        self.current_record = None
        self.state = "UNBOUND"
        self.calls = 0
        self.emit_id = None
        self.on_preflight = None
        self.lease_error = None

    def _check_lease(self):
        if self.lease_error: raise GuardError(self.lease_error)
        if self.current_record is None: raise GuardError("BINDING_REQUIRED")
        rec = self.current_record.rec
        if self.clock.sample().utc >= datetime.strptime(rec.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc):
            raise GuardError("BINDING_EXPIRED")

    def preflight(self, _config, **_kwargs):
        self.calls += 1
        if self.on_preflight: self.on_preflight()
        now = self.clock.sample().utc.replace(microsecond=0)
        rec = ParsedBinding(self.emit_id or str(uuid.uuid4()), "SYNTHETIC-TARGET-A", EPOCH,
                            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            (now + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.current_record, self.state = FakeOwnerRecord(rec), "BOUND"
        return {"status": "PASS", "state": "BOUND", "binding": rec.to_dict(), "binding_sha256": rec.record_hash}


def bridge_fixture():
    clock = FakeClock()
    owner = FakeOwner(clock)
    bridge = L03BindingBridge(owner, clock=clock)
    result = bridge.preflight(object(), policy_allowed=True, work_settled=True)
    return bridge, owner, clock, result


def test_live_owner_bridge_same_envelope_projection(evidence_case):
    """Web and independent generic consumer share one admitted L03 envelope/hash."""
    bridge, owner, _, result = bridge_fixture()
    web_guard = bridge.create_guard()
    consumer_guard = bridge.create_guard()
    projected = bridge.target_projection()
    web_guard.check(mutation=True)
    consumer_guard.check()
    assert projected.target_id == web_guard.record.target_id == consumer_guard.record.target_id
    assert web_guard.record.to_dict() == result["binding"]
    assert web_guard.record.record_hash == consumer_guard.record.record_hash == result["binding_sha256"]
    assert owner.calls == 1
    emit(evidence_case, expected={"same_envelope": True, "preflight_calls": 1, "projection_keys": 4},
         actual={"same_envelope": True, "preflight_calls": owner.calls, "projection_keys": len(result["binding"]["target"]),
                 "binding_id": web_guard.record.binding_id, "record_hash": web_guard.record.record_hash,
                 "epoch": web_guard.record.expected_browser_id, "target_id": projected.target_id},
         tgt=["TGT-01", "TGT-23"], ac=["AC01", "AC04"], bind=["BIND-01", "BIND-04"])


def test_bridge_rejects_unmonitored_preexisting_record(evidence_case):
    """An owner that preflights before continuity keeper cannot be retroactively admitted."""
    clock, owner = FakeClock(), None
    owner = FakeOwner(clock)
    owner.preflight(object())
    with pytest.raises(GuardError) as error: L03BindingBridge(owner, clock=clock)
    assert error.value.code == "CLOCK_DISCONTINUITY"
    emit(evidence_case, expected={"reason": "CLOCK_DISCONTINUITY"}, actual={"reason": error.value.code}, tgt=["TGT-25", "TGT-24"])


def test_bridge_requires_explicit_preflight_and_unique_registry(evidence_case):
    """Raw record parsing creates no registry authority; only explicit fresh preflight admits."""
    clock = FakeClock()
    owner = FakeOwner(clock)
    bridge = L03BindingBridge(owner, clock=clock)
    with pytest.raises(GuardError) as absent: bridge.create_guard()
    with pytest.raises(GuardError) as duplicate: L03BindingBridge(owner, clock=clock)
    assert absent.value.code == "BINDING_REQUIRED" and duplicate.value.code == "OWNER_BUSY"
    assert owner.calls == 0
    emit(evidence_case, expected={"unregistered": "BINDING_REQUIRED", "duplicate": "OWNER_BUSY", "preflight_calls": 0},
         actual={"unregistered": absent.value.code, "duplicate": duplicate.value.code, "preflight_calls": owner.calls}, tgt=["TGT-08", "TGT-24"])


@pytest.mark.parametrize("when", ["during", "before_registration"])
def test_bridge_detects_suspend_across_preflight_registration_gap(when, evidence_case):
    """Suspend between issuance and guard registration is covered by the original keeper."""
    clock = FakeClock()
    owner = FakeOwner(clock)
    bridge = L03BindingBridge(owner, clock=clock)
    if when == "during":
        owner.on_preflight = lambda: setattr(clock, "value", replace(clock.value, suspend_epoch=1))
        with pytest.raises(GuardError) as error: bridge.preflight(object(), policy_allowed=True, work_settled=True)
    else:
        bridge.preflight(object(), policy_allowed=True, work_settled=True)
        clock.value = replace(clock.value, suspend_epoch=1)
        with pytest.raises(GuardError) as error: bridge.create_guard()
    assert error.value.code == "CLOCK_DISCONTINUITY" and owner.calls == 1
    emit(evidence_case, expected={"reason": "CLOCK_DISCONTINUITY", "preflight_calls": 1},
         actual={"reason": error.value.code, "preflight_calls": owner.calls}, tgt=["TGT-25"])


def test_terminal_registry_cannot_be_resurrected_by_fresh_guard(evidence_case):
    """An invalidation poisons cached and newly requested guards for the same record."""
    bridge, _, _, _ = bridge_fixture()
    first, cached = bridge.create_guard(), bridge.create_guard()
    first.invalidate("SINGLE_PAGE_VIOLATION")
    with pytest.raises(GuardError) as old: cached.check()
    with pytest.raises(GuardError) as new: bridge.create_guard()
    assert old.value.code == new.value.code == "SINGLE_PAGE_VIOLATION"
    emit(evidence_case, expected={"cached_denied": True, "new_guard_denied": True},
         actual={"cached_denied": True, "new_guard_denied": True, "reason": new.value.code}, tgt=["TGT-10", "TGT-30"])


def test_new_explicit_preflight_has_new_generation_old_ref_cannot_retarget(evidence_case):
    """Only a new explicit Preflight changes generation; stale guards never retarget it."""
    bridge, owner, clock, _ = bridge_fixture()
    old = bridge.create_guard()
    old_id = old.record.binding_id
    clock.advance(1)
    bridge.preflight(object(), policy_allowed=True, work_settled=True)
    new = bridge.create_guard()
    with pytest.raises(GuardError) as error: old.check()
    assert error.value.code == "BINDING_REVOKED"
    new.check(mutation=True)
    assert new.record.binding_id != old_id and new.generation == 2 and owner.calls == 2
    emit(evidence_case, expected={"old_denied": True, "new_generation": 2, "explicit_preflight_calls": 2},
         actual={"old_denied": True, "new_generation": new.generation, "explicit_preflight_calls": owner.calls}, tgt=["TGT-04", "TGT-30"])


def test_same_record_from_fake_preflight_cannot_revive_tombstone(evidence_case):
    """Even an asserted new Preflight cannot reuse a tombstoned binding_id."""
    bridge, owner, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    owner.emit_id = guard.record.binding_id
    guard.invalidate("TARGET_MISSING")
    with pytest.raises(GuardError) as error: bridge.preflight(object(), policy_allowed=True, work_settled=True)
    assert error.value.code == "BINDING_REVOKED"
    emit(evidence_case, expected={"reason": "BINDING_REVOKED", "same_id_resurrection": False},
         actual={"reason": error.value.code, "same_id_resurrection": False}, tgt=["TGT-04", "TGT-30"])


def test_owner_change_or_revocation_cannot_reuse_old_json(evidence_case):
    """A different live owner record object/generation invalidates retained guards."""
    bridge, owner, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    owner.current_record = FakeOwnerRecord(guard.record)
    with pytest.raises(GuardError) as error: guard.check()
    assert error.value.code == "BINDING_REVOKED"
    emit(evidence_case, expected={"reason": "BINDING_REVOKED"}, actual={"reason": error.value.code}, tgt=["TGT-04", "TGT-24"])


def test_overlay_shared_between_web_and_independent_consumer(evidence_case):
    """An uncertain result prevents new mutation through every guard in the generation."""
    bridge, _, _, _ = bridge_fixture()
    first, second = bridge.create_guard(), bridge.create_guard()
    first.require_reconciliation()
    second.check(mutation=False)
    with pytest.raises(GuardError) as error: second.check(mutation=True)
    assert error.value.code == "RECONCILIATION_REQUIRED"
    emit(evidence_case, expected={"reason": "RECONCILIATION_REQUIRED", "new_mutation": 0},
         actual={"reason": error.value.code, "new_mutation": 0}, tgt=["TGT-27"], bind=["BIND-03"])


def test_unchanged_l03_owner_issues_same_envelope_with_synthetic_probe(monkeypatch, evidence_case):
    """Actual fixed L03 owner delegates through synthetic metadata probe; no L03 browser/OS actions."""
    from pathlib import Path
    import importlib.util
    import sys
    path = Path(__file__).resolve().parents[1] / "accepted_l03/code/cdpd_chatgpt_profile/preflight.py"
    name = "_l04_synthetic_l03_preflight"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    clock = FakeClock()
    calls = []
    monkeypatch.setattr(module, "_check_runtime_dependency", lambda: None)
    monkeypatch.setattr(module, "_utc_now", lambda: clock.sample().utc)
    monkeypatch.setattr(module, "_monotonic", lambda: clock.sample().monotonic)

    def synthetic_metadata_probe(config, expected, audit):
        calls.append(("synthetic_probe", expected))
        return "SYNTHETIC-TARGET-A", EPOCH, {"fixture": "SYNTHETIC_NO_OS_INSPECTION"}

    monkeypatch.setattr(module, "_probe", synthetic_metadata_probe)
    owner = module.PreflightOwner()
    bridge = L03BindingBridge(owner, clock=clock)
    config = module.PreflightConfig("synthetic-chrome", "synthetic-profile", "synthetic-state")
    result = bridge.preflight(config, policy_allowed=True, work_settled=True)
    guard = bridge.create_guard()
    guard.check()
    projected = bridge.target_projection()
    assert result["status"] == "PASS" and len(calls) == 1
    assert owner.current_record.to_bytes() == guard.record.to_bytes()
    assert result["binding_sha256"] == guard.record.record_hash
    assert projected.target_id == guard.record.target_id
    assert result["audit"]["websocket_connections"] == result["audit"]["content_commands"] == result["audit"]["mutation_commands"] == 0
    emit(evidence_case, expected={"same_bytes": True, "synthetic_probe_calls": 1, "real_cdp": 0},
         actual={"same_bytes": owner.current_record.to_bytes() == guard.record.to_bytes(),
                 "synthetic_probe_calls": len(calls), "real_cdp": result["audit"]["websocket_connections"],
                 "binding_id": guard.record.binding_id, "record_hash": guard.record.record_hash,
                 "fixed_l03_source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
         tgt=["TGT-01", "TGT-23"], ac=["AC01", "AC04"], bind=["BIND-01", "BIND-04"])


def test_unavailable_native_continuity_fails_closed(monkeypatch, evidence_case):
    """Unavailable native continuity never degrades to UTC-only record admission."""
    from types import SimpleNamespace
    import chatgpt_web_api.target_guard as target_guard
    monkeypatch.setattr(target_guard, "os", SimpleNamespace(name="unsupported"))
    clock = target_guard.SystemClock()
    authority = FakeAuthority(record())
    with pytest.raises(GuardError) as error: BindingGuard(record(), authority, clock=clock)
    assert error.value.code == "CLOCK_DISCONTINUITY"
    emit(evidence_case, expected={"reason": "CLOCK_DISCONTINUITY", "dispatch": 0},
         actual={"reason": error.value.code, "dispatch": 0}, tgt=["TGT-25"])


def test_shared_owner_reservation_serializes_distinct_consumers(evidence_case):
    """Two guards share one operation owner; nested same token cannot admit another."""
    bridge, _, _, _ = bridge_fixture()
    first, second = bridge.create_guard(), bridge.create_guard()
    token_a, token_b = object(), object()
    first.begin_operation(token_a)
    first.begin_operation(token_a)
    with pytest.raises(GuardError) as busy1: second.begin_operation(token_b)
    first.end_operation(token_a)
    with pytest.raises(GuardError) as busy2: second.begin_operation(token_b)
    first.end_operation(token_a)
    second.begin_operation(token_b)
    second.end_operation(token_b)
    assert busy1.value.code == busy2.value.code == "OWNER_BUSY"
    emit(evidence_case, expected={"parallel_admitted": 0, "after_release_admitted": 1},
         actual={"parallel_admitted": 0, "after_release_admitted": 1, "reason": busy1.value.code},
         tgt=["TGT-27"], bind=["BIND-03"])


def test_same_token_from_another_thread_is_not_nested_operation(evidence_case):
    """A shared session token cannot disguise concurrent callers as nesting."""
    bridge, _, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    token = object()
    guard.begin_operation(token)
    observed = []

    def attempt():
        try:
            guard.begin_operation(token)
        except GuardError as error:
            observed.append(error.code)
        else:
            observed.append("UNEXPECTED_ADMISSION")
            guard.end_operation(token)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=2)
    guard.end_operation(token)
    assert not thread.is_alive() and observed == ["OWNER_BUSY"]
    emit(evidence_case, expected={"other_thread": "OWNER_BUSY"}, actual={"other_thread": observed[0]},
         tgt=["TGT-27"], bind=["BIND-03"])


def test_operation_end_does_not_clear_reconciliation(evidence_case):
    """Reservation release never permits a new mutation after unknown outcome."""
    bridge, _, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    token = object()
    guard.begin_operation(token)
    guard.require_reconciliation()
    guard.end_operation(token)
    guard.begin_operation(token)
    with pytest.raises(GuardError) as error: guard.check(mutation=True)
    guard.end_operation(token)
    assert error.value.code == "RECONCILIATION_REQUIRED"
    emit(evidence_case, expected={"reason": "RECONCILIATION_REQUIRED", "retry_dispatch": 0},
         actual={"reason": error.value.code, "retry_dispatch": 0}, tgt=["TGT-27"], bind=["BIND-03"])


def test_expired_inflight_reservation_can_release_without_renewal(evidence_case):
    """An expired operation releases its reservation but leaves binding terminal."""
    bridge, _, clock, _ = bridge_fixture()
    guard = bridge.create_guard()
    token = object()
    guard.begin_operation(token)
    clock.advance(14400)
    with pytest.raises(GuardError) as error: guard.check()
    guard.end_operation(token)
    assert error.value.code == "BINDING_EXPIRED" and bridge._operation_token is None
    with pytest.raises(GuardError) as retry: guard.begin_operation(token)
    assert retry.value.code == "BINDING_EXPIRED"
    emit(evidence_case, expected={"reason": "BINDING_EXPIRED", "released": True, "renewed": False},
         actual={"reason": error.value.code, "released": bridge._operation_token is None, "renewed": False},
         tgt=["TGT-11", "TGT-27"], bind=["BIND-02", "BIND-03"])


def test_explicit_preflight_cannot_replace_active_operation(evidence_case):
    """Even work_settled assertion cannot replace a real reserved active generation."""
    bridge, owner, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    token = object()
    guard.begin_operation(token)
    with pytest.raises(GuardError) as error: bridge.preflight(object(), policy_allowed=True, work_settled=True)
    guard.check()
    guard.end_operation(token)
    assert error.value.code == "WORK_NOT_SETTLED" and owner.calls == 1
    emit(evidence_case, expected={"reason": "WORK_NOT_SETTLED", "preflight_calls": 1},
         actual={"reason": error.value.code, "preflight_calls": owner.calls}, tgt=["TGT-27"], bind=["BIND-03"])


def test_actual_operation_requires_reservation_interface(evidence_case):
    """A predicate-only authority cannot be used for an actual operation dispatch."""
    guard, _, _ = fixture_guard()
    guard.check(mutation=True)
    with pytest.raises(GuardError) as error: guard.begin_operation(object())
    assert error.value.code == "BINDING_REQUIRED"
    emit(evidence_case, expected={"reason": "BINDING_REQUIRED", "dispatch": 0},
         actual={"reason": error.value.code, "dispatch": 0}, tgt=["TGT-08", "TGT-24"], bind=["BIND-02"])


def test_operation_identity_claim_shared_across_guard_recreation(evidence_case):
    """Recreating a wrapper/guard cannot dispatch a claimed Facade operation again."""
    bridge, _, _, _ = bridge_fixture()
    first, second = bridge.create_guard(), bridge.create_guard()
    token_a, token_b = object(), object()
    first.begin_operation(token_a)
    first.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-A")
    first.end_operation(token_a)
    second.begin_operation(token_b)
    with pytest.raises(GuardError) as error:
        second.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-A")
    second.end_operation(token_b)
    assert error.value.code == "RECONCILIATION_REQUIRED" and len(bridge._claimed_operations) == 1
    emit(evidence_case, expected={"claim_count": 1, "duplicate_admitted": 0},
         actual={"claim_count": len(bridge._claimed_operations), "duplicate_admitted": 0}, tgt=["TGT-27"], bind=["BIND-03"])


def test_operation_identity_claim_survives_explicit_preflight(evidence_case):
    """A fresh Binding is not permission to replay the same caller-owned operation."""
    bridge, owner, clock, _ = bridge_fixture()
    first = bridge.create_guard()
    token = object()
    first.begin_operation(token)
    first.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-A")
    first.end_operation(token)
    clock.advance(1)
    bridge.preflight(object(), policy_allowed=True, work_settled=True)
    next_guard = bridge.create_guard()
    next_guard.begin_operation(token)
    with pytest.raises(GuardError) as error:
        next_guard.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-A")
    next_guard.end_operation(token)
    assert error.value.code == "RECONCILIATION_REQUIRED" and owner.calls == 2
    emit(evidence_case, expected={"explicit_preflight": 2, "replay_admitted": 0},
         actual={"explicit_preflight": owner.calls, "replay_admitted": 0}, tgt=["TGT-27"], bind=["BIND-03"])


def test_operation_identity_requires_reservation_and_capability(evidence_case):
    """Claim requires active owner reservation and an implemented shared claim capability."""
    bridge, _, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    with pytest.raises(GuardError) as unreserved:
        guard.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-A")
    bare_guard, _, _ = fixture_guard()
    with pytest.raises(GuardError) as missing:
        bare_guard.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-A")
    assert unreserved.value.code == "OWNER_BUSY" and missing.value.code == "BINDING_REQUIRED"
    emit(evidence_case, expected={"unreserved": "OWNER_BUSY", "missing_capability": "BINDING_REQUIRED"},
         actual={"unreserved": unreserved.value.code, "missing_capability": missing.value.code}, tgt=["TGT-27"], bind=["BIND-03"])


def test_operation_identity_ledger_limit_fails_closed_without_eviction(evidence_case):
    """Full in-memory owner ledger refuses new work and retains all anti-replay keys."""
    bridge, _, _, _ = bridge_fixture()
    guard = bridge.create_guard()
    bridge._claimed_operations = {("execute_prompt_and_wait", "SYNTHETIC-OPERATION-" + str(i)) for i in range(10000)}
    token = object()
    guard.begin_operation(token)
    with pytest.raises(GuardError) as error:
        guard.claim_operation_id("execute_prompt_and_wait", "SYNTHETIC-OPERATION-NEW")
    guard.end_operation(token)
    assert error.value.code == "RECONCILIATION_REQUIRED" and len(bridge._claimed_operations) == 10000
    emit(evidence_case, expected={"ledger_size": 10000, "new_admission": 0},
         actual={"ledger_size": len(bridge._claimed_operations), "new_admission": 0}, tgt=["TGT-27"], bind=["BIND-03"])
