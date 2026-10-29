"""R1 F-003/F-004 closure controls. All data and processes are local synthetic."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from cdpd_chatgpt_profile import spool as spoolmod
from cdpd_chatgpt_profile.spool import (
    FileMailbox, JournalSpool, SpoolError, SpoolLimits, canonical_json,
    payload_sha256, read_history, read_journal, stream_key, verify_receipt,
)
from test_spool_core import event, checkpoint, fixture_root, run_child, NOW


LIMITS = SpoolLimits(max_bytes=262144, max_records=16,
                     critical_reserved_bytes=0, critical_reserved_records=0)


def usage(root):
    paths = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock"))
    unique = {(p.stat().st_dev, p.stat().st_ino): p.stat().st_size for p in paths}
    return {"path_content_bytes": sum(p.stat().st_size for p in paths),
            "unique_inode_content_bytes": sum(unique.values()),
            "files": [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size,
                       "links": p.stat().st_nlink,
                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]}


def publication(root, route, hook=None, *, one_byte_short=False):
    """Return a real publication with an exactly sized synthetic quota."""
    envelope = event()
    owner = FileMailbox(root, limits=LIMITS)
    if route == "ready":
        data = canonical_json({"payload_sha256": payload_sha256(envelope), "envelope": envelope})
        invoke = lambda: owner.publish(envelope)
    elif route == "checkpoint":
        snapshot = checkpoint(envelope, high_water=1)
        record = {"schema": "cdpd.spool-checkpoint-record/v1", "mailbox_instance_id": owner.instance_id,
                  "checkpoint_seq": 1, "snapshot": snapshot}
        record["record_sha256"] = spoolmod._row_hash(record)
        data = canonical_json(record)
        invoke = lambda: owner.publish_checkpoint(snapshot)
    else:
        owner = JournalSpool(root, limits=LIMITS)
        acknowledgement = owner.append(envelope)
        (root / "receipts" / (envelope["event_id"] + ".json")).unlink()
        row = read_journal(root)["rows"][0]
        data = canonical_json(acknowledgement)

        def invoke():
            with spoolmod._FileLock(root / "quota.lock"):
                return owner._publish_ack(row)
    before = usage(root)
    cap = before["path_content_bytes"] + 2 * len(data) - int(one_byte_short)
    owner.limits = replace(LIMITS, max_bytes=cap)
    owner.fault_hook = hook
    return owner, invoke, envelope, data, before, cap


@pytest.mark.parametrize("route", ["ready", "receipt", "checkpoint"])
def test_f3_immutable_alias_peak_and_one_byte_short_guard(tmp_path, evidence_case, route):
    fixture = fixture_root(tmp_path, "FIX001_F3_alias_" + route)
    observed = []
    root = fixture / "normal"

    def observe(stage):
        if stage.endswith("_after_link"):
            observed.append({"stage": stage, **usage(root)})

    owner, invoke, envelope, data, before, cap = publication(root, route, observe)
    try:
        result = invoke()
        after = usage(root)
        assert len(observed) == 1
        assert observed[0]["path_content_bytes"] == cap
        assert after["path_content_bytes"] == before["path_content_bytes"] + len(data)
        assert any(row["links"] == 2 for row in observed[0]["files"])
        if route == "receipt":
            assert verify_receipt(result, envelope, read_journal(root)["rows"][0])
        short_root = fixture / "one_byte_short"
        short_owner, short_call, _, _, short_before, short_cap = publication(short_root, route, one_byte_short=True)
        try:
            with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
                short_call()
            short_after = usage(short_root)
            assert short_after == short_before
        finally:
            if isinstance(short_owner, JournalSpool):
                short_owner.limits = LIMITS
                short_owner.close()
        details = {"route": route, "cap": cap, "payload_bytes": len(data), "before": before,
                   "link_peak": observed, "after": after, "one_byte_short_cap": short_cap,
                   "one_byte_short_before": short_before, "one_byte_short_after": short_after,
                   "metric": "PATH_CONTENT_BYTES_EXCLUDING_FIXED_LOCK_FILES"}
        (fixture / "RESULT.json").write_bytes(canonical_json(details))
        evidence_case(expected={"alias_peak_within_cap": True, "actual_alias_paths": True,
                                "one_byte_short_rejected_without_writes": True},
                      actual={"alias_peak_within_cap": observed[0]["path_content_bytes"] <= cap,
                              "actual_alias_paths": any(row["links"] == 2 for row in observed[0]["files"]),
                              "one_byte_short_rejected_without_writes": short_before == short_after},
                      ac=("AC02",), evt=("EVT-03",),
                      commands=("L05-R1-F3-01; L05-R1-F3-02; real os.link then temporary unlink",))
    finally:
        if isinstance(owner, JournalSpool):
            owner.limits = LIMITS
            owner.close()


CHILD = r'''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from test_l05_fix001_quota_dedup import publication,usage
root=Path(sys.argv[2]);route=sys.argv[3]
def crash(stage):
    if stage.endswith('_after_link'):
        print(json.dumps({'stage':stage,'cap':cap,'before':before,'payload_bytes':len(data),
                          'observed':usage(root),'publication_returned':False}),flush=True)
        os._exit(73)
owner,invoke,envelope,data,before,cap=publication(root,route,crash)
invoke()
raise SystemExit(99)
'''


@pytest.mark.parametrize("route", ["ready", "receipt", "checkpoint"])
def test_f3_real_process_crash_after_link_preserves_cap(tmp_path, evidence_case, route):
    fixture = fixture_root(tmp_path, "FIX001_F3_crash_" + route)
    root = fixture / "spool"
    child = run_child(fixture, CHILD, str(Path(__file__).parent), str(root), route, name="after_link_child")
    assert child.returncode == 73
    assert child.stderr == ""
    observation = json.loads(child.stdout)
    after = usage(root)
    assert after["path_content_bytes"] == observation["cap"]
    assert after["path_content_bytes"] == spoolmod._disk_usage(root)[0]
    assert any(row["links"] == 2 for row in after["files"])
    assert list(root.rglob("*.tmp"))
    if route == "ready":
        ready = next((root / "inbox").glob("*.ready.json"))
        wrapper = spoolmod.strict_json(ready.read_bytes())
        assert payload_sha256(wrapper["envelope"]) == wrapper["payload_sha256"]
    elif route == "receipt":
        row = read_journal(root)["rows"][0]
        receipt = spoolmod.strict_json((root / "receipts" / (row["event_id"] + ".json")).read_bytes())
        assert verify_receipt(receipt, row["envelope"], row)
    else:
        record = spoolmod.strict_json(next((root / "checkpoints").glob("*.json")).read_bytes())
        assert record["record_sha256"] == spoolmod._row_hash(record)
    # The leftover alias is counted on the next process's admission check;
    # recovery cannot treat it as free space because it shares an inode.
    resumed_mailbox = FileMailbox(root, limits=replace(LIMITS, max_bytes=observation["cap"]))
    with pytest.raises(SpoolError, match="SPOOL_BACKPRESSURE"):
        resumed_mailbox.publish(event(2))
    assert usage(root) == after
    (fixture / "POST_CRASH.json").write_bytes(canonical_json(after))
    evidence_case(expected={"child_exit": 73, "crash_residue_within_cap": True,
                            "actual_link_aliases": True, "publication_returned": False},
                  actual={"child_exit": child.returncode, "crash_residue_within_cap": after["path_content_bytes"] <= observation["cap"],
                          "actual_link_aliases": any(row["links"] == 2 for row in after["files"]),
                          "publication_returned": observation["publication_returned"]},
                  ac=("AC02",), evt=("EVT-02", "EVT-03"),
                  commands=("L05-R1-F3-02; real child os._exit(73) after os.link before temporary unlink",))


def test_f3_existing_immutable_final_is_never_overwritten(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "FIX001_F3_no_overwrite")
    path = fixture / "immutable.json"
    original = b'{"metadata":"original"}'
    spoolmod._write_file(path, original)
    with pytest.raises(SpoolError, match="EVENT_ID_CONFLICT"):
        spoolmod._write_file(path, b'{"metadata":"different"}')
    assert path.read_bytes() == original
    assert not list(fixture.glob("*.tmp"))
    evidence_case(expected={"original_preserved": True, "conflict": True},
                  actual={"original_preserved": path.read_bytes() == original, "conflict": True},
                  ac=("AC02",), evt=("EVT-03", "EVT-05"), commands=("L05-R1-F3-02",))


def test_f4_rejected_identity_sequence_matrix_survives_restart(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "FIX001_F4_identity_matrix")
    root = fixture / "spool"
    limits = replace(LIMITS, max_records=2)
    missing, other = event(3), event(4)
    conflict_id = deepcopy(missing)
    conflict_id["event"]["operation_id"] = "different-opaque-caller-id"
    conflict_sequence = deepcopy(missing)
    conflict_sequence["event_id"] = str(uuid4())
    conflict_sequence["control_generation"] = 2
    observations = []
    with JournalSpool(root, limits=limits) as writer:
        assert writer.append(event(1))["stage"] == "ACK_DURABLE"
        assert writer.append(event(2))["stage"] == "ACK_DURABLE"
        for value in (missing, missing, conflict_id, conflict_sequence, other):
            result = writer.append(value)
            observations.append({"failure_code": result["failure_code"], "stats": writer.stats})
        assert [r["failure_code"] for r in observations] == ["SPOOL_BACKPRESSURE", "SPOOL_BACKPRESSURE", "EVENT_ID_CONFLICT", "EVENT_ID_CONFLICT", "SPOOL_BACKPRESSURE"]
        assert observations[3]["stats"]["spool_dropped"] == 1
        assert writer.stats["spool_dropped"] == 2
    before_restart = read_journal(root)["current"]
    with JournalSpool(root, limits=limits) as writer:
        assert writer.append(missing)["failure_code"] == "SPOOL_BACKPRESSURE"
        assert writer.append(conflict_sequence)["failure_code"] == "EVENT_ID_CONFLICT"
        assert writer.append(conflict_id)["failure_code"] == "EVENT_ID_CONFLICT"
        assert writer.stats["spool_dropped"] == 2
        stats = writer.stats
        history = read_history(root, stream=stream_key(missing), now_utc=NOW)
        assert history["history_dropped_count"] == 2
    current = read_journal(root)["current"]
    assert len(current["dropped_sequences"]) == len(current["dropped_event_ids"]) == 2
    assert current["drop_identity_complete"]
    (fixture / "RESULT.json").write_bytes(canonical_json({"observations": observations,
        "before_restart": before_restart, "after_restart": current, "history": history}))
    evidence_case(expected={"known_unique_losses": 2, "same_sequence_loss_after_conflict": 1,
                            "conflicts_after_restart": 4, "paired_ledger_entries": 2},
                  actual={"known_unique_losses": history["history_dropped_count"],
                          "same_sequence_loss_after_conflict": observations[3]["stats"]["spool_dropped"],
                          "conflicts_after_restart": stats["conflicts"],
                          "paired_ledger_entries": len(current["dropped_sequences"])},
                  ac=("AC02",), evt=("EVT-03", "EVT-05"),
                  commands=("L05-R1-F4-01; L05-R1-F4-03",))


def test_f4_full_rejected_ledger_preserves_lower_bound_and_unknown_after_restart(tmp_path, evidence_case):
    fixture = fixture_root(tmp_path, "FIX001_F4_ledger_full")
    root = fixture / "spool"
    limits = replace(LIMITS, max_records=2)
    tracked = [event(3), event(4)]
    untracked = event(5)
    with JournalSpool(root, limits=limits) as writer:
        writer.append(event(1)); writer.append(event(2))
        for value in tracked + [untracked, untracked, event(5), event(6)]:
            assert writer.append(value)["failure_code"] == "SPOOL_BACKPRESSURE"
        assert writer.stats["spool_dropped"] == 2
    prior = read_journal(root)["current"]
    assert prior["drop_identity_complete"] is False
    with JournalSpool(root, limits=limits) as writer:
        assert writer.append(untracked)["failure_code"] == "SPOOL_BACKPRESSURE"
        alias = deepcopy(tracked[0]); alias["event_id"] = str(uuid4())
        assert writer.append(alias)["failure_code"] == "EVENT_ID_CONFLICT"
        assert writer.stats["spool_dropped"] == 2
        history = read_history(root, stream=stream_key(untracked), now_utc=NOW)
        assert history["history_coverage"]["loss_lower_bound"] == 2
        assert history["history_coverage"]["loss_count_exact"] is False
    current = read_journal(root)["current"]
    assert len(current["dropped_event_ids"]) == len(current["dropped_sequences"]) == 2
    # Changing a configured capacity does not reconstruct previously forgotten
    # identities. A later rejected event still cannot increment the lower bound.
    with JournalSpool(root, limits=replace(limits, max_records=4)) as writer:
        assert writer.append(event(7))["stage"] == "ACK_DURABLE"
        assert writer.append(event(8))["stage"] == "ACK_DURABLE"
        assert writer.append(event(9))["failure_code"] == "SPOOL_BACKPRESSURE"
        assert writer.stats["spool_dropped"] == 2
    after_capacity_change = read_journal(root)["current"]
    assert after_capacity_change["drop_identity_complete"] is False
    assert len(after_capacity_change["dropped_sequences"]) == 2
    (fixture / "RESULT.json").write_bytes(canonical_json({"before_restart": prior, "after_restart": current,
        "after_capacity_change": after_capacity_change, "history": history}))
    evidence_case(expected={"lower_bound": 2, "exact": False, "bounded_entries": 2,
                            "identity_complete": False},
                  actual={"lower_bound": history["history_dropped_count"],
                          "exact": history["history_coverage"]["loss_count_exact"],
                          "bounded_entries": len(current["dropped_sequences"]),
                          "identity_complete": current["drop_identity_complete"]},
                  ac=("AC02",), evt=("EVT-03", "EVT-05"),
                  commands=("L05-R1-F4-02; L05-R1-F4-03",))


@pytest.mark.parametrize("legacy_count,carrier_loss", [(1, 0), (2, 0), (2, 1)])
def test_f4_legacy_unverifiable_drop_totals_fail_closed_without_rewriting(tmp_path, evidence_case, legacy_count, carrier_loss):
    fixture = fixture_root(tmp_path, "FIX001_F4_legacy_unknown_" + str(legacy_count) + "_" + str(carrier_loss))
    root = fixture / "spool"
    limits = replace(LIMITS, max_records=2)
    missing = event(3, loss=carrier_loss)
    with JournalSpool(root, limits=limits) as writer:
        writer.append(event(1, loss=carrier_loss))
        writer.append(event(2, loss=carrier_loss))
        writer.append(missing)
    path = root / "current.json"
    current = json.loads(path.read_bytes())
    legacy_names = {"schema", "clean_shutdown", "last_journal_seq", "stats", "journal_sizes",
                    "retained_event_ids", "dropped_event_ids", "producer_high_water",
                    "retired_high_water", "uncertain_streams", "stream_losses", "record_sha256"}
    legacy = {key: value for key, value in current.items() if key in legacy_names}
    legacy["schema"] = "cdpd.spool-current/v1"
    if legacy_count == 2:
        # Exact shape of R1's old bug: a second ID for the same producer seq3,
        # persisted as a second supposedly unique drop without its sequence.
        alias = deepcopy(missing)
        alias["event_id"] = str(uuid4())
        legacy["dropped_event_ids"][alias["event_id"]] = payload_sha256(alias)
        legacy["stats"]["spool_dropped"] = 2
        for loss in legacy["stream_losses"]:
            loss["lower_bound"] = 2
        (fixture / "LEGACY_REJECTED_ENVELOPES.json").write_bytes(canonical_json([missing, alias]))
    legacy["record_sha256"] = spoolmod._row_hash(legacy)
    path.write_bytes(canonical_json(legacy))
    (fixture / "LEGACY_INPUT.json").write_bytes(path.read_bytes())
    before = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file()}
    history = read_history(root, stream=stream_key(missing), now_utc=NOW)
    assert history["history_dropped_count"] == carrier_loss
    assert history["history_coverage"]["loss_count_exact"] is False
    assert history["history_coverage"]["complete"] is False
    assert any(f["code"] == "LEGACY_DROP_IDENTITY_UNVERIFIED" for f in history["faults"])
    with pytest.raises(SpoolError, match="CURRENT_NOT_DURABLE"):
        JournalSpool(root, limits=limits)
    after = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert json.loads(path.read_bytes()) == legacy
    (fixture / "RESULT.json").write_bytes(canonical_json({"legacy_unverified_scalar": legacy_count,
        "proven_carrier_lower": carrier_loss, "history": history, "before": before, "after": after}))
    evidence_case(expected={"proven_lower_only": carrier_loss, "exact": False,
                            "writer_rejected": True, "all_raw_bytes_unchanged": True},
                  actual={"proven_lower_only": history["history_dropped_count"],
                          "exact": history["history_coverage"]["loss_count_exact"],
                          "writer_rejected": True, "all_raw_bytes_unchanged": after == before},
                  ac=("AC02",), evt=("EVT-03", "EVT-05"), commands=("L05-R1-F4-02",))


@pytest.mark.parametrize("defect", ["missing_pair", "duplicate_sequence", "duplicate_id", "unknown_id", "invalid_domain"])
def test_f4_persisted_rejected_ledger_pairing_is_strict(tmp_path, evidence_case, defect):
    fixture = fixture_root(tmp_path, "FIX001_F4_invalid_" + defect)
    root = fixture / "spool"
    limits = replace(LIMITS, max_records=2)
    with JournalSpool(root, limits=limits) as writer:
        for sequence in range(1, 5):
            writer.append(event(sequence))
    path = root / "current.json"
    value = json.loads(path.read_bytes())
    if defect == "missing_pair":
        value["dropped_sequences"].pop()
    elif defect == "duplicate_sequence":
        value["dropped_sequences"][1]["key"] = value["dropped_sequences"][0]["key"]
    elif defect == "duplicate_id":
        value["dropped_sequences"][1]["event_id"] = value["dropped_sequences"][0]["event_id"]
    elif defect == "unknown_id":
        value["dropped_sequences"][1]["event_id"] = str(uuid4())
    else:
        value["dropped_sequences"][1]["key"][1] = "LIVENESS_SAMPLE"
    value["record_sha256"] = spoolmod._row_hash(value)
    path.write_bytes(canonical_json(value))
    observed = read_journal(root)
    assert observed["current"] == {}
    assert any(f["code"] == "CURRENT_NOT_DURABLE" for f in observed["faults"])
    (fixture / "RESULT.json").write_bytes(canonical_json({"defect": defect, "faults": observed["faults"]}))
    evidence_case(expected={"invalid_current_rejected": True, "fault_recorded": True},
                  actual={"invalid_current_rejected": observed["current"] == {},
                          "fault_recorded": any(f["code"] == "CURRENT_NOT_DURABLE" for f in observed["faults"])},
                  ac=("AC02",), evt=("EVT-03", "EVT-05"), commands=("L05-R1-F4-01",))
