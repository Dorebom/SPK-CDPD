"""Original L05 process-crash, independent reader and durability evidence."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from cdpd_chatgpt_profile.spool import (
    FileMailbox, JournalSpool, SpoolError, SpoolLimits, canonical_json,
    event_domain, payload_sha256, read_history, read_journal, stream_key,
    validate_envelope, verify_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-24T00:00:00Z"
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()


def event(sequence=1, *, kind="ACTION_FINISHED", failure="NONE", reason="OPERATION_FINISHED", loss=0, exact=True):
    return {
        "schema": "cdpd.chatgpt-event-envelope/v1", "event_id": str(uuid4()),
        "producer_seq": sequence, "occurred_at_utc": STAMP,
        "binding_id": "11111111-1111-4111-8111-111111111111", "binding_sha256": "a" * 64,
        "control_generation": 1,
        "event": {"schema": "cdpd.event/v1", "producer_id": "l05-test-producer",
                  "producer_instance_id": "l05-instance-001", "operation_id": "caller-op-001",
                  "kind": kind, "failure_code": failure, "correlation": {"task_id": "opaque-task-001"}},
        "profile": {"reason": reason, "dispatch_state": "UNKNOWN", "ui_context": "UNDETERMINED",
                    "document_marker": None, "loss_lower_bound": loss, "loss_count_exact": exact},
    }


def checkpoint(envelope, *, high_water, liveness=0, clean=True, loss=0, exact=True):
    return {"schema": "cdpd.producer-checkpoint/v1",
            "producer_id": envelope["event"]["producer_id"],
            "producer_instance_id": envelope["event"]["producer_instance_id"],
            "binding_id": envelope["binding_id"], "binding_sha256": envelope["binding_sha256"],
            "control_generation": envelope["control_generation"],
            "diagnostic_high_water": high_water, "liveness_high_water": liveness,
            "loss_lower_bound": loss, "loss_count_exact": exact, "clean_shutdown": clean}


def fixture_root(tmp_path: Path, name: str) -> Path:
    base = Path(os.environ.get("CDPD_L05_EVIDENCE_DIR", str(tmp_path)))
    result = base / name
    if result.exists():
        result = base / (name + "_" + uuid4().hex)
    result.mkdir(parents=True)
    return result


def run_child(folder: Path, source: str, *arguments: str, name="child"):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "code")
    command = [sys.executable, "-B", "-c", source, *arguments]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20, check=False)
    (folder / (name + "_command.json")).write_text(json.dumps(command, indent=2), encoding="utf-8")
    (folder / (name + "_stdout.txt")).write_text(result.stdout, encoding="utf-8")
    (folder / (name + "_stderr.txt")).write_text(result.stderr, encoding="utf-8")
    (folder / (name + "_exit_code.json")).write_text(json.dumps({"exit_code": result.returncode}), encoding="utf-8")
    return result


def test_mailbox_ack_and_separate_process_history(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "EVT01_separate_process")
    spool_root = fixture / "spool"
    mailbox = FileMailbox(spool_root)
    first, failure = event(1), event(2, kind="ACTION_FAILED", failure="UNKNOWN", reason="PROVIDER_FAILURE_UNCLASSIFIED")
    mailbox.publish_checkpoint(checkpoint(first, high_water=0, clean=False))
    stages = [mailbox.publish(first)["stage"], mailbox.publish(failure)["stage"]]
    assert not list((spool_root / "receipts").glob("*.json"))
    with JournalSpool(spool_root) as spool:
        acknowledgements = spool.process_pending()
        assert [r["stage"] for r in acknowledgements] == ["ACK_DURABLE", "ACK_DURABLE"]
    mailbox.publish_checkpoint(checkpoint(first, high_water=2))
    source = """import json,sys
