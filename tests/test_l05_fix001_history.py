"""L05 R1 F1/F2 closure controls; synthetic bytes and local child processes only."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from cdpd import DiagnosticEvent
from cdpd_chatgpt_profile.failure_hooks import BindingContext, FailureHooks
from cdpd_chatgpt_profile.spool import (
    FileMailbox, JournalSpool, canonical_json, payload_sha256, read_history,
    read_journal, stream_key,
)
from test_spool_core import NOW, checkpoint, event, fixture_root, run_child


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


def rehash(value):
    value["record_sha256"] = hashlib.sha256(canonical_json({key: item for key, item in value.items() if key != "record_sha256"})).hexdigest()


def observe(case, label, root, value):
    journal = read_journal(root)
    history = read_history(root, stream=stream_key(value), now_utc=NOW)
    save(case / label / "journal.json", journal)
    save(case / label / "history.json", history)
    return journal, history


def two_segments(root):
    first, second = event(1), event(2)
    with JournalSpool(root) as writer:
        assert writer.append(first)["stage"] == "ACK_DURABLE"
    with JournalSpool(root) as writer:
        assert writer.append(second)["stage"] == "ACK_DURABLE"
    FileMailbox(root).publish_checkpoint(checkpoint(second, high_water=2))
    return first, second


@pytest.mark.parametrize("fault", ["missing_segment", "all_segments_missing", "shrunk_segment", "missing_retained_id", "changed_retained_payload", "missing_high_water", "missing_journal_cutoff"])
def test_fix001_current_reconciles_durable_projection_and_keeps_fault_after_recovery(tmp_path, evidence_case, fault):
    """L05-R1-F1-01/02/03: normal current control, injected discrepancy, sticky recovery."""
    case = fixture_root(tmp_path, "FIX001_F1_" + fault)
    root = case / "runtime"
    first, second = two_segments(root)
    normal, normal_history = observe(case, "normal", root, second)
    assert not normal["faults"] and normal_history["history_coverage"]["complete"]
    shutil.copytree(root, case / "original_bytes")
    target = sorted((root / "journal").glob("*.jsonl"))[0]
    if fault == "missing_segment":
        target.unlink()
    elif fault == "all_segments_missing":
        for segment in (root / "journal").glob("*.jsonl"):
            segment.unlink()
    elif fault == "shrunk_segment":
        target.write_bytes(b"")
    elif fault in ("missing_retained_id", "changed_retained_payload"):
        row = json.loads(target.read_bytes())
        if fault == "missing_retained_id":
            row["event_id"] = row["envelope"]["event_id"] = str(uuid4())
        else:
            row["envelope"]["event"]["correlation"]["task_id"] = "opaque-task-002"
        row["payload_sha256"] = payload_sha256(row["envelope"])
        rehash(row)
        target.write_bytes(canonical_json(row) + b"\n")
    else:
        current = json.loads((root / "current.json").read_bytes())
        if fault == "missing_journal_cutoff":
            current["last_journal_seq"] += 1
        else:
            current["producer_high_water"][0]["sequence"] += 1
        rehash(current)
        save(root / "current.json", current)
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (root / "journal").glob("*.jsonl")}
    damaged, damaged_history = observe(case, "damaged", root, second)
    assert damaged["faults"]
    assert damaged_history["history_coverage"]["complete"] is False
    assert damaged_history["history_coverage"]["loss_count_exact"] is False
    missing = fault in ("missing_segment", "all_segments_missing", "shrunk_segment", "missing_retained_id")
    missing_count = 2 if fault == "all_segments_missing" else int(missing)
    if missing:
        assert damaged_history["missing_durable_event_lower_bound"] == missing_count
        assert damaged_history["history_dropped_count"] >= missing_count
    with JournalSpool(root):
        pass
    recovered, recovered_history = observe(case, "recovered", root, second)
    assert recovered["faults"]
    assert recovered_history["history_coverage"]["complete"] is False
    assert recovered_history["history_coverage"]["loss_count_exact"] is False
    assert all(hashlib.sha256((root / "journal" / name).read_bytes()).hexdigest() == digest for name, digest in before.items())
    if missing:
        assert recovered_history["missing_durable_event_lower_bound"] == missing_count
    actual = {"normal_complete": normal_history["history_coverage"]["complete"],
              "fault_detected": bool(damaged["faults"]), "recovered_fault": bool(recovered["faults"]),
              "recovered_complete": recovered_history["history_coverage"]["complete"],
              "recovered_exact": recovered_history["history_coverage"]["loss_count_exact"],
              "original_damaged_journal_bytes_unchanged": True,
              "known_missing_lower": recovered_history["missing_durable_event_lower_bound"]}
    expected = {"normal_complete": True, "fault_detected": True, "recovered_fault": True,
                "recovered_complete": False, "recovered_exact": False,
                "original_damaged_journal_bytes_unchanged": True, "known_missing_lower": missing_count}
    save(case / "expected_actual.json", {"expected": expected, "actual": actual})
    evidence_case(expected=expected, actual=actual, ac=("AC02",), evt=("EVT-02",))


def test_fix001_current_lag_after_valid_append_is_not_journal_loss(tmp_path, evidence_case):
    """L05-R1-F1-01/03: a valid append can be ahead of current's saved prefix."""
    case = fixture_root(tmp_path, "FIX001_F1_append_ahead")
    root = case / "runtime"
    first, second = event(1), event(2)
    with JournalSpool(root) as writer:
        writer.append(first)
        old_current = (root / "current.json").read_bytes()
        writer.append(second)
    FileMailbox(root).publish_checkpoint(checkpoint(second, high_water=2))
    (case / "saved_current_prefix.json").write_bytes(old_current)
    (root / "current.json").write_bytes(old_current)
    journal, history = observe(case, "append_ahead", root, second)
    assert journal["faults"] == []
    assert len(journal["rows"]) == 2
    assert history["history_coverage"]["complete"] is True
    evidence_case(expected={"rows": 2, "faults": 0, "complete": True},
                  actual={"rows": len(journal["rows"]), "faults": len(journal["faults"]), "complete": history["history_coverage"]["complete"]},
                  ac=("AC02",), evt=("EVT-02",))


