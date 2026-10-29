"""L07 pure decision controls, not native process/keeper/Windows acceptance.

FLT-44 replay and progress, expiry boundary, operation scope and conservative
dedupe.  Virtual receiver time is explicit; no browser, process kill or storage
durability is inferred from these controls.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from cdpd_chatgpt_profile.watchdog import (
    HEARTBEAT_EXPIRY_SECONDS, MAX_SUPERVISOR_POLL_SECONDS,
    PRODUCER_INTERVAL_SECONDS, HeartbeatDisposition, HeartbeatTracker,
    ObservationSource, OncePerOperation, OperationScope, TriggerKind,
    WatchdogError, WatchdogTrigger, select_trigger,
)


BINDING = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "33333333-3333-4333-8333-333333333333"


def scope(**updates):
    base = OperationScope("run-A", "operation-A", BINDING, 1,
                          OWNER, "consumer", "consumer-instance-A")
    return replace(base, **updates)


def envelope(sequence=1, *, occurred_at="2026-10-25T00:00:00Z"):
    return {
        "schema": "cdpd.chatgpt-event-envelope/v1",
        "event_id": "44444444-4444-4444-8444-444444444444",
        "producer_seq": sequence, "occurred_at_utc": occurred_at,
        "binding_id": BINDING, "binding_sha256": "a" * 64,
        "control_generation": 1,
        "event": {"schema": "cdpd.event/v1", "producer_id": "consumer",
                  "producer_instance_id": "consumer-instance-A",
                  "operation_id": "operation-A", "kind": "HEARTBEAT",
                  "failure_code": "NONE", "correlation": {"run_id": "run-A"}},
        "profile": {"reason": "HEARTBEAT_SAMPLE", "dispatch_state": "UNKNOWN",
                    "ui_context": "UNDETERMINED", "document_marker": None,
                    "loss_lower_bound": 0, "loss_count_exact": True},
    }


def trigger(kind, when=10.0, *, scoped=None):
    sources = {
        TriggerKind.HEARTBEAT_EXPIRED: ObservationSource.LOCAL_MONOTONIC,
        TriggerKind.CONSUMER_EXITED: ObservationSource.CONFIRMED_PROCESS_EXIT,
        TriggerKind.FAILURE_EVENT: ObservationSource.VERIFIED_DURABLE_EVENT,
        TriggerKind.CHROME_EXITED: ObservationSource.CONFIRMED_PROCESS_EXIT,
        TriggerKind.CDP_DISCONNECTED: ObservationSource.AUTHENTICATED_TRANSPORT,
        TriggerKind.WORKER_FAILED: ObservationSource.OWNED_WORKER,
        TriggerKind.WORKER_DEADLINE: ObservationSource.OWNED_WORKER,
        TriggerKind.CLOCK_OR_KEEPER_LOST: ObservationSource.TRUSTED_KEEPER,
    }
    return WatchdogTrigger(scoped or scope(), kind, when, sources[kind])


def test_initial_window_and_exact_expiry_emit_once(evidence_case):
    tracker = HeartbeatTracker(scope(), 100.0)
    assert tracker.poll(109.999999) is None
    observed = tracker.poll(110.0)
    actual = {"reason": observed.reason_code, "at": observed.observed_monotonic,
              "diagnostic_only": tracker.diagnostic_only,
              "metadata_only": observed.metadata_only,
              "repeat": tracker.poll(111.0)}
    expected = {"reason": "HEARTBEAT_EXPIRED", "at": 110.0,
                "diagnostic_only": True, "metadata_only": False, "repeat": None}
    evidence_case(expected=expected, actual=actual, evt=("FLT-44",))
    assert actual == expected


def test_fresh_sequence_anchors_receiver_time_and_gaps_are_legal():
    tracker = HeartbeatTracker(scope(), 100.0)
    assert tracker.observe(scope(), 1, 102.0) is HeartbeatDisposition.FRESH
    # Liveness coalescing may skip sample numbers; it is not event-history loss.
    assert tracker.observe(scope(), 999, 111.5) is HeartbeatDisposition.FRESH
    assert tracker.last_sequence == 999
    assert tracker.last_received_monotonic == 111.5
    assert tracker.poll(121.49999) is None
    assert tracker.poll(121.5).kind is TriggerKind.HEARTBEAT_EXPIRED


def test_same_sequence_future_utc_replay_does_not_extend_window(evidence_case):
    tracker = HeartbeatTracker(scope(), 0.0)
    first = envelope(5)
    assert tracker.observe(OperationScope.from_envelope(first, supervisor_instance_id=OWNER),
                           first["producer_seq"], 2.0) is HeartbeatDisposition.FRESH
    replay = envelope(5, occurred_at="2099-12-31T23:59:59Z")
    replay_scope = OperationScope.from_envelope(replay, supervisor_instance_id=OWNER)
    assert tracker.observe(replay_scope, 5, 11.9) is HeartbeatDisposition.REPLAY
    actual = {"anchor": tracker.last_received_monotonic,
              "expiry": tracker.expires_at_monotonic,
              "reason": tracker.poll(12.0).reason_code}
    expected = {"anchor": 2.0, "expiry": 12.0, "reason": "HEARTBEAT_EXPIRED"}
    evidence_case(expected=expected, actual=actual, evt=("FLT-44",))
    assert actual == expected


def test_lower_sequence_does_not_rewind_or_extend():
    tracker = HeartbeatTracker(scope(), 0.0)
    assert tracker.observe(scope(), 4, 2.0) is HeartbeatDisposition.FRESH
    assert tracker.observe(scope(), 3, 9.0) is HeartbeatDisposition.STALE
    assert (tracker.last_sequence, tracker.last_received_monotonic) == (4, 2.0)
    assert tracker.poll(12.0).reason_code == "HEARTBEAT_EXPIRED"


@pytest.mark.parametrize("updates", [
    {"run_id": "other-run"}, {"operation_id": "other-operation"},
    {"binding_id": OTHER_UUID}, {"control_generation": 2},
    {"supervisor_instance_id": OTHER_UUID}, {"producer_id": "other-producer"},
    {"producer_instance_id": "other-instance"},
])
def test_every_scope_dimension_rejects_foreign_sample(updates):
    tracker = HeartbeatTracker(scope(), 0.0)
    assert tracker.observe(scope(**updates), 100, 9.0) is HeartbeatDisposition.SCOPE_MISMATCH
    assert (tracker.last_sequence, tracker.last_received_monotonic) == (0, 0.0)
    assert tracker.poll(10.0).reason_code == "HEARTBEAT_EXPIRED"


@pytest.mark.parametrize("sequence", [True, False, 0, -1, 1.0, "1", None, 9007199254740992])
def test_invalid_sequence_is_not_a_liveness_sample(sequence):
    tracker = HeartbeatTracker(scope(), 0.0)
    assert tracker.observe(scope(), sequence, 9.0) is HeartbeatDisposition.INVALID_SEQUENCE
    assert tracker.last_sequence == 0
    assert tracker.poll(10.0).kind is TriggerKind.HEARTBEAT_EXPIRED


@pytest.mark.parametrize("receipt_at", [10.0, 10.001, 50.0])
def test_heartbeat_at_or_after_expiry_cannot_erase_unpolled_expiration(receipt_at):
    tracker = HeartbeatTracker(scope(), 0.0)
    assert tracker.observe(scope(), 1, receipt_at) is HeartbeatDisposition.FRESH_DIAGNOSTIC_ONLY
    assert tracker.expires_at_monotonic == 10.0
    observed = tracker.poll(receipt_at)
    assert observed.kind is TriggerKind.HEARTBEAT_EXPIRED
    assert observed.observed_monotonic == receipt_at
    assert tracker.poll(receipt_at + 1) is None


def test_heartbeat_return_never_resumes_diagnostic_only(evidence_case):
    tracker = HeartbeatTracker(scope(), 0.0)
    first = tracker.poll(10.0)
    dispositions = [tracker.observe(scope(), seq, instant).value
                    for seq, instant in [(1, 11.0), (2, 13.0), (3, 15.0)]]
    actual = {"first_reason": first.reason_code, "later": dispositions,
              "diagnostic_only": tracker.diagnostic_only,
              "second_expiration": tracker.poll(100.0)}
    expected = {"first_reason": "HEARTBEAT_EXPIRED",
                "later": ["FRESH_DIAGNOSTIC_ONLY"] * 3,
                "diagnostic_only": True, "second_expiration": None}
    evidence_case(expected=expected, actual=actual, evt=("FLT-44",), ac=("AC04",))
    assert actual == expected


def test_non_timeout_failure_also_latches_without_reclassifying_cause():
    tracker = HeartbeatTracker(scope(), 0.0)
    failure = trigger(TriggerKind.FAILURE_EVENT, 1.0)
    tracker.latch_diagnostic_only(failure)
    assert tracker.observe(scope(), 1, 2.0) is HeartbeatDisposition.FRESH_DIAGNOSTIC_ONLY
    assert tracker.poll(3.0) is None
    assert failure.reason_code == "FAILURE_EVENT"
    assert tracker.diagnostic_only


def test_late_authenticated_observation_is_not_a_backward_receiver_clock():
    tracker = HeartbeatTracker(scope(), 0.0)
    assert tracker.observe(scope(), 1, 2.0) is HeartbeatDisposition.FRESH
    tracker.latch_diagnostic_only(trigger(TriggerKind.FAILURE_EVENT, 1.0))
    assert tracker.observe(scope(), 2, 4.0) is HeartbeatDisposition.FRESH_DIAGNOSTIC_ONLY
    assert tracker.last_received_monotonic == 4.0


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"),
                                  -1.0, True, "10", 10 ** 400])
def test_constructor_rejects_unusable_monotonic(now):
    with pytest.raises(WatchdogError, match="MONOTONIC_INVALID"):
        HeartbeatTracker(scope(), now)


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), 3.999, True, "5"])
def test_receiver_clock_fault_is_sticky_fail_closed(bad_time):
    tracker = HeartbeatTracker(scope(), 2.0)
    assert tracker.observe(scope(), 1, 4.0) is HeartbeatDisposition.FRESH
    with pytest.raises(WatchdogError, match="MONOTONIC_INVALID"):
        tracker.poll(bad_time)
    assert tracker.diagnostic_only
    assert (tracker.last_sequence, tracker.last_received_monotonic) == (1, 4.0)
    with pytest.raises(WatchdogError, match="MONOTONIC_FAILED"):
        tracker.observe(scope(), 2, 5.0)


@pytest.mark.parametrize("expiry", [0, -1, 10.00001, float("inf"), True])
def test_expiry_cannot_be_disabled_or_extended_past_design(expiry):
    with pytest.raises(WatchdogError):
        HeartbeatTracker(scope(), 0.0, expiry_seconds=expiry)


def test_shorter_explicit_deadline_preserves_fail_closed_semantics():
    tracker = HeartbeatTracker(scope(), 0.0, expiry_seconds=0.25)
    assert tracker.poll(0.249) is None
    assert tracker.poll(0.25).kind is TriggerKind.HEARTBEAT_EXPIRED


def test_design_periods_are_declared_not_performance_measurements():
    assert (PRODUCER_INTERVAL_SECONDS, HEARTBEAT_EXPIRY_SECONDS,
            MAX_SUPERVISOR_POLL_SECONDS) == (2.0, 10.0, 0.25)


def test_scope_is_immutable_and_preserves_opaque_ids():
    value = scope(operation_id=" operation-A ", run_id=" run-A ")
    assert value.operation_id == " operation-A "
    assert value.run_id == " run-A "
    with pytest.raises(FrozenInstanceError):
        value.operation_id = "other"


@pytest.mark.parametrize("updates", [
    {"run_id": ""}, {"operation_id": "\ncanary"}, {"producer_id": " "},
    {"producer_instance_id": 0}, {"control_generation": True},
    {"control_generation": 0}, {"control_generation": 9007199254740992},
    {"binding_id": "../other"}, {"supervisor_instance_id": "owner"},
])
def test_invalid_scope_is_fixed_code_and_does_not_echo_input(updates):
    with pytest.raises(WatchdogError) as caught:
        scope(**updates)
    assert str(caught.value) == "INPUT_INVALID"


def test_envelope_scope_copies_values_and_does_not_infer_missing_run():
    value = envelope()
    bound = OperationScope.from_envelope(value, supervisor_instance_id=OWNER)
    value["event"]["operation_id"] = "edited"
    assert bound == scope()
    value["event"]["correlation"] = {}
    with pytest.raises(WatchdogError, match="INPUT_INVALID"):
        OperationScope.from_envelope(value, supervisor_instance_id=OWNER)


def test_explicit_registered_run_supplies_missing_correlation_and_rejects_conflict():
    value = envelope()
    value["event"]["correlation"] = {"task_id": "opaque-task"}
    observed = OperationScope.from_envelope(value, supervisor_instance_id=OWNER, run_id="registered-run")
    assert observed.run_id == "registered-run"
    value["event"]["correlation"]["run_id"] = "registered-run"
    assert OperationScope.from_envelope(value, supervisor_instance_id=OWNER, run_id="registered-run") == observed
    value["event"]["correlation"]["run_id"] = "other-run"
    with pytest.raises(WatchdogError, match="INPUT_INVALID"):
        OperationScope.from_envelope(value, supervisor_instance_id=OWNER, run_id="registered-run")


def test_action_started_sequence_cannot_be_used_as_heartbeat_highwater():
    value = envelope(sequence=999)
    value["event"]["kind"] = "ACTION_STARTED"
    value["profile"]["reason"] = "OPERATION_ARMED"
    tracker = HeartbeatTracker(OperationScope.from_envelope(value, supervisor_instance_id=OWNER), 0.0)
    assert tracker.observe(scope(), 1, 2.0) is HeartbeatDisposition.FRESH
    assert tracker.last_sequence == 1


def test_operation_dedupe_ignores_producer_owner_and_generation(evidence_case):
    once = OncePerOperation()
    original = scope()
    variants = [scope(producer_id="different"), scope(producer_instance_id="different"),
                scope(supervisor_instance_id=OTHER_UUID), scope(control_generation=2),
                scope(binding_id=OTHER_UUID)]
    actual = {"first": once.claim(original),
              "repeated": [once.claim(item) for item in variants],
              "new_operation": once.claim(scope(operation_id="other-operation"))}
    expected = {"first": True, "repeated": [False] * 5, "new_operation": True}
    evidence_case(expected=expected, actual=actual, ac=("AC04",), evt=("WD-04",))
    assert actual == expected
    assert original.dedupe_key == variants[0].dedupe_key == variants[1].dedupe_key
    assert original.dedupe_key == variants[2].dedupe_key
    assert original.dedupe_key != variants[3].dedupe_key
    assert original.operation_key == variants[3].operation_key


def test_dedupe_capacity_never_evicts_old_attempts():
    once = OncePerOperation(max_operations=1)
    assert once.claim(scope())
    with pytest.raises(WatchdogError, match="DEDUPE_CAPACITY"):
        once.claim(scope(operation_id="different"))
    assert not once.claim(scope())


@pytest.mark.parametrize("kind", list(TriggerKind))
def test_observations_require_their_specific_typed_authority(kind):
    value = trigger(kind)
    wrong_source = (ObservationSource.LOCAL_MONOTONIC
                    if value.source is not ObservationSource.LOCAL_MONOTONIC
                    else ObservationSource.AUTHENTICATED_TRANSPORT)
    with pytest.raises(WatchdogError):
        WatchdogTrigger(scope(), kind, 1.0, wrong_source)
    # Plain exception text / free-form string cannot become a trusted cause.
    with pytest.raises(WatchdogError):
        WatchdogTrigger(scope(), kind.value, 1.0, value.source)


@pytest.mark.parametrize("kind", [TriggerKind.CHROME_EXITED, TriggerKind.CDP_DISCONNECTED,
                                   TriggerKind.CLOCK_OR_KEEPER_LOST, TriggerKind.WORKER_FAILED,
                                   TriggerKind.WORKER_DEADLINE])
def test_known_dom_inadmissibility_dominates_same_tick_capture_action(kind):
    events = [trigger(TriggerKind.HEARTBEAT_EXPIRED, 4.0),
              trigger(TriggerKind.CONSUMER_EXITED, 5.0),
              trigger(TriggerKind.FAILURE_EVENT, 6.0), trigger(kind, 7.0)]
    chosen = select_trigger(events)
    assert chosen.kind is kind
    assert chosen.metadata_only
    assert select_trigger(reversed(events)) is chosen
    # Selection doesn't alter chronology or mutate a previously recorded cause.
    assert [event.observed_monotonic for event in events] == [4.0, 5.0, 6.0, 7.0]
    assert events[0].kind is TriggerKind.HEARTBEAT_EXPIRED


def test_failure_then_confirmed_consumer_then_expiry_action_priority():
    expiry = trigger(TriggerKind.HEARTBEAT_EXPIRED)
    consumer = trigger(TriggerKind.CONSUMER_EXITED)
    failure = trigger(TriggerKind.FAILURE_EVENT)
    assert select_trigger([expiry, consumer, failure]) is failure
    assert select_trigger([expiry, consumer]) is consumer
    assert not consumer.metadata_only
    assert select_trigger([]) is None


def test_mixed_scope_priority_and_failure_latch_are_rejected():
    foreign = trigger(TriggerKind.CHROME_EXITED, scoped=scope(operation_id="foreign"))
    with pytest.raises(WatchdogError, match="SCOPE_MISMATCH"):
        select_trigger([trigger(TriggerKind.HEARTBEAT_EXPIRED), foreign])
    tracker = HeartbeatTracker(scope(), 0.0)
    with pytest.raises(WatchdogError, match="SCOPE_MISMATCH"):
        tracker.latch_diagnostic_only(foreign)
    assert not tracker.diagnostic_only
