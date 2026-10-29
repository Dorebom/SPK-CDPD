"""Original L05 AC01/02: real local producer death and independent readers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import jsonschema

from cdpd import DiagnosticEvent
from cdpd_chatgpt_profile.failure_hooks import FailureHooks
from cdpd_chatgpt_profile.spool import FileMailbox, JournalSpool, read_journal, verify_receipt

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "references/approved_l02/contracts"
MATRIX = json.loads((CONTRACTS / "diagnostic_event_matrix.json").read_text())["rules"]


@pytest.mark.parametrize("mode,exit_code,lower,exact,complete", [
    ("clean", 0, 0, True, True),
    ("queue_full", 0, 1, True, False),
    ("crash_unflushed", 73, 0, False, False),
])
def test_another_process_reads_durable_history_after_producer_exit(
        mode, exit_code, lower, exact, complete, tmp_path, evidence_case):
    """A separate reader sees confirmed rows, explicit queue loss or crash uncertainty."""
    spool_root = tmp_path / "spool"
    script = ROOT / "tools/l05_fixture_process.py"
    producer_command = [sys.executable, "-B", str(script), mode, "--root", str(spool_root)]
    producer = subprocess.run(producer_command, capture_output=True, timeout=10)
    assert producer.returncode == exit_code, producer.stderr.decode()
    assert not producer.stderr
    produced = json.loads(producer.stdout)
    before = {str(p.relative_to(spool_root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in spool_root.rglob("*") if p.is_file()}
    reader_command = [sys.executable, "-B", str(script), "read", "--root", str(spool_root)]
    reader = subprocess.run(reader_command, capture_output=True, timeout=10)
    assert reader.returncode == 0, reader.stderr.decode()
    assert not reader.stderr
    observed = json.loads(reader.stdout)
    after = {str(p.relative_to(spool_root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in spool_root.rglob("*") if p.is_file()}
    assert after == before
    assert observed["pid"] != produced["pid"] and observed["pid"] != os.getpid()
    history = observed["history"]
    coverage = history["history_coverage"]
    assert coverage["loss_lower_bound"] == lower
    assert history["history_dropped_count"] == lower
    assert coverage["loss_count_exact"] is exact
    assert coverage["complete"] is complete
    assert observed["journal_row_count"] == (1 if exit_code else 3)
    assert all(e["event"]["operation_id"] == " caller-owned-op-01 " for e in history["events"])
    assert history["metadata_only"] and history["capture_status"] == "NOT_PERFORMED"

    artifact_ref = None
    output = os.environ.get("CDPD_L05_EVIDENCE_DIR")
    if output:
        target = Path(output) / ("independent_process_" + mode)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(spool_root, target / "spool", dirs_exist_ok=True)
        (target / "producer.stdout.txt").write_bytes(producer.stdout)
        (target / "producer.stderr.txt").write_bytes(producer.stderr)
        (target / "reader.stdout.txt").write_bytes(reader.stdout)
        (target / "reader.stderr.txt").write_bytes(reader.stderr)
        (target / "COMMANDS.json").write_text(json.dumps([
            {"argv": producer_command, "exit_code": producer.returncode},
            {"argv": reader_command, "exit_code": reader.returncode},
        ], indent=2) + "\n")
        artifact_ref = target.relative_to(ROOT).as_posix()
    evidence_case(
        expected={"producer_exit_code": exit_code, "reader_exit_code": 0,
                  "loss_lower_bound": lower, "loss_count_exact": exact,
                  "complete": complete, "reader_changed_bytes": False},
        actual={"producer_exit_code": producer.returncode, "reader_exit_code": reader.returncode,
                "producer_pid": produced["pid"], "reader_pid": observed["pid"],
                "journal_rows": observed["journal_row_count"], "coverage": coverage,
                "reader_changed_bytes": before != after, "raw_evidence": artifact_ref,
                "producer_memory_drop_count": produced["memory_stats"]["queue_dropped"]},
        ac=["AC01", "AC02"], evt=["EVT-01", "EVT-02", "EVT-03"],
        commands=[producer_command, reader_command])


@pytest.mark.parametrize("rule", MATRIX, ids=[r["case_id"] for r in MATRIX])
def test_fixed_approved_matrix_events_roundtrip_through_hooks_and_journal(rule, tmp_path, evidence_case):
    """Trusted explicit mapping emits a Schema-valid envelope and EVENT_DURABILITY receipt."""
    mailbox = FileMailbox(tmp_path)
    hooks = FailureHooks(mailbox.publish, producer_id="matrix-producer",
                         producer_instance_id="matrix-instance",
                         checkpoint_publisher=mailbox.publish_checkpoint)
    event = DiagnosticEvent("matrix-producer", "matrix-instance", "original-op-matrix",
                            rule["event_kind"], failure_code=rule["failure_code"],
                            correlation={"opaque_case": rule["case_id"]})
    assert hooks.record(event, reason=rule["profile_reason"])
    assert hooks.close(timeout=2.0)
    with JournalSpool(tmp_path) as spool:
        receipts = spool.process_pending()
    data = read_journal(tmp_path)
    assert len(receipts) == len(data["rows"]) == 1
    row = data["rows"][0]
    envelope = row["envelope"]
    for value, schema_name in [(envelope, "diagnostic_envelope_v1.schema.json"),
                               (receipts[0], "diagnostic_receipt_v1.schema.json")]:
        schema = json.loads((CONTRACTS / schema_name).read_text())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    assert verify_receipt(receipts[0], envelope, row)
    assert envelope["event"] == event.to_dict()
    assert envelope["profile"]["reason"] == rule["profile_reason"]
    assert receipts[0]["ack_purpose"] == "EVENT_DURABILITY"
    assert receipts[0]["current_projection"] is None
    evidence_case(
        expected={"matrix_source": "references/approved_l02/contracts/diagnostic_event_matrix.json",
                  "matrix_rule": rule["case_id"], "schema_valid": True, "provider_calls": 0,
                  "ack_purpose": "EVENT_DURABILITY", "original_event_equal": True},
        actual={"matrix_rule": rule["case_id"], "schema_valid": True,
                "event_kind": envelope["event"]["kind"], "failure_code": envelope["event"]["failure_code"],
                "reason": envelope["profile"]["reason"], "journal_rows": len(data["rows"]),
                "ack_purpose": receipts[0]["ack_purpose"], "original_event_equal": envelope["event"] == event.to_dict()},
        ac=["AC01", "AC04"], evt=[])