def test_fix001_legacy_current_preserves_readability_and_unknown_missing_attribution(tmp_path, evidence_case):
    """L05-R1-F1-01/02: v1 retained IDs detect loss without inventing a per-stream identity mapping."""
    case = fixture_root(tmp_path, "FIX001_F1_legacy_current")
    root = case / "runtime"
    first, second = two_segments(root)
    current = json.loads((root / "current.json").read_bytes())
    # A v1 document has the exact original schema/field shape. It is a
    # synthetic compatibility control, not an edit to accepted source bytes.
    current = {key: item for key, item in current.items() if key in {
        "schema", "clean_shutdown", "last_journal_seq", "stats", "journal_sizes",
        "retained_event_ids", "dropped_event_ids", "producer_high_water",
        "retired_high_water", "uncertain_streams", "stream_losses", "record_sha256"}}
    current["schema"] = "cdpd.spool-current/v1"
    rehash(current)
    save(root / "current.json", current)
    normal, normal_history = observe(case, "legacy_normal", root, second)
    assert normal["faults"] == [] and normal_history["history_coverage"]["complete"]
    shutil.copytree(root, case / "legacy_original_bytes")
    sorted((root / "journal").glob("*.jsonl"))[0].unlink()
    damaged, damaged_history = observe(case, "legacy_missing", root, second)
    assert damaged["faults"] and not damaged_history["history_coverage"]["loss_count_exact"]
    assert damaged_history["missing_durable_event_lower_bound"] == 0
    with JournalSpool(root):
        pass
    _, recovered = observe(case, "legacy_recovered", root, second)
    assert not recovered["history_coverage"]["complete"]
    assert not recovered["history_coverage"]["loss_count_exact"]
    assert recovered["missing_durable_event_lower_bound"] == 0
    evidence_case(expected={"legacy_normal_complete": True, "missing_detected": True,
                            "recovered_complete": False, "recovered_exact": False, "unattributed_loss_not_guessed": 0},
                  actual={"legacy_normal_complete": normal_history["history_coverage"]["complete"],
                          "missing_detected": bool(damaged["faults"]), "recovered_complete": recovered["history_coverage"]["complete"],
                          "recovered_exact": recovered["history_coverage"]["loss_count_exact"], "unattributed_loss_not_guessed": recovered["missing_durable_event_lower_bound"]},
                  ac=("AC02",), evt=("EVT-02",))