from cdpd_chatgpt_profile.spool import read_history
print(json.dumps(read_history(sys.argv[1],now_utc=float(sys.argv[2])),sort_keys=True))
"""
    child = run_child(fixture, source, str(spool_root), str(NOW), name="reader")
    assert child.returncode == 0
    history = json.loads(child.stdout)
    assert [e["event_id"] for e in history["events"]] == [first["event_id"], failure["event_id"]]
    assert history["history_coverage"] == {"complete": True, "loss_count_exact": True, "loss_lower_bound": 0, "sampled": False}
    rows = read_journal(spool_root)["rows"]
    assert all(verify_receipt(ack, row["envelope"], row) for ack, row in zip(acknowledgements, rows))
    evidence_case(expected={"local_publish_before_ack": True, "child_exit": 0, "events": 2, "history_complete": True},
                  actual={"local_publish_before_ack": stages == ["LOCAL_PUBLISHED"] * 2, "child_exit": child.returncode,
                          "events": len(history["events"]), "history_complete": history["history_coverage"]["complete"]},
                  ac=("AC01",), evt=("EVT-01",), commands=("separate Python reader; see reader_command.json",))


@pytest.mark.parametrize("stage,expected_rows,receipt_present", [
    ("journal_mid_write", 1, False),
    ("journal_before_fsync", 2, False),
    ("journal_after_fsync", 2, False),
    ("journal_before_ack", 2, False),
    ("receipt_before_fsync", 2, False),
    ("receipt_after_publish", 2, True),
])
def test_real_process_crash_at_durability_boundaries(tmp_path, evidence_case, stage, expected_rows, receipt_present):
    fixture = fixture_root(tmp_path, "EVT02_crash_" + stage)
    spool_root = fixture / "spool"
    mailbox = FileMailbox(spool_root)
    first, interrupted, later = event(1), event(2), event(3)
    mailbox.publish_checkpoint(checkpoint(first, high_water=0, clean=False))
    with JournalSpool(spool_root) as spool:
        assert spool.append(first)["stage"] == "ACK_DURABLE"
    envelope_file = fixture / "interrupted_envelope.json"
    envelope_file.write_bytes(canonical_json(interrupted))
    source = """import json,os,sys
from cdpd_chatgpt_profile.spool import JournalSpool
def crash(stage):
    if stage == sys.argv[2]:
        os.write(1,('FAULT:'+stage+'\\n').encode())
        os._exit(73)
with JournalSpool(sys.argv[1],fault_hook=crash) as writer:
    writer.append(json.load(open(sys.argv[3],encoding='utf-8')))
