"""EXECUTION regression probes for approved L05/EVT-01..05 edge contracts.

No browser, CDP connection, Web operation or independent Assessment is performed.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from cdpd import DiagnosticEvent
from cdpd_chatgpt_profile.failure_hooks import BindingContext, FailureHooks
from cdpd_chatgpt_profile import spool as spool_module
from cdpd_chatgpt_profile.spool import (
    FileMailbox,
    JournalSpool,
    SpoolLimits,
    canonical_json,
    read_history,
    read_journal,
    stream_key,
    verify_receipt,
)


def envelope(sequence=1, *, event_id=None, binding=None, kind="ACTION_FINISHED",
             reason="OPERATION_FINISHED", loss=0, exact=True):
    return {
        "schema": "cdpd.chatgpt-event-envelope/v1",
        "event_id": event_id or str(uuid4()),
        "producer_seq": sequence,
        "occurred_at_utc": "2026-10-24T00:00:00Z",
        "binding_id": binding,
        "binding_sha256": "a" * 64 if binding else None,
        "control_generation": 1 if binding else 0,
        "event": {"schema": "cdpd.event/v1", "producer_id": "contract-test",
                  "producer_instance_id": "contract-instance",
                  "operation_id": "caller-operation", "kind": kind,
                  "failure_code": "NONE", "correlation": {"trace": "opaque-42"}},
        "profile": {"reason": reason, "dispatch_state": "UNKNOWN",
                    "ui_context": "UNDETERMINED", "document_marker": None,
                    "loss_lower_bound": loss, "loss_count_exact": exact},
    }


def checkpoint(value, *, clean=True, high_water=None):
    event = value["event"]
    return {
        "schema": "cdpd.producer-checkpoint/v1", "producer_id": event["producer_id"],
        "producer_instance_id": event["producer_instance_id"],
        "binding_id": value["binding_id"], "binding_sha256": value["binding_sha256"],
        "control_generation": value["control_generation"],
        "diagnostic_high_water": value["producer_seq"] if high_water is None else high_water,
        "liveness_high_water": 0,
        "loss_lower_bound": value["profile"]["loss_lower_bound"],
        "loss_count_exact": value["profile"]["loss_count_exact"],
        "clean_shutdown": clean,
    }


def test_receipt_boolean_journal_sequence_is_rejected(tmp_path, evidence_case):
    """L02 receipt integer contract: Python True==1 cannot validate a receipt."""
    value = envelope()
    with JournalSpool(tmp_path) as writer:
        receipt = writer.append(value)
    row = read_journal(tmp_path)["rows"][0]
    assert verify_receipt(receipt, value, row)
    forged = {**receipt, "journal_seq": True}
    actual = verify_receipt(forged, value, row)
    evidence_case(expected={"valid_ack": True, "boolean_sequence_accepted": False},
                  actual={"valid_ack": verify_receipt(receipt, value, row), "boolean_sequence_accepted": actual},
                  ac=("AC02",), evt=("EVT-05",))
    assert actual is False


def test_actual_journal_fsync_failure_never_publishes_ack(tmp_path, monkeypatch, evidence_case):
    """Intercept the actual file descriptor sync, not a claimed fsync flag."""
    value = envelope()
    writer = JournalSpool(tmp_path)
    original_fsync = os.fsync
    journal_fd = writer._handle.fileno()
    observed = []

    def fail_journal(descriptor):
        observed.append(descriptor)
        if descriptor == journal_fd:
            raise OSError("synthetic fsync failure; must not become stored metadata")
        return original_fsync(descriptor)

    monkeypatch.setattr(spool_module.os, "fsync", fail_journal)
    try:
        with pytest.raises(OSError):
            writer.append(value)
        evidence_case(expected={"actual_journal_sync_attempted": True, "ack_published": False},
                      actual={"actual_journal_sync_attempted": journal_fd in observed,
                              "ack_published": (tmp_path / "receipts" / (value["event_id"] + ".json")).exists()},
                      ac=("AC02",), evt=("EVT-02",))
        assert journal_fd in observed
        assert not (tmp_path / "receipts" / (value["event_id"] + ".json")).exists()
    finally:
        monkeypatch.setattr(spool_module.os, "fsync", original_fsync)
        writer.close()
    assert "synthetic fsync failure" not in b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()).decode("utf-8", errors="replace")


def test_ack_publication_follows_actual_journal_sync(tmp_path, monkeypatch, evidence_case):
    value = envelope()
    phases = []
    original_fsync = os.fsync
    writer = JournalSpool(tmp_path, fault_hook=lambda stage: phases.append(stage))
    journal_fd = writer._handle.fileno()

    def track_sync(descriptor):
        original_fsync(descriptor)
        if descriptor == journal_fd:
            phases.append("actual_journal_fsync_returned")

    monkeypatch.setattr(spool_module.os, "fsync", track_sync)
    try:
        receipt = writer.append(value)
        evidence_case(expected={"stage": "ACK_DURABLE", "journal_sync_before_receipt_write": True,
                                "receipt_sync_before_publication": True},
                      actual={"stage": receipt["stage"],
                              "journal_sync_before_receipt_write": phases.index("actual_journal_fsync_returned") < phases.index("receipt_before_flush"),
                              "receipt_sync_before_publication": phases.index("receipt_after_fsync") < phases.index("receipt_after_publish")},
                      ac=("AC01", "AC02"), evt=("EVT-01", "EVT-02"))
        assert receipt["stage"] == "ACK_DURABLE"
        assert phases.index("actual_journal_fsync_returned") < phases.index("receipt_before_flush")
        assert phases.index("receipt_after_fsync") < phases.index("receipt_after_publish")
    finally:
        writer.close()


def test_sequence_conflict_across_binding_change_survives_restart(tmp_path, evidence_case):
    first = envelope(binding=str(uuid4()))
    with JournalSpool(tmp_path) as writer:
        original_ack = writer.append(first)
    with JournalSpool(tmp_path) as writer:
        replay = writer.append(first)
        assert replay == original_ack
        conflicting = envelope(binding=str(uuid4()))
        rejection = writer.append(conflicting)
        assert rejection["stage"] == "REJECTED"
        assert rejection["failure_code"] == "EVENT_ID_CONFLICT"
    count = len(read_journal(tmp_path)["rows"])
    evidence_case(expected={"duplicate_same_receipt": True, "cross_binding_sequence_conflict": "EVENT_ID_CONFLICT", "rows": 1},
                  actual={"duplicate_same_receipt": replay == original_ack,
                          "cross_binding_sequence_conflict": rejection["failure_code"], "rows": count},
                  ac=("AC02",), evt=("EVT-05",))
    assert count == 1


def test_retirement_and_restart_do_not_reset_journal_sequence(tmp_path, evidence_case):
    """A retained durable current high-water still applies with zero live rows."""
    value = envelope()
    mailbox = FileMailbox(tmp_path)
    with JournalSpool(tmp_path, wall_time=lambda: 1000.0) as writer:
        first_ack = writer.append(value)
    mailbox.publish_checkpoint(checkpoint(value))
    with JournalSpool(tmp_path, wall_time=lambda: 16000.0) as writer:
        result = writer.prune_expired(now_utc=16000.0)
        assert result["records_removed"] == 1
    with JournalSpool(tmp_path, wall_time=lambda: 16001.0) as writer:
        next_ack = writer.append(envelope(2))
        evidence_case(expected={"retired_records": 1, "first_journal_seq": 1, "next_journal_seq": 2},
                      actual={"retired_records": result["records_removed"], "first_journal_seq": first_ack["journal_seq"],
                              "next_journal_seq": next_ack["journal_seq"]},
                      ac=("AC02",), evt=("EVT-05",))
        assert next_ack["journal_seq"] > first_ack["journal_seq"]


def test_duplicate_rejected_event_is_not_counted_as_two_known_losses(tmp_path, evidence_case):
    limits = SpoolLimits(max_records=2, critical_reserved_records=0)
    missing = envelope(3)
    with JournalSpool(tmp_path, limits=limits) as writer:
        assert writer.append(envelope(1))["stage"] == "ACK_DURABLE"
        assert writer.append(envelope(2))["stage"] == "ACK_DURABLE"
        first = writer.append(missing)
        second = writer.append(missing)
        assert first["failure_code"] == second["failure_code"] == "SPOOL_BACKPRESSURE"
        history = read_history(tmp_path, stream=stream_key(missing), now_utc=1792800001.0)
        evidence_case(expected={"two_delivery_results": ["SPOOL_BACKPRESSURE"] * 2, "distinct_event_loss": 1},
                      actual={"two_delivery_results": [first["failure_code"], second["failure_code"]],
                              "distinct_event_loss": history["history_dropped_count"]},
                      ac=("AC02",), evt=("EVT-03", "EVT-05"))
        assert history["history_dropped_count"] == 1


def test_lock_contention_unknown_reaches_old_stream_after_binding_change(evidence_case):
    hooks = FailureHooks(lambda value: {"stage": "LOCAL_PUBLISHED", "ack_purpose": "EVENT_DURABILITY"},
                         producer_id="contract-test", producer_instance_id="contract-instance")
    event = DiagnosticEvent("contract-test", "contract-instance", "caller-operation", "ACTION_FINISHED")
    assert hooks.try_emit(event)
    hooks._lock.acquire()
    try:
        assert hooks.try_emit(event) is False
    finally:
        hooks._lock.release()
    hooks.set_binding(BindingContext(str(uuid4()), "b" * 64, 1))
    assert hooks.try_emit(event)
    hooks.set_binding(BindingContext())
    history = hooks.history()
    evidence_case(expected={"old_stream_exact_after_contention_and_roundtrip": False},
                  actual={"old_stream_exact_after_contention_and_roundtrip": history["history_coverage"]["loss_count_exact"]},
                  ac=("AC02", "AC03"), evt=("EVT-03",))
    assert history["history_coverage"]["loss_count_exact"] is False


def test_corrupt_prefix_does_not_hide_new_segment_or_reset_unknown(tmp_path, evidence_case):
    first = envelope(1)
    with JournalSpool(tmp_path) as writer:
        writer.append(first)
    segment = next((tmp_path / "journal").glob("*.jsonl"))
    original = segment.read_bytes()
    with segment.open("ab") as handle:
        handle.write(b'{"torn":')
        handle.flush()
        os.fsync(handle.fileno())
    second = envelope(2)
    with JournalSpool(tmp_path) as writer:
        assert writer.append(second)["stage"] == "ACK_DURABLE"
    result = read_journal(tmp_path)
    assert [row["event_id"] for row in result["rows"]] == [first["event_id"], second["event_id"]]
    assert result["faults"]
    assert segment.read_bytes() == original + b'{"torn":'
    FileMailbox(tmp_path).publish_checkpoint(checkpoint(second))
    history = read_history(tmp_path, stream=stream_key(first), now_utc=1792800001.0)
    evidence_case(expected={"valid_rows": 2, "corruption_detected": True, "original_bytes_preserved": True,
                            "complete": False, "loss_count_exact": False},
                  actual={"valid_rows": len(result["rows"]), "corruption_detected": bool(result["faults"]),
                          "original_bytes_preserved": segment.read_bytes() == original + b'{"torn":',
                          "complete": history["history_coverage"]["complete"],
                          "loss_count_exact": history["history_coverage"]["loss_count_exact"]},
                  ac=("AC02",), evt=("EVT-02",))
    assert history["history_coverage"]["complete"] is False
    assert history["history_coverage"]["loss_count_exact"] is False


def test_loss_unknown_survives_window_motion_and_heartbeat(tmp_path, evidence_case):
    value = envelope(1, loss=1, exact=False)
    heartbeat = envelope(40, kind="HEARTBEAT", reason="HEARTBEAT_SAMPLE", loss=1, exact=False)
    with JournalSpool(tmp_path) as writer:
        writer.append(value)
        writer.append(heartbeat)
    FileMailbox(tmp_path).publish_checkpoint(checkpoint(value))
    history = read_history(tmp_path, stream=stream_key(value), now_utc=1892800001.0)
    evidence_case(expected={"retained_events": 0, "lower_bound": 1, "exact": False, "complete": False},
                  actual={"retained_events": len(history["events"]), "lower_bound": history["history_dropped_count"],
                          "exact": history["history_coverage"]["loss_count_exact"],
                          "complete": history["history_coverage"]["complete"]},
                  ac=("AC02",), evt=("EVT-03",))
    assert history["events"] == []
    assert history["history_dropped_count"] == 1
    assert history["history_coverage"]["loss_count_exact"] is False
    assert history["history_coverage"]["complete"] is False


def test_hash_valid_regressing_loss_is_detected_on_readback(tmp_path, evidence_case):
    """Per-row schema/hash success cannot repair contradictory stream history."""
    with JournalSpool(tmp_path) as writer:
        writer.append(envelope(1, loss=1))
        writer.append(envelope(2, loss=1))
    segment = next((tmp_path / "journal").glob("*.jsonl"))
    rows = [json.loads(line) for line in segment.read_bytes().splitlines()]
    rows[1]["envelope"]["profile"]["loss_lower_bound"] = 0
    rows[1]["payload_sha256"] = hashlib.sha256(canonical_json(rows[1]["envelope"])).hexdigest()
    rows[1]["record_sha256"] = hashlib.sha256(canonical_json({k: v for k, v in rows[1].items() if k != "record_sha256"})).hexdigest()
    segment.write_bytes(b"".join(canonical_json(row) + b"\n" for row in rows))
    recovered = read_journal(tmp_path)
    evidence_case(expected={"hash_valid_loss_regression_detected": True},
                  actual={"hash_valid_loss_regression_detected": bool(recovered["faults"])},
                  ac=("AC02",), evt=("EVT-03",))
    assert recovered["faults"], "Hash-valid contradictory loss snapshots require explicit readback rejection/fault"


def test_loss_plus_count_truncation_is_not_an_understated_exact_total(tmp_path, evidence_case):
    """Known dropped seq2 and omitted persisted seq1 are separate omissions."""
    first, third = envelope(1), envelope(3, loss=1)
    with JournalSpool(tmp_path) as writer:
        writer.append(first)
        writer.append(third)
    FileMailbox(tmp_path).publish_checkpoint(checkpoint(third))
    # Use a timestamp matching the fixed fixture; count truncation, not age,
    # removes seq1. UTC is a window selector, never ordering or loss reset.
    from datetime import datetime, timezone
    now = datetime(2026, 10, 24, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    history = read_history(tmp_path, stream=stream_key(third), now_utc=now, max_events=1)
    evidence_case(expected={"retained_events": 1, "omitted_by_limit": 1, "lower_bound": 1,
                            "exact": False, "sampled": True, "complete": False},
                  actual={"retained_events": len(history["events"]), "omitted_by_limit": history["omitted_by_limit"],
                          "lower_bound": history["history_dropped_count"],
                          "exact": history["history_coverage"]["loss_count_exact"],
                          "sampled": history["history_coverage"]["sampled"],
                          "complete": history["history_coverage"]["complete"]},
                  ac=("AC02",), evt=("EVT-03",))
    assert history["omitted_by_limit"] == 1
    assert history["history_coverage"]["sampled"] is True
    assert history["history_coverage"]["complete"] is False
    assert (history["history_coverage"]["loss_count_exact"] is False or
            history["history_dropped_count"] >= 2)


@pytest.mark.parametrize("replacement", ["OPERATION_ADMISSION", "CAPTURE_RESERVATION"])
def test_event_durability_ack_cannot_be_relabelled_as_admission(tmp_path, replacement, evidence_case):
    value = envelope()
    with JournalSpool(tmp_path) as writer:
        receipt = writer.append(value)
    row = read_journal(tmp_path)["rows"][0]
    altered = deepcopy(receipt)
    altered["ack_purpose"] = replacement
    actual = verify_receipt(altered, value, row)
    evidence_case(expected={"original_purpose": "EVENT_DURABILITY", "altered_purpose": replacement,
                            "altered_receipt_accepted": False},
                  actual={"original_purpose": receipt["ack_purpose"], "altered_purpose": replacement,
                          "altered_receipt_accepted": actual},
                  ac=("AC02",), evt=("EVT-01",))
    assert actual is False