@pytest.mark.parametrize("crash_stage", [None, "prune_after_intent", "prune_after_unlink"])
def test_fix001_retention_intent_distinguishes_authorized_prune_and_process_crash(tmp_path, evidence_case, crash_stage):
    """L05-R1-F1-03: recorded retirement intent precedes deletion; missing intended bytes are not unexplained loss."""
    case = fixture_root(tmp_path, "FIX001_F1_prune_" + (crash_stage or "normal"))
    root = case / "runtime"
    value = event(1)
    with JournalSpool(root, wall_time=lambda: 1000.0) as writer:
        writer.append(value)
    FileMailbox(root).publish_checkpoint(checkpoint(value, high_water=1))
    shutil.copytree(root, case / "original_bytes")
    exit_code = 0
    if crash_stage:
        source = """import os,sys
from cdpd_chatgpt_profile.spool import JournalSpool
def fault(stage):
    if stage == sys.argv[2]:
        os.write(1, ('FAULT:'+stage+'\\n').encode())
        os._exit(74)
with JournalSpool(sys.argv[1],wall_time=lambda:16000.0,fault_hook=fault) as writer:
    writer.prune_expired(now_utc=16000.0)
raise SystemExit(99)
"""
        child = run_child(case, source, str(root), crash_stage, name="prune_crash")
        exit_code = child.returncode
        assert exit_code == 74
    else:
        with JournalSpool(root, wall_time=lambda: 16000.0) as writer:
            assert writer.prune_expired(now_utc=16000.0)["records_removed"] == 1
    journal, history = observe(case, "after_prune", root, value)
    assert journal["faults"] == []
    assert journal["current"]["retired_journal_seq"] == 1
    with JournalSpool(root, wall_time=lambda: 16001.0):
        pass
    recovered, _ = observe(case, "recovered", root, value)
    assert recovered["faults"] == []
    expected = {"child_exit": 74 if crash_stage else 0, "unexpected_faults": 0, "recovered_faults": 0}
    actual = {"child_exit": exit_code, "unexpected_faults": len(journal["faults"]), "recovered_faults": len(recovered["faults"])}
    save(case / "expected_actual.json", {"expected": expected, "actual": actual})
    evidence_case(expected=expected, actual=actual, ac=("AC02",), evt=("EVT-02", "EVT-03"),
                  commands=("local os._exit(74); see prune_crash_command.json",) if crash_stage else ())


@pytest.mark.parametrize("new_mailbox", [False, True])
def test_fix001_consistent_dirty_to_clean_checkpoint_is_complete(tmp_path, evidence_case, new_mailbox):
    """L05-R1-F2-01/02/03: clean completion is permitted when all retained snapshots agree."""
    case = fixture_root(tmp_path, "FIX001_F2_consistent_" + str(new_mailbox))
    root = case / "runtime"
    value = event(1)
    with JournalSpool(root) as writer:
        writer.append(value)
    mailbox = FileMailbox(root)
    before = mailbox.publish_checkpoint(checkpoint(value, high_water=1, clean=False))
    if new_mailbox:
        mailbox = FileMailbox(root)
    after = mailbox.publish_checkpoint(checkpoint(value, high_water=1, clean=True))
    journal, history = observe(case, "clean", root, value)
    assert after["checkpoint_seq"] > before["checkpoint_seq"]
    assert not journal["faults"] and history["history_coverage"]["complete"]
    evidence_case(expected={"ordered_across_mailboxes": True, "faults": 0, "complete": True},
                  actual={"ordered_across_mailboxes": after["checkpoint_seq"] > before["checkpoint_seq"], "faults": len(journal["faults"]), "complete": history["history_coverage"]["complete"]},
                  ac=("AC02",), evt=("EVT-02", "EVT-03"))


