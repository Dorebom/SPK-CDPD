"""F5 latest-value mailbox controls; local synthetic files/processes only.

L05-R1-F5-01..03: publication, capacity, replacement, restart and loss isolation.
No scheduler, timeout decision, browser, CDP, Worker or operation admission.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from cdpd_chatgpt_profile.failure_hooks import FailureHooks
from cdpd_chatgpt_profile.spool import (
    FileMailbox, HEARTBEAT_MAX_STREAMS, JournalSpool, SpoolError, SpoolLimits,
    canonical_json, payload_sha256, read_journal, strict_json,
)
from test_spool_core import event, fixture_root, run_child


def pulse(sequence=1, **kwargs):
    return event(sequence, kind="HEARTBEAT", reason="HEARTBEAT_SAMPLE", **kwargs)


def latest(root):
    return [strict_json(path.read_bytes(), max_bytes=33024)["envelope"]
            for path in sorted((root / "heartbeat").glob("*.ready.json"))]


def contents(root):
    return sum(path.stat().st_size for path in root.rglob("*")
               if path.is_file() and not path.name.endswith(".lock"))


def record(case, root, name, expected, actual, *, evt=("EVT-03",)):
    (root / (name + ".json")).write_text(json.dumps({
        "verification_ids": ["L05-R1-F5-01", "L05-R1-F5-02", "L05-R1-F5-03"],
        "expected": expected, "actual": actual,
    }, indent=2) + "\n", encoding="utf-8")
    case(expected=expected, actual=actual, ac=("AC02",), evt=evt)
    assert actual == expected


def test_f5_repeated_drain_replaces_latest_and_preserves_memory_coalescing(tmp_path, evidence_case):
    """Without a Supervisor, three publications retain only latest sequence 3."""
    root = fixture_root(tmp_path, "FIX001_F5_repeated_drain")
    limits = SpoolLimits(max_bytes=65536, max_records=3,
                         critical_reserved_bytes=0, critical_reserved_records=1)
    observed = {}
    for mode in ("memory_coalescing", "repeated_drain"):
        spool_root = root / mode
        mailbox = FileMailbox(spool_root, limits=limits)
        hooks = FailureHooks(mailbox.publish, producer_id="f5-producer",
                             producer_instance_id="f5-instance")
        drains = []
        for _ in range(3):
            assert hooks.heartbeat(operation_id=" original-f5-operation ")
            if mode == "repeated_drain":
                drains.append(hooks.drain_once())
        if mode == "memory_coalescing":
            drains.append(hooks.drain_once())
        rows = latest(spool_root)
        observed[mode] = {"drains": drains, "pending": len(rows),
                          "latest_sequence": rows[0]["producer_seq"],
                          "loss_lower_bound": rows[0]["profile"]["loss_lower_bound"],
                          "queue_dropped": hooks.stats["queue_dropped"],
                          "caller_id": rows[0]["event"]["operation_id"],
                          "memory_coalesced": hooks.stats["heartbeat_coalesced"]}
    common = {"pending": 1, "latest_sequence": 3, "loss_lower_bound": 0,
              "queue_dropped": 0, "caller_id": " original-f5-operation "}
    expected = {"memory_coalescing": {**common, "drains": [1], "memory_coalesced": 2},
                "repeated_drain": {**common, "drains": [1, 1, 1], "memory_coalesced": 0}}
    record(evidence_case, root, "RESULT", expected, observed)


def test_f5_normal_record_quota_cannot_consume_liveness_slot(tmp_path, evidence_case):
    """Normal pending records fill their quota; latest liveness still replaces."""
    root = fixture_root(tmp_path, "FIX001_F5_normal_records_full")
    spool_root = root / "spool"
    limits = SpoolLimits(max_bytes=65536, max_records=3,
                         critical_reserved_bytes=0, critical_reserved_records=1)
    mailbox = FileMailbox(spool_root, limits=limits)
    mailbox.publish(event(1))
    mailbox.publish(event(2))
    with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
        mailbox.publish(event(3))
    stages = [mailbox.publish(pulse(seq))["stage"] for seq in (1, 2, 3)]
    critical = event(3, kind="ACTION_FAILED", failure="UNKNOWN", reason="PROVIDER_FAILURE_UNCLASSIFIED")
    critical_stage = mailbox.publish(critical)["stage"]
    record(evidence_case, root, "RESULT",
           {"heartbeat_stages": ["LOCAL_PUBLISHED"] * 3, "pending": 1,
            "latest_sequence": 3, "diagnostic_pending": 3, "critical_stage": "LOCAL_PUBLISHED"},
           {"heartbeat_stages": stages, "pending": len(latest(spool_root)),
            "latest_sequence": latest(spool_root)[0]["producer_seq"],
            "diagnostic_pending": len(list((spool_root / "inbox").glob("*.ready.json"))),
            "critical_stage": critical_stage})


def test_f5_normal_byte_quota_and_actual_global_exhaustion(tmp_path, evidence_case):
    """Global headroom permits replacement; actual total exhaustion preserves old bytes."""
    root = fixture_root(tmp_path, "FIX001_F5_byte_headroom")
    spool_root = root / "spool"
    normal = event(1)
    normal_bytes = len(canonical_json({"payload_sha256": payload_sha256(normal), "envelope": normal}))
    # Two ready path aliases are budgeted by F3, but this F5 control also
    # rejects the next normal write under the older one-copy calculation.
    limits = SpoolLimits(max_bytes=normal_bytes * 6, max_records=20,
                         critical_reserved_bytes=normal_bytes * 4,
                         critical_reserved_records=0)
    mailbox = FileMailbox(spool_root, limits=limits)
    mailbox.publish(normal)
    normal_limit = limits.max_bytes - limits.critical_reserved_bytes
    # Select an existing-data budget boundary independent of F3 implementation.
    filler = spool_root / "quota_fixture.bin"
    filler.write_bytes(b"Q" * (normal_limit - contents(spool_root)))
    before = contents(spool_root)
    with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
        mailbox.publish(event(2))
    observed_peak = []
    mailbox.fault_hook = lambda stage: observed_peak.append(contents(spool_root)) if stage == "heartbeat_before_fsync" else None
    stages = [mailbox.publish(pulse(seq))["stage"] for seq in (1, 2, 3)]
    path = next((spool_root / "heartbeat").glob("*.ready.json"))
    previous = path.read_bytes()
    next_value = pulse(4)
    replacement_bytes = len(canonical_json({"payload_sha256": payload_sha256(next_value), "envelope": next_value}))
    blocked_cap = contents(spool_root) + replacement_bytes - 1
    mailbox.limits = replace(limits, max_bytes=blocked_cap,
                             critical_reserved_bytes=min(limits.critical_reserved_bytes, blocked_cap - 1))
    with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
        mailbox.publish(next_value)
    actual = {"normal_bytes_at_limit": before == normal_limit,
              "stages": stages, "latest_sequence": latest(spool_root)[0]["producer_seq"],
              "old_latest_unchanged_on_exhaustion": path.read_bytes() == previous,
              "all_replacement_peaks_within_global_cap": all(value <= limits.max_bytes for value in observed_peak),
              "replacement_peak_includes_old_and_new": max(observed_peak) > contents(spool_root),
              "temporary_files": len(list(spool_root.rglob("*.tmp")))}
    record(evidence_case, root, "RESULT",
           {"normal_bytes_at_limit": True, "stages": ["LOCAL_PUBLISHED"] * 3,
            "latest_sequence": 3, "old_latest_unchanged_on_exhaustion": True,
            "all_replacement_peaks_within_global_cap": True,
            "replacement_peak_includes_old_and_new": True, "temporary_files": 0}, actual)
    (root / "BYTE_OBSERVATIONS.json").write_text(json.dumps({
        "normal_limit": normal_limit, "global_cap": limits.max_bytes,
        "observed_peaks": observed_peak, "exhausted_cap": blocked_cap,
        "required_replacement_peak": blocked_cap + 1,
    }, indent=2) + "\n")


def test_f5_restart_sequence_conflict_and_loss_snapshot_controls(tmp_path, evidence_case):
    """Restart preserves latest identity/sequence/loss; replacement never repairs a contradiction."""
    root = fixture_root(tmp_path, "FIX001_F5_restart_loss")
    spool_root = root / "spool"
    first = pulse(1, loss=1, exact=False)
    FileMailbox(spool_root).publish(first)
    mailbox = FileMailbox(spool_root)
    newer = pulse(3, loss=2, exact=False)
    mailbox.publish(newer)
    duplicate = mailbox.publish(newer)
    before = {p.name: p.read_bytes() for p in (spool_root / "heartbeat").glob("*.ready.json")}
    rejected = []
    variants = [pulse(2, loss=2, exact=False), pulse(3, loss=2, exact=False),
                pulse(4, loss=1, exact=False), pulse(4, loss=2, exact=True)]
    for value in variants:
        try:
            mailbox.publish(value)
        except SpoolError as exc:
            rejected.append(exc.code)
    after = {p.name: p.read_bytes() for p in (spool_root / "heartbeat").glob("*.ready.json")}
    next_stream = pulse(1, loss=0, exact=True)
    next_stream["event"]["producer_instance_id"] = "new-legitimate-instance"
    mailbox.publish(next_stream)
    persisted = next(value for value in latest(spool_root)
                     if value["event"]["producer_instance_id"] == newer["event"]["producer_instance_id"])
    record(evidence_case, root, "RESULT",
           {"duplicate_stage": "LOCAL_PUBLISHED", "rejected": ["EVENT_ID_CONFLICT", "EVENT_ID_CONFLICT", "INPUT_INVALID", "INPUT_INVALID"],
            "old_stream_unchanged": True, "streams": 2, "old_latest": 3, "old_loss": 2, "old_exact": False},
           {"duplicate_stage": duplicate["stage"], "rejected": rejected,
            "old_stream_unchanged": before == after, "streams": len(latest(spool_root)),
            "old_latest": persisted["producer_seq"], "old_loss": persisted["profile"]["loss_lower_bound"],
            "old_exact": persisted["profile"]["loss_count_exact"]})


def test_f5_finite_stream_slots_do_not_evict_old_latest(tmp_path, evidence_case):
    """An exhausted finite stream table refuses new streams without evicting old ones."""
    root = fixture_root(tmp_path, "FIX001_F5_finite_slots")
    spool_root = root / "spool"
    mailbox = FileMailbox(spool_root)
    for number in range(HEARTBEAT_MAX_STREAMS):
        value = pulse()
        value["event"]["producer_instance_id"] = "f5-instance-" + str(number)
        mailbox.publish(value)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (spool_root / "heartbeat").glob("*.ready.json")}
    refused = pulse()
    refused["event"]["producer_instance_id"] = "f5-overflow-instance"
    with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
        mailbox.publish(refused)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (spool_root / "heartbeat").glob("*.ready.json")}
    record(evidence_case, root, "RESULT", {"slots": 32, "unchanged": True},
           {"slots": len(latest(spool_root)), "unchanged": before == after})


def test_f5_consumer_ack_cannot_unlink_newer_replacement(tmp_path, monkeypatch, evidence_case):
    """A publication between journal append and cleanup survives the old ACK cleanup."""
    root = fixture_root(tmp_path, "FIX001_F5_consumer_race")
    spool_root = root / "spool"
    mailbox = FileMailbox(spool_root)
    mailbox.publish(pulse(1))
    newer = pulse(2)
    with JournalSpool(spool_root) as writer:
        append = writer.append
        def concurrent_publish(value):
            result = append(value)
            if value["producer_seq"] == 1:
                mailbox.publish(newer)
            return result
        monkeypatch.setattr(writer, "append", concurrent_publish)
        first_results = writer.process_pending()
        pending_after_old_ack = latest(spool_root)
        second_results = writer.process_pending()
    rows = read_journal(spool_root)["rows"]
    record(evidence_case, root, "RESULT",
           {"first_ack": ["ACK_DURABLE"], "surviving_pending_seq": [2],
            "second_ack": ["ACK_DURABLE"], "journal_sequences": [1, 2], "remaining_latest": 0},
           {"first_ack": [r["stage"] for r in first_results],
            "surviving_pending_seq": [r["producer_seq"] for r in pending_after_old_ack],
            "second_ack": [r["stage"] for r in second_results],
            "journal_sequences": [r["envelope"]["producer_seq"] for r in rows],
            "remaining_latest": len(latest(spool_root))})


def test_f5_latest_remains_available_when_journal_is_full(tmp_path, evidence_case):
    """A stalled/full journal cannot consume or freeze the producer's latest sample."""
    root = fixture_root(tmp_path, "FIX001_F5_journal_backpressure")
    spool_root = root / "spool"
    limits = SpoolLimits(max_bytes=65536, max_records=2,
                         critical_reserved_bytes=0, critical_reserved_records=0)
    mailbox = FileMailbox(spool_root, limits=limits)
    with JournalSpool(spool_root, limits=limits) as writer:
        writer.append(event(1))
        writer.append(event(2))
        observed = []
        for sequence in (1, 2, 3):
            mailbox.publish(pulse(sequence))
            receipts = writer.process_pending()
            observed.append({"result": receipts[0]["stage"], "code": receipts[0]["failure_code"],
                             "latest_seq": latest(spool_root)[0]["producer_seq"],
                             "latest_count": len(latest(spool_root))})
        dropped = writer.stats["spool_dropped"]
    record(evidence_case, root, "RESULT",
           {"observations": [{"result": "REJECTED", "code": "SPOOL_BACKPRESSURE",
                              "latest_seq": seq, "latest_count": 1} for seq in (1, 2, 3)],
            "diagnostic_drops": 0, "journal_rows": 2},
           {"observations": observed, "diagnostic_drops": dropped,
            "journal_rows": len(read_journal(spool_root)["rows"])})