raise SystemExit(99)
"""
    child = run_child(fixture, source, str(spool_root), stage, str(envelope_file), name="crashing_writer")
    assert child.returncode == 73
    recovered = read_journal(spool_root)
    assert len(recovered["rows"]) == expected_rows
    ack_exists = (spool_root / "receipts" / (interrupted["event_id"] + ".json")).exists()
    assert ack_exists is receipt_present
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (spool_root / "journal").glob("*.jsonl")}
    with JournalSpool(spool_root) as reopened:
        assert reopened.append(later)["stage"] == "ACK_DURABLE"
    assert all(hashlib.sha256((spool_root / "journal" / name).read_bytes()).hexdigest() == digest for name, digest in before.items())
    history = read_history(spool_root, now_utc=NOW)
    assert not history["history_coverage"]["complete"]
    assert not history["history_coverage"]["loss_count_exact"]
    assert first["event_id"] in {e["event_id"] for e in history["events"]}
    assert later["event_id"] in {e["event_id"] for e in history["events"]}
    evidence_case(expected={"child_exit": 73, "observed_recovered_rows": expected_rows, "receipt_published": receipt_present,
                            "old_bytes_unchanged": True, "history_exact": False},
                  actual={"child_exit": child.returncode, "observed_recovered_rows": len(recovered["rows"]),
                          "receipt_published": ack_exists, "old_bytes_unchanged": True,
                          "history_exact": history["history_coverage"]["loss_count_exact"]},
                  ac=("AC02",), evt=("EVT-02",), commands=("os._exit(73) at " + stage + "; see crashing_writer_command.json",))


def test_combined_quota_and_critical_reserve(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "EVT03_global_quota")
    limits = SpoolLimits(max_records=3, critical_reserved_records=1, max_bytes=262144, critical_reserved_bytes=4096)
    mailbox = FileMailbox(fixture / "spool", limits=limits)
    normal1, normal2, normal3 = event(1), event(2), event(3)
    critical3 = event(3, kind="ACTION_FAILED", failure="UNKNOWN", reason="PROVIDER_FAILURE_UNCLASSIFIED")
    with JournalSpool(fixture / "spool", limits=limits) as spool:
        assert spool.append(normal1)["stage"] == "ACK_DURABLE"
        mailbox.publish(normal2)
        with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
            mailbox.publish(normal3)
        mailbox.publish(critical3)
        with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
            mailbox.publish(event(4, kind="ACTION_FAILED", failure="UNKNOWN", reason="PROVIDER_FAILURE_UNCLASSIFIED"))
        receipts = spool.process_pending()
        assert [r["stage"] for r in receipts] == ["ACK_DURABLE", "ACK_DURABLE"]
        assert len(read_journal(fixture / "spool")["rows"]) == 3
    evidence_case(expected={"normal_limit": 2, "critical_total": 3, "durable_rows": 3},
                  actual={"normal_limit": 2, "critical_total": 3, "durable_rows": len(read_journal(fixture / "spool")["rows"])},
                  ac=("AC02",), evt=("EVT-03",))


def test_order_gap_timeout_late_delivery_and_liveness_domain(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "EVT03_ordering")
    tick = [0.0]
    mailbox = FileMailbox(fixture / "spool")
    normal1, normal2, normal3 = event(1), event(2), event(3)
    heartbeat = event(99, kind="HEARTBEAT", reason="HEARTBEAT_SAMPLE")
    with JournalSpool(fixture / "spool", monotonic=lambda: tick[0]) as spool:
        mailbox.publish(normal1)
        mailbox.publish(normal3)
        mailbox.publish(heartbeat)
        first = spool.process_pending()
        assert len(first) == 2
        tick[0] = 1.999
        assert spool.process_pending() == []
        tick[0] = 2.0
        third = spool.process_pending()
        assert len(third) == 1
        mailbox.publish(normal2)
        late = spool.process_pending()
        assert len(late) == 1
        rows = read_journal(fixture / "spool")["rows"]
        three = next(r for r in rows if r["event_id"] == normal3["event_id"])
        two = next(r for r in rows if r["event_id"] == normal2["event_id"])
        pulse = next(r for r in rows if r["event_id"] == heartbeat["event_id"])
        assert three["ordering"]["missing_ranges"] == [[2, 2]]
        assert two["ordering"]["late"]
        assert pulse["ordering"]["missing_ranges"] == []
    evidence_case(expected={"wait_1_999": 0, "accept_at_2": 1, "missing": [[2, 2]], "late": True, "heartbeat_gap": []},
                  actual={"wait_1_999": 0, "accept_at_2": len(third), "missing": three["ordering"]["missing_ranges"],
                          "late": two["ordering"]["late"], "heartbeat_gap": pulse["ordering"]["missing_ranges"]},
                  ac=("AC02",), evt=("EVT-03", "EVT-05"))


def test_duplicate_counts_and_original_receipt_across_process_restart(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "EVT05_duplicates")
    envelope = event()
    with JournalSpool(fixture / "spool") as spool:
        original = spool.append(envelope)
        duplicate = spool.append(envelope)
        assert duplicate == original
    with JournalSpool(fixture / "spool") as spool:
        assert spool.append(envelope) == original
        changed = deepcopy(envelope)
        changed["event"]["operation_id"] = "different-caller-op"
        assert spool.append(changed)["failure_code"] == "EVENT_ID_CONFLICT"
        same_seq = deepcopy(envelope)
        same_seq["event_id"] = str(uuid4())
        same_seq["control_generation"] = 2
        assert spool.append(same_seq)["failure_code"] == "EVENT_ID_CONFLICT"
        actual = {"rows": len(read_journal(fixture / "spool")["rows"]), "duplicates": spool.stats["duplicates"], "conflicts": spool.stats["conflicts"]}
        assert actual == {"rows": 1, "duplicates": 2, "conflicts": 2}
    evidence_case(expected={"rows": 1, "duplicates": 2, "conflicts": 2}, actual=actual, ac=("AC02",), evt=("EVT-05",))


@pytest.mark.parametrize("location,key,value", [
    ("root", "prompt", "CANARY_PROMPT_92e7"),
    ("event", "response", "CANARY_RESPONSE_92e7"),
    ("profile", "url_path", "/private/chat/CANARY_URL_92e7"),
    ("profile", "cookie", "CANARY_COOKIE_92e7"),
])
def test_disallowed_content_is_rejected_before_any_persistence(tmp_path, evidence_case, location, key, value):
    fixture = fixture_root(tmp_path, "EVT04_reject_" + key)
    mailbox = FileMailbox(fixture / "spool")
    envelope = event()
    target = envelope if location == "root" else envelope[location]
    target[key] = value
    with pytest.raises(SpoolError, match="INPUT_INVALID"):
        mailbox.publish(envelope)
    all_output = b"".join(path.read_bytes() for path in (fixture / "spool").rglob("*") if path.is_file())
    assert value.encode() not in all_output
    assert not list((fixture / "spool" / "inbox").glob("*.ready.json"))
    evidence_case(expected={"rejected": True, "stored_bytes": 0, "canary_present": False},
                  actual={"rejected": True, "stored_bytes": len(all_output), "canary_present": value.encode() in all_output},
                  ac=("AC04",), evt=("EVT-04",))


def test_approved_schema_and_limits_conformance(tmp_path, evidence_case):
    contracts = ROOT / "references" / "approved_l02" / "contracts"
    envelope_schema = json.loads((contracts / "diagnostic_envelope_v1.schema.json").read_text())
    receipt_schema = json.loads((contracts / "diagnostic_receipt_v1.schema.json").read_text())
    matrix = json.loads((contracts / "diagnostic_event_matrix.json").read_text())
    approved = json.loads((contracts / "diagnostic_profile_limits.json").read_text())
    envelope_validator = Draft202012Validator(envelope_schema, format_checker=FormatChecker())
    receipt_validator = Draft202012Validator(receipt_schema, format_checker=FormatChecker())
    for rule in matrix["rules"]:
        envelope = event(kind=rule["event_kind"], failure=rule["failure_code"], reason=rule["profile_reason"])
        envelope_validator.validate(envelope)
        assert validate_envelope(envelope) == envelope
    fixture = fixture_root(tmp_path, "schema_limits")
    with JournalSpool(fixture / "spool") as spool:
        receipt_validator.validate(spool.append(event()))
    defaults = SpoolLimits()
    assert defaults.max_bytes == approved["spool_max_bytes"]
    assert defaults.max_records == approved["spool_max_records"]
    assert defaults.critical_reserved_bytes == approved["spool_critical_reserved_bytes"]
    assert defaults.critical_reserved_records == approved["spool_critical_reserved_records"]
    assert defaults.retention_seconds == approved["binding_ttl_seconds_inherited"] + approved["history_seconds"]
    evidence_case(expected={"matrix_rules": 26, "receipt_schema": True, "default_limits_match": True},
                  actual={"matrix_rules": len(matrix["rules"]), "receipt_schema": True, "default_limits_match": True},
                  ac=("AC02", "AC04"), evt=("EVT-04", "EVT-05"))


@pytest.mark.parametrize("override", [
    {"max_bytes": 67108865}, {"max_records": 4097}, {"max_records": True},
    {"critical_reserved_bytes": 1048577}, {"critical_reserved_records": 33},
    {"gap_wait_seconds": 2.001}, {"retention_seconds": 14519},
])
def test_config_cannot_relax_approved_boundaries(override):
    with pytest.raises(SpoolError, match="INPUT_INVALID"):
        SpoolLimits(**override)


def test_strict_json_negative_cases(tmp_path):
    base = canonical_json(event())
    with pytest.raises(SpoolError):
        validate_envelope(base[:-1] + b',"producer_seq":2}')
    with pytest.raises(SpoolError):
        validate_envelope(base.replace(b'"producer_seq":1', b'"producer_seq":NaN'))
    with pytest.raises(SpoolError):
        validate_envelope(base.replace(b'2026-10-24', b'2026-02-30'))
    malformed = event()
    malformed["producer_seq"] = True
    with pytest.raises(SpoolError):
        validate_envelope(malformed)
    malformed = event()
    malformed["event"]["operation_id"] = "line\nbreak"
    with pytest.raises(SpoolError):
        validate_envelope(malformed)


def test_second_supervisor_cannot_append_concurrently(tmp_path):
    with JournalSpool(tmp_path / "spool") as first:
        with pytest.raises(SpoolError, match="CURRENT_NOT_DURABLE"):
            JournalSpool(tmp_path / "spool")
        assert first.append(event())["stage"] == "ACK_DURABLE"


def test_peak_current_staging_and_missing_receipt_respect_byte_cap(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "EVT03_peak_staging_cap")
    spool_root = fixture / "spool"
    initial = SpoolLimits(max_bytes=65536, max_records=10, critical_reserved_bytes=0, critical_reserved_records=0)
    observed = []
    active_cap = [initial.max_bytes]

    def content_bytes():
        return sum(p.stat().st_size for p in spool_root.rglob("*") if p.is_file() and not p.name.endswith(".lock"))

    def observe(stage):
        if stage in ("current_before_fsync", "current_after_fsync"):
            observed.append({"stage": stage, "bytes": content_bytes(), "cap": active_cap[0]})

    writer = JournalSpool(spool_root, limits=initial, fault_hook=observe)
    envelope = event()
    try:
        acknowledgement = writer.append(envelope)
        baseline = content_bytes()
        current_size = (spool_root / "current.json").stat().st_size
        peak_cap = baseline + current_size + 16
        writer.limits = replace(initial, max_bytes=peak_cap)
        active_cap[0] = peak_cap
        observed.clear()
        assert writer.append(envelope) == acknowledgement
        assert {o["stage"] for o in observed} == {"current_before_fsync", "current_after_fsync"}
        assert all(o["bytes"] <= o["cap"] for o in observed)
        assert all(o["bytes"] >= baseline + current_size for o in observed)

        # Remove only this synthetic ACK to exercise the recovery publication
        # guard with a deliberately one-byte-insufficient storage budget.
        receipt_path = spool_root / "receipts" / (envelope["event_id"] + ".json")
        receipt_size = receipt_path.stat().st_size
        receipt_path.unlink()
        baseline_without_receipt = content_bytes()
        ack_cap = baseline_without_receipt + receipt_size - 1
        writer.limits = replace(initial, max_bytes=ack_cap)
        active_cap[0] = ack_cap
        row = read_journal(spool_root)["rows"][0]
        with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
            writer._publish_ack(row)  # Isolate the actual immutable-ACK I/O guard.
        assert not receipt_path.exists()
        assert content_bytes() == baseline_without_receipt
        with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
            writer.append(envelope)  # Public duplicate path cannot bypass either budget.
        assert not receipt_path.exists()
        assert content_bytes() <= ack_cap
        details = {"current_peak_observations": observed, "baseline_bytes": baseline,
                   "old_current_bytes": current_size, "peak_cap": peak_cap,
                   "ack_cap": ack_cap, "receipt_bytes": receipt_size,
                   "missing_receipt_recreated": False, "final_bytes_at_cap": content_bytes()}
        (fixture / "peak_occupancy.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
        evidence_case(expected={"peak_within_cap": True, "both_current_fsync_boundaries": True,
                                "missing_receipt_recreated": False, "public_duplicate_ack": False},
                      actual={"peak_within_cap": all(o["bytes"] <= o["cap"] for o in observed),
                              "both_current_fsync_boundaries": len(observed) == 2,
                              "missing_receipt_recreated": receipt_path.exists(), "public_duplicate_ack": False},
                      ac=("AC02",), evt=("EVT-03", "EVT-05"))
    finally:
        writer.limits = initial
        active_cap[0] = initial.max_bytes
        writer.close()