@pytest.mark.parametrize("regression", ["diagnostic_high_water", "liveness_high_water", "loss_lower_bound", "loss_count_exact"])
@pytest.mark.parametrize("new_mailbox", [False, True])
def test_fix001_checkpoint_regressions_cannot_be_cleared_by_clean_or_restart(tmp_path, evidence_case, regression, new_mailbox):
    """L05-R1-F2-01/02/03: contradictory checkpoints remain raw, faulted and inexact after recovery."""
    case = fixture_root(tmp_path, "FIX001_F2_" + regression + "_" + str(new_mailbox))
    root = case / "runtime"
    value = event(1)
    with JournalSpool(root) as writer:
        writer.append(value)
    mailbox = FileMailbox(root)
    mailbox.publish_checkpoint(checkpoint(value, high_water=1, clean=False))
    mailbox.publish_checkpoint(checkpoint(value, high_water=1))
    _, normal = observe(case, "normal", root, value)
    assert normal["history_coverage"]["complete"]
    before = checkpoint(value, high_water=1, clean=False)
    after = checkpoint(value, high_water=1, clean=True)
    if regression == "loss_count_exact":
        before[regression] = False
    elif regression == "loss_lower_bound":
        before.update(diagnostic_high_water=3, loss_lower_bound=2)
        after.update(diagnostic_high_water=3, loss_lower_bound=1)
    else:
        before[regression] = 2
        after[regression] = 1
    mailbox.publish_checkpoint(before)
    observe(case, "dirty", root, value)
    if new_mailbox:
        mailbox = FileMailbox(root)
    mailbox.publish_checkpoint(after)
    original = {path.name: path.read_bytes() for path in (root / "checkpoints").glob("*.json")}
    journal, history = observe(case, "regressed", root, value)
    assert journal["faults"]
    assert history["history_coverage"]["complete"] is False
    assert history["history_coverage"]["loss_count_exact"] is False
    with JournalSpool(root):
        pass
    recovered, recovered_history = observe(case, "recovered", root, value)
    assert recovered_history["history_coverage"]["complete"] is False
    assert recovered_history["history_coverage"]["loss_count_exact"] is False
    assert all((root / "checkpoints" / name).read_bytes() == content for name, content in original.items())
    if regression == "loss_lower_bound":
        assert recovered_history["history_dropped_count"] >= 2
    expected = {"normal_complete": True, "fault_detected": True, "recovered_complete": False, "recovered_exact": False, "raw_checkpoints_unchanged": True}
    actual = {"normal_complete": normal["history_coverage"]["complete"], "fault_detected": bool(journal["faults"]),
              "recovered_complete": recovered_history["history_coverage"]["complete"], "recovered_exact": recovered_history["history_coverage"]["loss_count_exact"], "raw_checkpoints_unchanged": True}
    save(case / "expected_actual.json", {"expected": expected, "actual": actual})
    evidence_case(expected=expected, actual=actual, ac=("AC02",), evt=("EVT-02", "EVT-03"))


@pytest.mark.parametrize("boundary", ["new_instance", "new_binding", "new_generation"])
def test_fix001_legitimate_stream_change_does_not_inherit_old_loss(tmp_path, evidence_case, boundary):
    """L05-R1-F2-02: loss belongs to the exact stream; Binding changes retain instance sequence order."""
    case = fixture_root(tmp_path, "FIX001_F2_boundary_" + boundary)
    root = case / "runtime"
    old = event(1, loss=2, exact=False)
    new = event(2)
    if boundary == "new_instance":
        new["event"]["producer_instance_id"] = "l05-instance-002"
        new["producer_seq"] = 1
    elif boundary == "new_binding":
        new["binding_id"] = str(uuid4())
        new["binding_sha256"] = "b" * 64
        new["control_generation"] = 2
    else:
        new["control_generation"] = 2
    with JournalSpool(root) as writer:
        writer.append(old)
        writer.append(new)
    mailbox = FileMailbox(root)
    mailbox.publish_checkpoint(checkpoint(old, high_water=1, loss=2, exact=False))
    FileMailbox(root).publish_checkpoint(checkpoint(new, high_water=new["producer_seq"]))
    journal, history = observe(case, "new_stream", root, new)
    _, old_history = observe(case, "old_stream", root, old)
    assert journal["faults"] == []
    assert history["history_coverage"]["complete"] is True
    assert history["history_dropped_count"] == 0
    assert old_history["history_coverage"]["loss_count_exact"] is False
    evidence_case(expected={"faults": 0, "new_complete": True, "new_loss": 0, "old_exact": False},
                  actual={"faults": len(journal["faults"]), "new_complete": history["history_coverage"]["complete"],
                          "new_loss": history["history_dropped_count"], "old_exact": old_history["history_coverage"]["loss_count_exact"]},
                  ac=("AC02",), evt=("EVT-02", "EVT-03"))