@pytest.mark.parametrize("fault_stage,expected_latest", [
    ("heartbeat_after_fsync", 1), ("heartbeat_after_publish", 2),
])
def test_f5_atomic_replacement_process_crash(tmp_path, evidence_case, fault_stage, expected_latest):
    """Actual exit73 before/after replacement yields complete old/new bytes and no ACK."""
    root = fixture_root(tmp_path, "FIX001_F5_crash_" + fault_stage)
    spool_root = root / "spool"
    FileMailbox(spool_root).publish(pulse(1))
    next_value = pulse(2)
    (root / "next_envelope.json").write_bytes(canonical_json(next_value))
    child = run_child(root, '''
import json, os, sys
from pathlib import Path
from cdpd_chatgpt_profile.spool import FileMailbox
root, source, phase = sys.argv[1:]
def fault(stage):
    if stage == phase:
        os._exit(73)
FileMailbox(root, fault_hook=fault).publish(json.loads(Path(source).read_bytes()))
''', str(spool_root), str(root / "next_envelope.json"), fault_stage,
                      name="heartbeat_crash")
    observed = latest(spool_root)
    residue = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in spool_root.rglob("*.tmp")}
    # Restart publishes a strictly later sample without deleting crash residue.
    FileMailbox(spool_root).publish(pulse(3))
    residue_after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in spool_root.rglob("*.tmp")}
    record(evidence_case, root, "RESULT",
           {"exit": 73, "visible_latest_after_crash": expected_latest,
            "latest_after_restart": 3, "pending_after_restart": 1,
            "residue_preserved": True, "receipts": 0, "journal_rows": 0},
           {"exit": child.returncode, "visible_latest_after_crash": observed[0]["producer_seq"],
            "latest_after_restart": latest(spool_root)[0]["producer_seq"],
            "pending_after_restart": len(latest(spool_root)),
            "residue_preserved": residue_after == residue,
            "receipts": len(list((spool_root / "receipts").glob("*.json"))),
            "journal_rows": len(read_journal(spool_root)["rows"])}, evt=("EVT-02", "EVT-03"))