def test_fix001_duplicate_checkpoint_position_and_legacy_order_are_unknown(tmp_path, evidence_case):
    """L05-R1-F2-01/03: sequence conflicts and legacy unordered mailboxes are not silently sorted by pathname."""
    case = fixture_root(tmp_path, "FIX001_F2_checkpoint_positions")
    actual = {}
    for mode in ("duplicate_position", "legacy_mailboxes"):
        root = case / mode
        value = event(1)
        with JournalSpool(root) as writer:
            writer.append(value)
        first = FileMailbox(root).publish_checkpoint(checkpoint(value, high_water=1, clean=False))
        second = FileMailbox(root).publish_checkpoint(checkpoint(value, high_water=1))
        for path in (root / "checkpoints").glob("*.json"):
            record = json.loads(path.read_bytes())
            record["checkpoint_seq"] = first["checkpoint_seq"]
            if mode == "legacy_mailboxes":
                record["schema"] = "cdpd.spool-checkpoint-record/v1"
            rehash(record)
            save(path, record)
        journal, history = observe(case, mode + "_read", root, value)
        assert journal["faults"] and history["history_coverage"]["loss_count_exact"] is False
        actual[mode + "_faulted"] = bool(journal["faults"])
        actual[mode + "_complete"] = history["history_coverage"]["complete"]
    expected = {"duplicate_position_faulted": True, "duplicate_position_complete": False,
                "legacy_mailboxes_faulted": True, "legacy_mailboxes_complete": False}
    evidence_case(expected=expected, actual=actual, ac=("AC02",), evt=("EVT-02", "EVT-03"))


@pytest.mark.parametrize("mode", ["mixed_domains", "old_stream_close"])
def test_fix001_production_hook_binding_transitions_keep_per_stream_checkpoint_high_water(tmp_path, evidence_case, mode):
    """L05-R1-F2-02: per-stream checkpoints remain valid when global domain allocation advances in another stream."""
    case = fixture_root(tmp_path, "FIX001_F2_hook_binding_" + mode)
    root = case / "runtime"
    mailbox = FileMailbox(root)
    hooks = FailureHooks(mailbox.publish, producer_id="checkpoint-control-producer",
                         producer_instance_id="checkpoint-control-instance",
                         checkpoint_publisher=mailbox.publish_checkpoint,
                         utc_now=lambda: "2026-10-24T00:00:00Z")
    operation = " original-checkpoint-operation "
    diagnostic = lambda: DiagnosticEvent("checkpoint-control-producer", "checkpoint-control-instance",
                                         operation, "ACTION_FINISHED", failure_code="NONE")
    with JournalSpool(root) as writer:
        assert (hooks.heartbeat(operation_id=operation) if mode == "mixed_domains" else hooks.try_emit(diagnostic()))
        assert hooks.drain_once() == 1
        assert writer.process_pending()[0]["stage"] == "ACK_DURABLE"
        hooks.set_binding(BindingContext("22222222-2222-4222-8222-222222222222", "b" * 64, 2))
        assert hooks.try_emit(diagnostic())
        assert hooks.drain_once() == 1
        assert writer.process_pending()[0]["stage"] == "ACK_DURABLE"
        # close publishes old stream's lower nonzero HWM after the new stream's
        # higher HWM. Those are snapshots of different populations, not rollback.
        assert hooks.close()
    journal = read_journal(root)
    save(case / "journal.json", journal)
    histories = [read_history(root, stream=stream_key(row["envelope"]), now_utc=NOW) for row in journal["rows"]]
    save(case / "histories.json", histories)
    snapshots = [record["snapshot"] for record in journal["checkpoints"]]
    save(case / "checkpoint_snapshots.json", snapshots)
    expected = {"faults": 0, "streams_complete": [True, True], "diagnostic_loss": [0, 0],
                "diagnostic_sequences": [1] if mode == "mixed_domains" else [1, 2],
                "checkpoint_failures": 0, "caller_id_preserved": True}
    actual = {"faults": len(journal["faults"]), "streams_complete": [h["history_coverage"]["complete"] for h in histories],
              "diagnostic_loss": [h["history_dropped_count"] for h in histories],
              "diagnostic_sequences": [row["envelope"]["producer_seq"] for row in journal["rows"] if row["ordering"]["domain"] == "DIAGNOSTIC_EVENT"],
              "checkpoint_failures": hooks.stats["checkpoint_failed"],
              "caller_id_preserved": all(row["envelope"]["event"]["operation_id"] == operation for row in journal["rows"])}
    save(case / "expected_actual.json", {"expected": expected, "actual": actual})
    evidence_case(expected=expected, actual=actual, ac=("AC02",), evt=("EVT-02", "EVT-03"))
    assert actual == expected


def test_fix001_liveness_closing_checkpoint_lag_is_unknown_without_diagnostic_loss(tmp_path, evidence_case):
    """L05-R1-F2-01/03: observed liveness beyond clean HWM is incomplete; a consistent later close resolves only that lag."""
    case = fixture_root(tmp_path, "FIX001_F2_liveness_closure")
    root = case / "runtime"
    value = event(1)
    pulse = event(3, kind="HEARTBEAT", reason="HEARTBEAT_SAMPLE")
    with JournalSpool(root) as writer:
        writer.append(value)
        writer.append(pulse)
    mailbox = FileMailbox(root)
    mailbox.publish_checkpoint(checkpoint(value, high_water=1, liveness=2))
    _, lagging = observe(case, "lagging", root, value)
    assert lagging["history_coverage"]["complete"] is False
    assert lagging["history_coverage"]["loss_count_exact"] is False
    assert lagging["checkpoint_incomplete_domains"] == ["LIVENESS_SAMPLE"]
    assert lagging["history_dropped_count"] == lagging["internal_diagnostic_gap"] == 0
    mailbox.publish_checkpoint(checkpoint(value, high_water=1, liveness=3))
    _, closed = observe(case, "closed", root, value)
    assert closed["history_coverage"]["complete"] is True
    # Skipped/coalesced liveness sample numbers do not imply diagnostic loss.
    mailbox.publish_checkpoint(checkpoint(value, high_water=1, liveness=5))
    _, coalesced = observe(case, "coalesced", root, value)
    assert coalesced["history_dropped_count"] == 0
    assert coalesced["history_coverage"]["complete"] is True
    expected = {"lagging_complete": False, "lagging_exact": False, "diagnostic_lower": 0,
                "later_consistent_complete": True, "coalesced_liveness_diagnostic_lower": 0}
    actual = {"lagging_complete": lagging["history_coverage"]["complete"], "lagging_exact": lagging["history_coverage"]["loss_count_exact"],
              "diagnostic_lower": lagging["history_dropped_count"], "later_consistent_complete": closed["history_coverage"]["complete"],
              "coalesced_liveness_diagnostic_lower": coalesced["history_dropped_count"]}
    evidence_case(expected=expected, actual=actual, ac=("AC02",), evt=("EVT-02", "EVT-03"))


def test_fix001_legacy_pruned_journal_cutoff_is_unverified_not_invented(tmp_path, evidence_case):
    """L05-R1-F1-01/02: legacy producer retirement cannot prove a journal sequence cutoff."""
    case = fixture_root(tmp_path, "FIX001_F1_legacy_retirement")
    root = case / "runtime"
    value = event(1)
    with JournalSpool(root, wall_time=lambda: 1000.0) as writer:
        writer.append(value)
    FileMailbox(root).publish_checkpoint(checkpoint(value, high_water=1))
    with JournalSpool(root, wall_time=lambda: 16000.0) as writer:
        assert writer.prune_expired(now_utc=16000.0)["records_removed"] == 1
    current = json.loads((root / "current.json").read_bytes())
    current = {key: item for key, item in current.items() if key in {
        "schema", "clean_shutdown", "last_journal_seq", "stats", "journal_sizes",
        "retained_event_ids", "dropped_event_ids", "producer_high_water",
        "retired_high_water", "uncertain_streams", "stream_losses", "record_sha256"}}
    current["schema"] = "cdpd.spool-current/v1"
    rehash(current)
    save(root / "current.json", current)
    journal, history = observe(case, "read", root, value)
    detected = any(fault["code"] == "CURRENT_RETIREMENT_CUTOFF_UNVERIFIED" for fault in journal["faults"])
    assert detected and history["history_coverage"]["loss_count_exact"] is False
    assert history["missing_durable_event_lower_bound"] == 0
    evidence_case(expected={"retirement_cutoff_unknown": True, "exact": False, "loss_not_guessed": 0},
                  actual={"retirement_cutoff_unknown": detected, "exact": history["history_coverage"]["loss_count_exact"],
                          "loss_not_guessed": history["missing_durable_event_lower_bound"]},
                  ac=("AC02",), evt=("EVT-02",))
