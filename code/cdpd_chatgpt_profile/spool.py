"""L05 local metadata mailbox and append-only, single-writer event journal.

Only EVENT_DURABILITY receipts are issued.  Nothing in this module admits a Web
operation, discovers a target, captures DOM, or retries a provider.  All I/O is
explicit and belongs on a diagnostic writer/supervisor boundary, never in a
Facade hook.  ``durable`` means completed file flush/fsync and published receipt;
it is a process-crash contract, not a power-loss or native-Windows qualification.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4


MAX_SAFE_INTEGER = 9007199254740991
# Latest-only liveness slots are independent of diagnostic pending records.
# This is a finite implementation bound, not another consumer or scheduler.
HEARTBEAT_MAX_STREAMS = 32
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ENVELOPE_KEYS = frozenset({"schema", "event_id", "producer_seq", "occurred_at_utc", "binding_id", "binding_sha256", "control_generation", "event", "profile"})
_EVENT_KEYS = frozenset({"schema", "producer_id", "producer_instance_id", "operation_id", "kind", "failure_code", "correlation"})
_PROFILE_KEYS = frozenset({"reason", "dispatch_state", "ui_context", "document_marker", "loss_lower_bound", "loss_count_exact"})
_MATRIX = frozenset({
    ("ACTION_STARTED", "NONE", "OPERATION_ARMED"),
    ("ACTION_FINISHED", "NONE", "OPERATION_FINISHED"),
    ("ACTION_FAILED", "TARGET_MISSING", "LOCATOR_NOT_FOUND"),
    ("ACTION_FAILED", "TARGET_AMBIGUOUS", "LOCATOR_MULTI_MATCH"),
    ("ACTION_FAILED", "NOT_INTERACTABLE", "ELEMENT_NOT_INTERACTABLE"),
    ("ACTION_FAILED", "TIMEOUT", "ACTION_TIMEOUT"),
    ("OBSERVATION_FAILED", "TIMEOUT", "OBSERVATION_TIMEOUT"),
    ("OBSERVATION_FAILED", "UNKNOWN", "OBSERVATION_UNDETERMINED"),
    ("OBSERVATION_FAILED", "DISCONNECTED", "CDP_CHANNEL_LOST"),
    ("ACTION_FAILED", "TARGET_MISSING", "TARGET_IDENTITY_MISMATCH"),
    ("ACTION_FAILED", "TARGET_AMBIGUOUS", "PAGE_SET_VIOLATION"),
    ("OBSERVATION_FAILED", "TIMEOUT", "HEARTBEAT_EXPIRED"),
    ("OBSERVATION_FAILED", "UNKNOWN", "CONSUMER_EXITED"),
    ("OBSERVATION_FAILED", "DISCONNECTED", "CHROME_EXITED"),
    ("OBSERVATION_FAILED", "UNKNOWN", "WORKER_FAILED"),
    ("OBSERVATION_FAILED", "TIMEOUT", "WORKER_DEADLINE"),
    ("OBSERVATION_FAILED", "UNKNOWN", "SPOOL_BACKPRESSURE"),
    ("MANUAL", "NONE", "MANUAL_CAPTURE"),
    ("MANUAL", "NONE", "SCHEDULED_CAPTURE"),
    ("ACTION_FAILED", "RECONCILE_REQUIRED", "SUBMISSION_UNCERTAIN"),
    ("OBSERVATION_FAILED", "UNKNOWN", "CLOCK_OR_KEEPER_LOST"),
    ("OBSERVATION_FAILED", "UNKNOWN", "NAVIGATION_DURING_CAPTURE"),
    ("ACTION_FAILED", "POSTCONDITION_FAILED", "POSTCONDITION_FAILED"),
    ("HEARTBEAT", "NONE", "HEARTBEAT_SAMPLE"),
    ("ACTION_FAILED", "UNKNOWN", "PROVIDER_FAILURE_UNCLASSIFIED"),
    ("ACTION_FAILED", "DISCONNECTED", "PROVIDER_DISCONNECTED"),
})
_FAILURE_CODES = frozenset({"INPUT_INVALID", "EVENT_ID_CONFLICT", "SPOOL_BACKPRESSURE", "STORAGE_ERROR", "PERMISSION_DENIED", "JOURNAL_HASH_MISMATCH", "CURRENT_NOT_DURABLE", "UNKNOWN"})


class SpoolError(Exception):
    """Fixed code only.  Never include input bytes or an OS exception message."""
    def __init__(self, code: str) -> None:
        self.code = code if code in _FAILURE_CODES else "UNKNOWN"
        super().__init__(self.code)


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise SpoolError("INPUT_INVALID") from None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SpoolError("INPUT_INVALID")
        result[key] = value
    return result


def strict_json(data: bytes, *, max_bytes: int = 32768) -> Any:
    if type(data) is not bytes or len(data) > max_bytes:
        raise SpoolError("INPUT_INVALID")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(SpoolError("INPUT_INVALID")))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SpoolError("INPUT_INVALID") from None


def _keys(value: Any, expected: frozenset[str] | set[str]) -> None:
    if type(value) is not dict or set(value) != expected:
        raise SpoolError("INPUT_INVALID")


def _text(value: Any, maximum: int) -> None:
    if type(value) is not str or not value or not value.strip() or len(value) > maximum or _CONTROL.search(value):
        raise SpoolError("INPUT_INVALID")


def _integer(value: Any, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise SpoolError("INPUT_INVALID")


def _binding(value: Mapping[str, Any]) -> None:
    _integer(value["control_generation"])
    if value["binding_id"] is None:
        if value["binding_sha256"] is not None or value["control_generation"] != 0:
            raise SpoolError("INPUT_INVALID")
    elif (type(value["binding_id"]) is not str or not _UUID.fullmatch(value["binding_id"])
          or type(value["binding_sha256"]) is not str or not _SHA.fullmatch(value["binding_sha256"])
          or value["control_generation"] < 1):
        raise SpoolError("INPUT_INVALID")


def validate_envelope(value: dict[str, Any] | bytes, *, max_bytes: int = 32768) -> dict[str, Any]:
    """Validate a detached, strictly allowlisted approved L02 envelope.

    This function performs no I/O.  A correlation value is a caller-asserted
    opaque identifier: validation cannot prove the absence of a secret disguised
    as an otherwise-valid ID.  Requests/results/exceptions are never arguments.
    """
    if type(value) is bytes:
        value = strict_json(value, max_bytes=max_bytes)
    else:
        encoded = canonical_json(value)
        value = strict_json(encoded, max_bytes=max_bytes)
    _keys(value, _ENVELOPE_KEYS)
    if value["schema"] != "cdpd.chatgpt-event-envelope/v1" or type(value["event_id"]) is not str or not _UUID.fullmatch(value["event_id"]):
        raise SpoolError("INPUT_INVALID")
    _integer(value["producer_seq"], 1)
    _binding(value)
    stamp = value["occurred_at_utc"]
    if type(stamp) is not str or not _UTC.fullmatch(stamp):
        raise SpoolError("INPUT_INVALID")
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SpoolError("INPUT_INVALID") from None
    event, profile = value["event"], value["profile"]
    _keys(event, _EVENT_KEYS)
    _keys(profile, _PROFILE_KEYS)
    if event["schema"] != "cdpd.event/v1":
        raise SpoolError("INPUT_INVALID")
    for name in ("producer_id", "producer_instance_id", "operation_id"):
        _text(event[name], 256)
    correlation = event["correlation"]
    if type(correlation) is not dict or len(correlation) > 16:
        raise SpoolError("INPUT_INVALID")
    for key, item in correlation.items():
        _text(key, 64)
        _text(item, 1024)
    triple = (event["kind"], event["failure_code"], profile["reason"])
    if any(type(part) is not str for part in triple) or triple not in _MATRIX:
        raise SpoolError("INPUT_INVALID")
    if profile["dispatch_state"] not in ("NOT_DISPATCHED", "POSSIBLE", "CONFIRMED", "UNKNOWN", "NOT_APPLICABLE") or profile["ui_context"] not in ("IDLE_EMPTY", "IDLE_INPUT_PRESENT", "GENERATING", "RESPONSE_READY", "UNDETERMINED"):
        raise SpoolError("INPUT_INVALID")
    _integer(profile["loss_lower_bound"])
    if type(profile["loss_count_exact"]) is not bool:
        raise SpoolError("INPUT_INVALID")
    marker = profile["document_marker"]
    if marker is not None:
        _keys(marker, {"frame_id", "loader_id"})
        _text(marker["frame_id"], 256)
        _text(marker["loader_id"], 256)
    if len(canonical_json(value)) > max_bytes:
        raise SpoolError("INPUT_INVALID")
    return value


def payload_sha256(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(envelope)).hexdigest()


def event_domain(envelope: dict[str, Any]) -> str:
    event, profile = envelope["event"], envelope["profile"]
    return "LIVENESS_SAMPLE" if (event["kind"], event["failure_code"], profile["reason"]) == ("HEARTBEAT", "NONE", "HEARTBEAT_SAMPLE") else "DIAGNOSTIC_EVENT"


def stream_key(envelope: dict[str, Any]) -> tuple[Any, ...]:
    event = envelope["event"]
    return (event["producer_id"], event["producer_instance_id"], envelope["binding_id"], envelope["binding_sha256"], envelope["control_generation"])


def _sequence_key(envelope: dict[str, Any]) -> tuple[Any, ...]:
    # Binding/generation changes never restart a producer instance's sequence.
    return (envelope["event"]["producer_instance_id"], event_domain(envelope), envelope["producer_seq"])


def is_critical(envelope: dict[str, Any]) -> bool:
    return envelope["event"]["kind"] in ("ACTION_STARTED", "ACTION_FAILED", "OBSERVATION_FAILED")


@dataclass(frozen=True)
class SpoolLimits:
    max_bytes: int = 67108864
    max_records: int = 4096
    critical_reserved_bytes: int = 1048576
    critical_reserved_records: int = 32
    event_max_bytes: int = 32768
    gap_wait_seconds: float = 2.0
    # Conservative hold: an event can arrive at the beginning of the 4h lease.
    retention_seconds: float = 14520.0

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_records", "critical_reserved_bytes", "critical_reserved_records", "event_max_bytes"):
            _integer(getattr(self, name))
        if not 1 <= self.max_bytes <= 67108864 or not 1 <= self.max_records <= 4096 or not 0 <= self.critical_reserved_bytes <= min(1048576, self.max_bytes - 1) or not 0 <= self.critical_reserved_records <= min(32, self.max_records - 1) or not 1 <= self.event_max_bytes <= 32768:
            raise SpoolError("INPUT_INVALID")
        for value in (self.gap_wait_seconds, self.retention_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise SpoolError("INPUT_INVALID")
        if self.gap_wait_seconds > 2 or self.retention_seconds < 14520:
            raise SpoolError("INPUT_INVALID")


def _receipt(envelope: dict[str, Any], stage: str, *, row: dict[str, Any] | None = None, failure: str | None = None) -> dict[str, Any]:
    return {"schema": "cdpd.chatgpt-receipt/v1", "event_id": envelope["event_id"],
            "payload_sha256": payload_sha256(envelope), "stage": stage,
            "journal_seq": row["journal_seq"] if row else None,
            "capture_id": None, "failure_code": failure,
            "journal_record_sha256": row["record_sha256"] if row else None,
            "ack_purpose": "EVENT_DURABILITY", "current_projection": None}


def verify_receipt(receipt: dict[str, Any], envelope: dict[str, Any], row: dict[str, Any]) -> bool:
    """Full receipt/payload/row comparison, not file-existence or mtime trust."""
    try:
        envelope = validate_envelope(envelope)
        _validate_row(row)
        expected = _receipt(envelope, "ACK_DURABLE", row=row)
        _keys(receipt, set(expected))
        _integer(receipt["journal_seq"], 1)
        return canonical_json(receipt) == canonical_json(expected) and row["envelope"] == envelope and row["event_id"] == envelope["event_id"] and row["payload_sha256"] == payload_sha256(envelope)
    except (SpoolError, KeyError, TypeError):
        return False


def _row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({k: v for k, v in row.items() if k != "record_sha256"})).hexdigest()


def _validate_row(row: Any) -> None:
    _keys(row, {"journal_seq", "event_id", "payload_sha256", "envelope", "ordering", "recorded_at_utc", "retention_not_before_utc", "record_sha256"})
    _integer(row["journal_seq"], 1)
    envelope = validate_envelope(row["envelope"])
    if row["event_id"] != envelope["event_id"] or row["payload_sha256"] != payload_sha256(envelope) or row["record_sha256"] != _row_hash(row):
        raise SpoolError("JOURNAL_HASH_MISMATCH")
    ordering = row["ordering"]
    _keys(ordering, {"domain", "late", "missing_ranges"})
    if ordering["domain"] != event_domain(envelope) or type(ordering["late"]) is not bool or type(ordering["missing_ranges"]) is not list:
        raise SpoolError("INPUT_INVALID")
    for gap in ordering["missing_ranges"]:
        if type(gap) is not list or len(gap) != 2:
            raise SpoolError("INPUT_INVALID")
        _integer(gap[0], 1)
        _integer(gap[1], gap[0])
    for name in ("recorded_at_utc", "retention_not_before_utc"):
        value = row[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise SpoolError("INPUT_INVALID")


def _sync_directory(directory: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_file(path: Path, content: bytes, *, replace: bool = False,
                fault: Callable[[str], None] | None = None, tag: str = "file") -> None:
    """Publish complete bytes on one volume; immutable publication never overwrites.

    An exclusive temporary file and an atomic hard-link publication are used for
    immutable mailbox/receipt names.  NTFS supports hard links; qualification on
    the deployment filesystem remains a caller responsibility.  Mutable current
    projections use atomic replace only after fsync.  Neither route exposes a
    partially written final pathname.
    """
    temporary = path.parent / (str(uuid4()) + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            if fault:
                fault(tag + "_before_flush")
            handle.flush()
            if fault:
                fault(tag + "_before_fsync")
            os.fsync(handle.fileno())
            if fault:
                fault(tag + "_after_fsync")
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            # Both pathnames count toward the declared file-content quota,
            # even though the link aliases the same physical inode.
            if fault:
                fault(tag + "_after_link")
            temporary.unlink()
        _sync_directory(path.parent)
        if fault:
            fault(tag + "_after_publish")
    except FileExistsError:
        raise SpoolError("EVENT_ID_CONFLICT") from None
    except PermissionError:
        raise SpoolError("PERMISSION_DENIED") from None
    except OSError:
        raise SpoolError("STORAGE_ERROR") from None
    finally:
        # An interrupted process can leave .tmp. Readers ignore it. Do not
        # remove an immutable final file or hide a published-but-unacknowledged row.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _FileLock:
    """Non-waiting exclusive OS lock, automatically released on process death."""
    def __init__(self, path: Path) -> None:
        self.handle = path.open("a+b")
        self.closed = False
        try:
            if os.name == "nt":
                import msvcrt
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise SpoolError("CURRENT_NOT_DURABLE") from None

    def close(self) -> None:
        if not self.closed:
            self.handle.close()
            self.closed = True

    def __enter__(self) -> "_FileLock":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


_CHECKPOINT_KEYS = frozenset({"schema", "producer_id", "producer_instance_id", "binding_id", "binding_sha256", "control_generation", "diagnostic_high_water", "liveness_high_water", "loss_lower_bound", "loss_count_exact", "clean_shutdown"})


def _validate_checkpoint(snapshot: Any) -> dict[str, Any]:
    snapshot = strict_json(canonical_json(snapshot), max_bytes=4096)
    _keys(snapshot, _CHECKPOINT_KEYS)
    if snapshot["schema"] != "cdpd.producer-checkpoint/v1":
        raise SpoolError("INPUT_INVALID")
    _text(snapshot["producer_id"], 256)
    _text(snapshot["producer_instance_id"], 256)
    _binding(snapshot)
    for name in ("diagnostic_high_water", "liveness_high_water", "loss_lower_bound"):
        _integer(snapshot[name])
    if type(snapshot["loss_count_exact"]) is not bool or type(snapshot["clean_shutdown"]) is not bool:
        raise SpoolError("INPUT_INVALID")
    return snapshot


def _checkpoint_stream(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(snapshot[name] for name in ("producer_id", "producer_instance_id", "binding_id", "binding_sha256", "control_generation"))


def _validate_stream(key: Any) -> None:
    if type(key) is not list or len(key) != 5:
        raise SpoolError("INPUT_INVALID")
    _text(key[0], 256)
    _text(key[1], 256)
    _binding({"binding_id": key[2], "binding_sha256": key[3], "control_generation": key[4]})


_INTEGRITY_CODES = frozenset({
    "JOURNAL_TORN_TAIL", "JOURNAL_HASH_MISMATCH", "CURRENT_NOT_DURABLE",
    "STORAGE_ERROR", "CHECKPOINT_INVALID", "CURRENT_SEGMENT_MISSING",
    "CURRENT_SEGMENT_SHRUNK", "CURRENT_EVENT_MISSING", "CURRENT_EVENT_MISMATCH",
    "CURRENT_HIGH_WATER_MISSING", "CHECKPOINT_SEQUENCE_CONFLICT",
    "CHECKPOINT_HIGH_WATER_REGRESSION", "CHECKPOINT_LOSS_REGRESSION",
    "CHECKPOINT_CARRIER_REGRESSION", "CHECKPOINT_ORDER_UNKNOWN",
    "CURRENT_JOURNAL_HIGH_WATER_MISSING", "CURRENT_RETIREMENT_CUTOFF_UNVERIFIED",
    "LEGACY_DROP_IDENTITY_UNVERIFIED",
})


def _integrity_fault(code: str, *, segment: str = "current.json", offset: int = 0,
                     stream: tuple[Any, ...] | None = None,
                     missing: list[str] | None = None) -> dict[str, Any]:
    return {"segment": segment, "offset": offset, "code": code,
            "stream": list(stream) if stream is not None else None,
            "missing_event_ids": sorted(set(missing or []))}


def _merge_integrity_faults(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Re-reading/recovering the same defect never increments a loss counter.
    unique = {}
    for group in groups:
        for fault in group:
            normalized = _integrity_fault(
                fault["code"], segment=fault["segment"], offset=fault["offset"],
                stream=tuple(fault["stream"]) if fault.get("stream") is not None else None,
                missing=fault.get("missing_event_ids", []))
            unique[canonical_json(normalized)] = normalized
    return [unique[key] for key in sorted(unique)]


def _event_metadata(row: dict[str, Any]) -> dict[str, Any]:
    envelope = row["envelope"]
    return {"event_id": row["event_id"], "stream": list(stream_key(envelope)),
            "domain": event_domain(envelope), "sequence": envelope["producer_seq"],
            "journal_seq": row["journal_seq"], "payload_sha256": row["payload_sha256"],
            "record_sha256": row["record_sha256"]}


def _validate_current(value: Any) -> None:
    base_keys = {"schema", "clean_shutdown", "last_journal_seq", "stats", "journal_sizes", "retained_event_ids", "dropped_event_ids", "producer_high_water", "retired_high_water", "uncertain_streams", "stream_losses", "record_sha256"}
    version_two = type(value) is dict and value.get("schema") == "cdpd.spool-current/v2"
    _keys(value, base_keys | ({"dropped_sequences", "drop_identity_complete", "retained_event_metadata", "prune_intent", "integrity_faults", "retired_journal_seq"} if version_two else set()))
    if value["schema"] not in ("cdpd.spool-current/v1", "cdpd.spool-current/v2") or value["record_sha256"] != _row_hash(value) or type(value["clean_shutdown"]) is not bool:
        raise SpoolError("INPUT_INVALID")
    _integer(value["last_journal_seq"])
    _keys(value["stats"], {"accepted", "duplicates", "conflicts", "rejected", "spool_dropped"})
    for count in value["stats"].values():
        _integer(count)
    if type(value["journal_sizes"]) is not dict or type(value["retained_event_ids"]) is not list or type(value["dropped_event_ids"]) is not dict:
        raise SpoolError("INPUT_INVALID")
    for name, size in value["journal_sizes"].items():
        if not re.fullmatch(r"\d{20}\.jsonl", name):
            raise SpoolError("INPUT_INVALID")
        _integer(size)
    for identifier in value["retained_event_ids"] + list(value["dropped_event_ids"]):
        if type(identifier) is not str or not _UUID.fullmatch(identifier):
            raise SpoolError("INPUT_INVALID")
    for digest in value["dropped_event_ids"].values():
        if type(digest) is not str or not _SHA.fullmatch(digest):
            raise SpoolError("INPUT_INVALID")
    if version_two:
        if type(value["drop_identity_complete"]) is not bool or type(value["dropped_sequences"]) is not list:
            raise SpoolError("INPUT_INVALID")
        keys, identifiers = set(), set()
        for item in value["dropped_sequences"]:
            _keys(item, {"key", "event_id"})
            key, identifier = item["key"], item["event_id"]
            if type(key) is not list or len(key) != 3 or key[1] != "DIAGNOSTIC_EVENT":
                raise SpoolError("INPUT_INVALID")
            _text(key[0], 256)
            _integer(key[2], 1)
            if type(identifier) is not str or identifier not in value["dropped_event_ids"] or tuple(key) in keys or identifier in identifiers:
                raise SpoolError("INPUT_INVALID")
            keys.add(tuple(key))
            identifiers.add(identifier)
        if value["drop_identity_complete"] and identifiers != set(value["dropped_event_ids"]):
            raise SpoolError("INPUT_INVALID")
    for name in ("producer_high_water", "retired_high_water"):
        if type(value[name]) is not list:
            raise SpoolError("INPUT_INVALID")
        for item in value[name]:
            _keys(item, {"key", "sequence"})
            if type(item["key"]) is not list or len(item["key"]) != 2 or item["key"][1] not in ("DIAGNOSTIC_EVENT", "LIVENESS_SAMPLE"):
                raise SpoolError("INPUT_INVALID")
            _text(item["key"][0], 256)
            _integer(item["sequence"])
    if type(value["uncertain_streams"]) is not list or type(value["stream_losses"]) is not list:
        raise SpoolError("INPUT_INVALID")
    for key in value["uncertain_streams"]:
        _validate_stream(key)
    for item in value["stream_losses"]:
        _keys(item, {"stream", "lower_bound", "exact"})
        _validate_stream(item["stream"])
        _integer(item["lower_bound"])
        if type(item["exact"]) is not bool:
            raise SpoolError("INPUT_INVALID")
    if value["schema"] == "cdpd.spool-current/v2":
        _integer(value["retired_journal_seq"])
        if value["retired_journal_seq"] > value["last_journal_seq"]:
            raise SpoolError("INPUT_INVALID")
        if type(value["retained_event_metadata"]) is not list:
            raise SpoolError("INPUT_INVALID")
        metadata_ids = []
        for item in value["retained_event_metadata"]:
            _keys(item, {"event_id", "stream", "domain", "sequence", "journal_seq", "payload_sha256", "record_sha256"})
            if type(item["event_id"]) is not str or not _UUID.fullmatch(item["event_id"]) or item["domain"] not in ("DIAGNOSTIC_EVENT", "LIVENESS_SAMPLE"):
                raise SpoolError("INPUT_INVALID")
            _validate_stream(item["stream"])
            _integer(item["sequence"], 1)
            _integer(item["journal_seq"], 1)
            for field in ("payload_sha256", "record_sha256"):
                if type(item[field]) is not str or not _SHA.fullmatch(item[field]):
                    raise SpoolError("INPUT_INVALID")
            metadata_ids.append(item["event_id"])
        if len(metadata_ids) != len(set(metadata_ids)) or set(metadata_ids) != set(value["retained_event_ids"]):
            raise SpoolError("INPUT_INVALID")
        intent = value["prune_intent"]
        _keys(intent, {"segments", "event_ids"})
        if any(type(intent[name]) is not list or any(type(item) is not str for item in intent[name]) or len(intent[name]) != len(set(intent[name])) for name in ("segments", "event_ids")):
            raise SpoolError("INPUT_INVALID")
        if not set(intent["segments"]) <= set(value["journal_sizes"]) or not set(intent["event_ids"]) <= set(value["retained_event_ids"]):
            raise SpoolError("INPUT_INVALID")
        retired = {tuple(item["key"]): item["sequence"] for item in value["retired_high_water"]}
        for item in value["retained_event_metadata"]:
            if item["event_id"] in intent["event_ids"] and item["sequence"] > retired.get((item["stream"][1], item["domain"]), 0):
                raise SpoolError("INPUT_INVALID")
        if type(value["integrity_faults"]) is not list:
            raise SpoolError("INPUT_INVALID")
        for fault in value["integrity_faults"]:
            _keys(fault, {"segment", "offset", "code", "stream", "missing_event_ids"})
            _text(fault["segment"], 256)
            _integer(fault["offset"])
            if type(fault["code"]) is not str or fault["code"] not in _INTEGRITY_CODES:
                raise SpoolError("INPUT_INVALID")
            if fault["stream"] is not None:
                _validate_stream(fault["stream"])
            if type(fault["missing_event_ids"]) is not list or any(type(identifier) is not str or not _UUID.fullmatch(identifier) for identifier in fault["missing_event_ids"]):
                raise SpoolError("INPUT_INVALID")


def _bounded_read(path: Path, maximum: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise SpoolError("INPUT_INVALID")
    return data


def _disk_usage(root: Path) -> tuple[int, set[str]]:
    files = [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock")]
    total = sum(p.stat().st_size for p in files)
    # Current's hash and exact segment-size projection make the common path
    # bounded without reparsing every historic payload for every publication.
    actual_sizes = {p.name: p.stat().st_size for p in (root / "journal").glob("*.jsonl")}
    try:
        current = strict_json(_bounded_read(root / "current.json", 67108864), max_bytes=67108864)
        _validate_current(current)
        if current.get("journal_sizes") != actual_sizes:
            raise SpoolError("INPUT_INVALID")
        ids = set(current["retained_event_ids"])
        if any(type(item) is not str or not _UUID.fullmatch(item) for item in ids):
            raise SpoolError("INPUT_INVALID")
    except (OSError, SpoolError, KeyError, TypeError, AttributeError):
        recovered = read_journal(root)
        ids = {row["event_id"] for row in recovered["rows"]}
        ids.update("fault:" + str(i) for i, _ in enumerate(recovered["faults"]))
    for path in files:
        # Latest-value liveness has its own bounded mailbox slots. Its
        # physical path bytes remain in total, but a pending sample does not
        # consume the diagnostic pending-record admission budget.
        if path.name.endswith(".ready.json") and path.parent != root / "heartbeat":
            try:
                wrapper = strict_json(_bounded_read(path, 33024), max_bytes=33024)
                envelope = validate_envelope(wrapper["envelope"])
                if wrapper["payload_sha256"] != payload_sha256(envelope):
                    raise SpoolError("INPUT_INVALID")
                ids.add(envelope["event_id"])
            except (OSError, SpoolError, KeyError, TypeError):
                ids.add("invalid:" + path.name)
    return total, ids


class FileMailbox:
    """Off-Web-thread publisher. True/LOCAL_PUBLISHED never means durable ACK."""
    def __init__(self, root: str | Path, *, limits: SpoolLimits | None = None,
                 fault_hook: Callable[[str], None] | None = None) -> None:
        self.root = Path(root)
        self.limits = limits or SpoolLimits()
        self.fault_hook = fault_hook
        self.instance_id = str(uuid4())
        self._checkpoint_seq = 0
        self._mutex = threading.Lock()
        for name in ("inbox", "heartbeat", "receipts", "checkpoints", "journal"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def publish(self, envelope: dict[str, Any]) -> dict[str, Any]:
        envelope = validate_envelope(envelope, max_bytes=self.limits.event_max_bytes)
        content = canonical_json({"payload_sha256": payload_sha256(envelope), "envelope": envelope})
        heartbeat = event_domain(envelope) == "LIVENESS_SAMPLE"
        if heartbeat:
            return self._publish_heartbeat(envelope, content)
        directory = self.root / ("heartbeat" if heartbeat else "inbox")
        # A fresh publication UUID means replaying the same event is delivered
        # to the supervisor's durable duplicate ledger, not silently overwritten.
        with self._mutex:
            lock = _FileLock(self.root / "quota.lock")
            try:
                used, event_ids = _disk_usage(self.root)
                byte_limit = self.limits.max_bytes - (0 if is_critical(envelope) else self.limits.critical_reserved_bytes)
                record_limit = self.limits.max_records - (0 if is_critical(envelope) else self.limits.critical_reserved_records)
                if used + 2 * len(content) > byte_limit or len(event_ids | {envelope["event_id"]}) > record_limit:
                    raise SpoolError("SPOOL_BACKPRESSURE")
                _write_file(directory / (str(uuid4()) + ".ready.json"), content, fault=self.fault_hook, tag="inbox")
            finally:
                lock.close()
        return _receipt(envelope, "LOCAL_PUBLISHED")

    def _publish_heartbeat(self, envelope: dict[str, Any], content: bytes) -> dict[str, Any]:
        """Atomically replace one latest sample for the exact metadata stream.

        Normal diagnostic record admission cannot consume these finite slots.
        All path-content bytes still count toward max_bytes: replacement reserves
        the old file plus the new temporary file, and may use global headroom
        beyond the normal-event byte limit. Actual total exhaustion can refuse.
        No journal ACK, operation permission, liveness deadline or Web I/O is
        implied by a successfully published latest sample.
        """
        key = stream_key(envelope)
        name = hashlib.sha256(canonical_json(list(key))).hexdigest() + ".ready.json"
        path = self.root / "heartbeat" / name
        with self._mutex, _FileLock(self.root / "quota.lock"):
            if path.exists():
                wrapper = strict_json(_bounded_read(path, self.limits.event_max_bytes + 256),
                                      max_bytes=self.limits.event_max_bytes + 256)
                _keys(wrapper, {"payload_sha256", "envelope"})
                old = validate_envelope(wrapper["envelope"], max_bytes=self.limits.event_max_bytes)
                if (event_domain(old) != "LIVENESS_SAMPLE" or stream_key(old) != key or
                        wrapper["payload_sha256"] != payload_sha256(old)):
                    raise SpoolError("INPUT_INVALID")
                if old == envelope:
                    return _receipt(envelope, "LOCAL_PUBLISHED")
                if (envelope["event_id"] == old["event_id"] or
                        envelope["producer_seq"] <= old["producer_seq"]):
                    raise SpoolError("EVENT_ID_CONFLICT")
                before, after = old["profile"], envelope["profile"]
                if (after["loss_lower_bound"] < before["loss_lower_bound"] or
                        (not before["loss_count_exact"] and after["loss_count_exact"])):
                    raise SpoolError("INPUT_INVALID")
            elif len(list(path.parent.glob("*.ready.json"))) >= HEARTBEAT_MAX_STREAMS:
                # Do not evict another stream or imply that its history moved.
                raise SpoolError("SPOOL_BACKPRESSURE")
            used, _ = _disk_usage(self.root)
            if used + len(content) > self.limits.max_bytes:
                raise SpoolError("SPOOL_BACKPRESSURE")
            _write_file(path, content, replace=True, fault=self.fault_hook, tag="heartbeat")
        return _receipt(envelope, "LOCAL_PUBLISHED")

    def publish_checkpoint(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        snapshot = _validate_checkpoint(snapshot)
        with self._mutex:
            lock = _FileLock(self.root / "quota.lock")
            try:
                # v2 orders checkpoint publications across mailbox reconstruction.
                # The quota lock serializes the read/max/publish transaction;
                # UUID, mtime and wall-clock order are never chronology proofs.
                records, _ = _read_checkpoints(self.root)
                self._checkpoint_seq = max((r["checkpoint_seq"] for r in records), default=0) + 1
                _integer(self._checkpoint_seq, 1)
                record = {"schema": "cdpd.spool-checkpoint-record/v2", "mailbox_instance_id": self.instance_id,
                          "checkpoint_seq": self._checkpoint_seq, "snapshot": snapshot}
                record["record_sha256"] = _row_hash(record)
                data = canonical_json(record)
                used, _ = _disk_usage(self.root)
                if used + 2 * len(data) > self.limits.max_bytes:
                    raise SpoolError("SPOOL_BACKPRESSURE")
                _write_file(self.root / "checkpoints" / (str(uuid4()) + ".json"), data, fault=self.fault_hook, tag="checkpoint")
            finally:
                lock.close()
        return record


def _read_checkpoints(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checkpoints, faults = [], []
    for path in sorted((root / "checkpoints").glob("*.json")):
        try:
            value = strict_json(_bounded_read(path, 8192), max_bytes=8192)
            _keys(value, {"schema", "mailbox_instance_id", "checkpoint_seq", "snapshot", "record_sha256"})
            if value["schema"] not in ("cdpd.spool-checkpoint-record/v1", "cdpd.spool-checkpoint-record/v2") or value["record_sha256"] != _row_hash(value):
                raise SpoolError("INPUT_INVALID")
            if type(value["mailbox_instance_id"]) is not str or not _UUID.fullmatch(value["mailbox_instance_id"]):
                raise SpoolError("INPUT_INVALID")
            _integer(value["checkpoint_seq"], 1)
            _validate_checkpoint(value["snapshot"])
            checkpoints.append(value)
        except (OSError, SpoolError, KeyError, TypeError, AttributeError):
            faults.append(_integrity_fault("CHECKPOINT_INVALID", segment=path.name))
    return checkpoints, faults


def _checkpoint_integrity_faults(records: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    faults = []
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    sequence_ids = {}
    for record in records:
        scope = "global" if record["schema"].endswith("/v2") else record["mailbox_instance_id"]
        position = (scope, record["checkpoint_seq"])
        old = sequence_ids.get(position)
        if old is not None and old["record_sha256"] != record["record_sha256"]:
            for snapshot in (old["snapshot"], record["snapshot"]):
                faults.append(_integrity_fault("CHECKPOINT_SEQUENCE_CONFLICT", segment="checkpoints", stream=_checkpoint_stream(snapshot)))
        else:
            sequence_ids[position] = record
        snapshot = record["snapshot"]
        # These high-waters describe this basic stream's allocated samples.
        # Another Binding may have no samples in one domain, and close() may
        # publish an older stream after a newer stream. Compare only like
        # populations; event sequence uniqueness remains instance/domain scoped
        # independently in _sequence_key and the accepted/rejected ledgers.
        groups.setdefault(_checkpoint_stream(snapshot), []).append(record)
    for group in groups.values():
        # v1's ordering is only defined within a mailbox. v2 publications are
        # later than all legacy records and globally ordered within this root.
        legacy: dict[str, list[dict[str, Any]]] = {}
        global_rows = []
        for record in group:
            if record["schema"].endswith("/v2"):
                global_rows.append(record)
            else:
                legacy.setdefault(record["mailbox_instance_id"], []).append(record)
        legacy_streams: dict[tuple[Any, ...], set[str]] = {}
        for mailbox, values in legacy.items():
            for record in values:
                legacy_streams.setdefault(_checkpoint_stream(record["snapshot"]), set()).add(mailbox)
        for stream, mailboxes in legacy_streams.items():
            if len(legacy) > 1:
                faults.append(_integrity_fault("CHECKPOINT_ORDER_UNKNOWN", segment="checkpoints", stream=stream))
        timelines = [sorted(values, key=lambda r: r["checkpoint_seq"]) for values in legacy.values()]
        timelines.append(sorted(global_rows, key=lambda r: r["checkpoint_seq"]))
        legacy_snapshots = [record["snapshot"] for values in legacy.values() for record in values]
        for timeline_index, timeline in enumerate(timelines):
            prior = list(legacy_snapshots) if timeline_index == len(timelines) - 1 else []
            for record in timeline:
                snapshot = record["snapshot"]
                stream = _checkpoint_stream(snapshot)
                for before in prior:
                    if any(snapshot[name] < before[name] for name in ("diagnostic_high_water", "liveness_high_water")):
                        faults.append(_integrity_fault("CHECKPOINT_HIGH_WATER_REGRESSION", segment="checkpoints", stream=stream))
                    if _checkpoint_stream(before) == stream and (snapshot["loss_lower_bound"] < before["loss_lower_bound"] or (not before["loss_count_exact"] and snapshot["loss_count_exact"])):
                        faults.append(_integrity_fault("CHECKPOINT_LOSS_REGRESSION", segment="checkpoints", stream=stream))
                prior.append(snapshot)
    # A checkpoint carries a snapshot at BOTH domain high-waters. Compare
    # carrier snapshots only using their own domain's sequence, never UTC or
    # cross-domain ordering. Equal high-water follows that carrier allocation.
    for record in records:
        snapshot = record["snapshot"]
        stream = _checkpoint_stream(snapshot)
        for row in rows:
            envelope = row["envelope"]
            if stream_key(envelope) != stream:
                continue
            high_water = snapshot["liveness_high_water"] if event_domain(envelope) == "LIVENESS_SAMPLE" else snapshot["diagnostic_high_water"]
            profile = envelope["profile"]
            before, after = (profile, snapshot) if envelope["producer_seq"] <= high_water else (snapshot, profile)
            if after["loss_lower_bound"] < before["loss_lower_bound"] or (not before["loss_count_exact"] and after["loss_count_exact"]):
                faults.append(_integrity_fault("CHECKPOINT_CARRIER_REGRESSION", segment="checkpoints", stream=stream))
    return _merge_integrity_faults(faults)


def _current_integrity_faults(root: Path, current: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not current:
        return []
    faults = list(current.get("integrity_faults", []))
    intent = current.get("prune_intent", {"segments": [], "event_ids": []})
    expected_ids = set(current["retained_event_ids"]) - set(intent["event_ids"])
    observed = {row["event_id"]: row for row in rows}
    metadata = {item["event_id"]: item for item in current.get("retained_event_metadata", [])}
    for name, size in current["journal_sizes"].items():
        path = root / "journal" / name
        try:
            actual_size = path.stat().st_size
        except OSError:
            if name not in intent["segments"]:
                faults.append(_integrity_fault("CURRENT_SEGMENT_MISSING", segment=name))
        else:
            # Additional complete or torn append bytes are handled by the
            # journal reader. Current may legally lag a new append; shrinking
            # an existing segment is never an append or retention operation.
            if actual_size < size:
                faults.append(_integrity_fault("CURRENT_SEGMENT_SHRUNK", segment=name))
    for identifier in sorted(expected_ids):
        item, row = metadata.get(identifier), observed.get(identifier)
        if row is None:
            faults.append(_integrity_fault(
                "CURRENT_EVENT_MISSING", stream=tuple(item["stream"]) if item else None,
                missing=[identifier] if item and item["domain"] == "DIAGNOSTIC_EVENT" else []))
        elif item is not None and _event_metadata(row) != item:
            faults.append(_integrity_fault("CURRENT_EVENT_MISMATCH", stream=tuple(item["stream"])))
    observed_high_water: dict[tuple[Any, ...], int] = {}
    for row in rows:
        key = _sequence_key(row["envelope"])[:2]
        observed_high_water[key] = max(observed_high_water.get(key, 0), row["envelope"]["producer_seq"])
    retired = {tuple(item["key"]): item["sequence"] for item in current["retired_high_water"]}
    for item in current["producer_high_water"]:
        key = tuple(item["key"])
        if max(observed_high_water.get(key, 0), retired.get(key, 0)) < item["sequence"]:
            faults.append(_integrity_fault("CURRENT_HIGH_WATER_MISSING"))
    observed_journal_seq = max((row["journal_seq"] for row in rows), default=0)
    if max(observed_journal_seq, current.get("retired_journal_seq", 0)) < current["last_journal_seq"]:
        # v1 did not retain the retired journal cutoff. A stored producer HWM
        # is not a proof of a journal sequence from a different sequence space.
        code = "CURRENT_RETIREMENT_CUTOFF_UNVERIFIED" if current["schema"].endswith("/v1") and retired else "CURRENT_JOURNAL_HIGH_WATER_MISSING"
        faults.append(_integrity_fault(code))
    return _merge_integrity_faults(faults)


def read_journal(root: str | Path) -> dict[str, Any]:
    """Read only complete, hash-valid rows, with explicit uncertainty metadata.

    Torn/corrupt tails are never rewritten.  Prefix rows remain usable and a
    subsequent writer starts a fresh segment.  No browser or provider is called.
    """
    root = Path(root)
    rows: list[dict[str, Any]] = []
    faults: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_sequences: set[tuple[Any, ...]] = set()
    snapshots: dict[tuple[Any, ...], dict[int, dict[str, Any]]] = {}
    last_seq = 0
    for path in sorted((root / "journal").glob("*.jsonl")):
        try:
            with path.open("rb") as handle:
                offset = 0
                while True:
                    line = handle.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536 or not line.endswith(b"\n"):
                        faults.append({"segment": path.name, "offset": offset, "code": "JOURNAL_TORN_TAIL"})
                        break
                    try:
                        row = strict_json(line, max_bytes=65536)
                        _validate_row(row)
                        key = _sequence_key(row["envelope"])
                        if row["journal_seq"] <= last_seq or row["event_id"] in seen_ids or key in seen_sequences:
                            raise SpoolError("JOURNAL_HASH_MISMATCH")
                        envelope = row["envelope"]
                        sample_key = stream_key(envelope) + (event_domain(envelope),)
                        prior = snapshots.setdefault(sample_key, {})
                        sequence, profile = envelope["producer_seq"], envelope["profile"]
                        for old_seq, old_profile in prior.items():
                            before, after = (old_profile, profile) if old_seq < sequence else (profile, old_profile)
                            if after["loss_lower_bound"] < before["loss_lower_bound"] or (not before["loss_count_exact"] and after["loss_count_exact"]):
                                raise SpoolError("INPUT_INVALID")
                    except (SpoolError, KeyError, TypeError):
                        faults.append({"segment": path.name, "offset": offset, "code": "JOURNAL_HASH_MISMATCH"})
                        break
                    rows.append(row)
                    seen_ids.add(row["event_id"])
                    seen_sequences.add(key)
                    prior[sequence] = profile
                    last_seq = row["journal_seq"]
                    offset += len(line)
        except OSError:
            faults.append({"segment": path.name, "offset": 0, "code": "STORAGE_ERROR"})
    current: dict[str, Any] = {}
    try:
        current = strict_json(_bounded_read(root / "current.json", 67108864), max_bytes=67108864)
        _validate_current(current)
        if current["schema"] == "cdpd.spool-current/v1" and current["dropped_event_ids"]:
            # v1 lacks rejected sequence identity and could have counted two
            # event IDs for one position. Its scalar is raw provenance only.
            faults.append({"segment": "current.json", "offset": 0,
                           "code": "LEGACY_DROP_IDENTITY_UNVERIFIED"})
    except (OSError, SpoolError, AttributeError, TypeError, KeyError):
        current = {}
        if rows:
            faults.append({"segment": "current.json", "offset": 0, "code": "CURRENT_NOT_DURABLE"})
    checkpoints, checkpoint_faults = _read_checkpoints(root)
    faults = _merge_integrity_faults(
        faults, checkpoint_faults, _current_integrity_faults(root, current, rows),
        _checkpoint_integrity_faults(checkpoints, rows))
    return {"schema": "cdpd.spool-read/v1", "rows": rows, "faults": faults,
            "current": current, "checkpoints": checkpoints,
            "last_journal_seq": max(last_seq, current.get("last_journal_seq", 0)), "metadata_only": True}


def read_history(root: str | Path, *, stream: tuple[Any, ...] | None = None,
                 now_utc: float | None = None, history_seconds: float = 120.0,
                 max_events: int = 1000, max_bytes: int = 1048576) -> dict[str, Any]:
    """Bounded per-stream S0 history plus cumulative, sticky loss lower bounds.

    This produces input for L06, not a capture bundle or downstream acceptance.
    UTC filtering only selects an observational window; it never clears loss.
    """
    if max_events < 1 or max_events > 1000 or max_bytes < 1 or max_bytes > 1048576 or not 0 < history_seconds <= 120:
        raise SpoolError("INPUT_INVALID")
    data = read_journal(root)
    streams = {stream_key(row["envelope"]) for row in data["rows"]}
    if stream is None:
        if len(streams) > 1:
            raise SpoolError("INPUT_INVALID")
        stream = next(iter(streams), None)
    all_rows = [row for row in data["rows"] if stream_key(row["envelope"]) == stream]
    now = time.time() if now_utc is None else now_utc
    cutoff = now - history_seconds
    candidates = [row for row in all_rows if datetime.strptime(row["envelope"]["occurred_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() >= cutoff]
    kept: list[dict[str, Any]] = []
    used = 0
    omitted = 0
    for row in reversed(candidates):
        size = len(canonical_json(row["envelope"]))
        if len(kept) >= max_events or used + size > max_bytes:
            omitted += 1
            continue
        used += size
        kept.append(row)
    kept.reverse()
    envelopes = [row["envelope"] for row in kept]
    carrier_loss = max((row["envelope"]["profile"]["loss_lower_bound"] for row in all_rows), default=0)
    exact = all(row["envelope"]["profile"]["loss_count_exact"] for row in all_rows)
    # Retained event seq gaps are independent of liveness coalescing.  Do not add
    # overlapping producer counter snapshots and gap counts.
    diagnostic = sorted({e["producer_seq"] for e in envelopes if event_domain(e) == "DIAGNOSTIC_EVENT"})
    gap = sum(max(0, b - a - 1) for a, b in zip(diagnostic, diagnostic[1:]))
    checkpoint_groups: dict[str, list[dict[str, Any]]] = {}
    for record in data["checkpoints"]:
        if _checkpoint_stream(record["snapshot"]) == stream:
            group = "global" if record["schema"].endswith("/v2") else record["mailbox_instance_id"]
            checkpoint_groups.setdefault(group, []).append(record)
    clean = bool(checkpoint_groups)
    checkpoint_loss = 0
    checkpoint_incomplete_domains: set[str] = set()
    for group_key, group in checkpoint_groups.items():
        group.sort(key=lambda v: v["checkpoint_seq"])
        final = group[-1]["snapshot"]
        # A v2 final snapshot follows every legacy record in this root. A clean
        # v2 snapshot can close a consistent earlier dirty checkpoint, but no
        # clean bit clears a contradictory or inexact retained snapshot.
        if group_key == "global" or "global" not in checkpoint_groups:
            clean = clean and final["clean_shutdown"]
        checkpoint_loss = max(checkpoint_loss, *(r["snapshot"]["loss_lower_bound"] for r in group))
        exact = exact and all(r["snapshot"]["loss_count_exact"] for r in group)
        if final["clean_shutdown"] and (group_key == "global" or "global" not in checkpoint_groups):
            observed = max((r["envelope"]["producer_seq"] for r in all_rows if event_domain(r["envelope"]) == "DIAGNOSTIC_EVENT"), default=0)
            if final["diagnostic_high_water"] > observed and final["loss_lower_bound"] < final["diagnostic_high_water"] - observed:
                exact = False
                checkpoint_incomplete_domains.add("DIAGNOSTIC_EVENT")
            if final["diagnostic_high_water"] < observed:
                exact = False
                checkpoint_incomplete_domains.add("DIAGNOSTIC_EVENT")
            observed_liveness = max((r["envelope"]["producer_seq"] for r in all_rows if event_domain(r["envelope"]) == "LIVENESS_SAMPLE"), default=0)
            if final["liveness_high_water"] < observed_liveness:
                # This is an incomplete closing snapshot, not a diagnostic
                # event loss. A later consistent checkpoint can close it.
                exact = False
                checkpoint_incomplete_domains.add("LIVENESS_SAMPLE")
    current = data["current"]
    recovered = {tuple(item) for item in current.get("uncertain_streams", [])}
    faults = [fault for fault in data["faults"] if fault.get("stream") is None or tuple(fault["stream"]) == stream]
    exact = exact and clean and not faults and stream not in recovered
    known_drop = 0
    legacy_drops = current.get("schema") == "cdpd.spool-current/v1" and bool(current.get("dropped_event_ids"))
    # Do not relabel an unverifiable legacy scalar as a proven unique lower
    # bound. Valid journal/carrier/gap evidence remains independently usable.
    for item in ([] if legacy_drops else current.get("stream_losses", [])):
        if tuple(item["stream"]) == stream:
            known_drop = max(known_drop, item["lower_bound"])
            exact = exact and item["exact"]
    missing_ids = {identifier for fault in faults if fault.get("stream") is not None and tuple(fault["stream"]) == stream for identifier in fault.get("missing_event_ids", [])}
    # This is a union of proven missing diagnostic identities in this stream,
    # never a sum of overlapping faults, snapshots, gaps, and rejected IDs.
    missing_lower = len(missing_ids)
    lower = max(carrier_loss, checkpoint_loss, gap, known_drop, omitted, missing_lower)
    # A lower bound from carrier snapshots/gaps and independently omitted rows
    # is not an exact total without proving their overlap. Do not label max()
    # as an exact sum merely because each contributing observation was exact.
    if omitted and max(carrier_loss, checkpoint_loss, gap, known_drop):
        exact = False
    if known_drop and max(carrier_loss, checkpoint_loss):
        exact = False
    complete = bool(all_rows) and exact and lower == 0 and omitted == 0
    return {"schema": "cdpd.spool-history/v1", "stream": list(stream) if stream is not None else None,
            "events": envelopes, "source_cutoff_seq": data["last_journal_seq"],
            "history_dropped_count": lower,
            "history_coverage": {"complete": complete, "loss_lower_bound": lower,
                                 "loss_count_exact": exact, "sampled": omitted > 0},
            "retained_events": len(envelopes), "retained_bytes": used,
            "omitted_by_limit": omitted, "internal_diagnostic_gap": gap,
            "checkpoint_incomplete_domains": sorted(checkpoint_incomplete_domains),
            "faults": faults, "missing_durable_event_lower_bound": missing_lower, "metadata_only": True,
            "capture_status": "NOT_PERFORMED"}


class JournalSpool:
    """Exclusive supervisor journal writer; no sleep, browser, or provider work."""
    def __init__(self, root: str | Path, *, limits: SpoolLimits | None = None,
                 fault_hook: Callable[[str], None] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_time: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.limits = limits or SpoolLimits()
        self.fault_hook, self.monotonic, self.wall_time = fault_hook, monotonic, wall_time
        for name in ("inbox", "heartbeat", "receipts", "journal", "checkpoints"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._lock = _FileLock(self.root / "writer.lock")
        self._mutex = threading.RLock()
        self._closed, self._faulted = False, False
        self._gap_started: dict[tuple[str, str], float] = {}
        self._id_ledger: dict[str, dict[str, Any]] = {}
        self._seq_ledger: dict[tuple[Any, ...], str] = {}
        self._high_water: dict[tuple[str, str], int] = {}
        self._stream_snapshot: dict[tuple[Any, ...], dict[int, dict[str, Any]]] = {}
        try:
            data = read_journal(self.root)
            self._rows = data["rows"]
            current = data["current"]
            if current.get("schema") == "cdpd.spool-current/v1" and current.get("dropped_event_ids"):
                # Preserve the legacy bytes and reject this diagnostic writer
                # before opening any new journal segment or rewriting current.
                raise SpoolError("CURRENT_NOT_DURABLE")
            self._next_seq = max(data["last_journal_seq"], current.get("last_journal_seq", 0)) + 1
            self._stats = {name: int(current.get("stats", {}).get(name, 0)) for name in ("accepted", "duplicates", "conflicts", "rejected", "spool_dropped")}
            self._uncertain = {tuple(item) for item in current.get("uncertain_streams", [])}
            self._losses: dict[tuple[Any, ...], dict[str, Any]] = {tuple(item["stream"]): {"lower_bound": item["lower_bound"], "exact": item["exact"]} for item in current.get("stream_losses", [])}
            self._dropped_ids = dict(current.get("dropped_event_ids", {}))
            self._dropped_sequences = {tuple(item["key"]): item["event_id"] for item in current.get("dropped_sequences", [])}
            # Only v1 with no rejections can be upgraded; nonempty v1 was
            # rejected above. v2 unknown flags and scoped losses remain sticky.
            self._drop_identity_complete = current.get("drop_identity_complete", True)
            self._retired_high_water = {tuple(item["key"]): item["sequence"] for item in current.get("retired_high_water", [])}
            self._retired_journal_seq = current.get("retired_journal_seq", 0)
            self._high_water = {tuple(item["key"]): item["sequence"] for item in current.get("producer_high_water", [])}
            self._integrity_faults = _merge_integrity_faults(data["faults"])
            self._prune_intent = {"segments": [], "event_ids": []}
            self._prune_journal_sizes: dict[str, int] = {}
            if self._rows and (not current or not current.get("clean_shutdown", False) or data["faults"]):
                for row in self._rows:
                    key = stream_key(row["envelope"])
                    if not current or not current.get("clean_shutdown", False) or any(f.get("stream") is None or tuple(f["stream"]) == key for f in data["faults"]):
                        self._uncertain.add(key)
            for row in self._rows:
                self._index(row)
            # Never append to an old or torn segment.  This preserves every
            # forensic byte and allows the validated prefix to remain readable.
            segments = list((self.root / "journal").glob("*.jsonl"))
            next_segment = max((int(p.stem) for p in segments if p.stem.isdigit()), default=0) + 1
            self._segment = self.root / "journal" / f"{next_segment:020d}.jsonl"
            self._handle = self._segment.open("xb", buffering=0)
            quota = _FileLock(self.root / "quota.lock")
            try:
                self._persist_current(clean=False)
            finally:
                quota.close()
        except BaseException:
            self._lock.close()
            raise

    def _fault(self, stage: str) -> None:
        if self.fault_hook:
            self.fault_hook(stage)

    def _index(self, row: dict[str, Any]) -> None:
        envelope = row["envelope"]
        self._id_ledger[row["event_id"]] = row
        self._seq_ledger[_sequence_key(envelope)] = row["event_id"]
        key = _sequence_key(envelope)[:2]
        self._high_water[key] = max(self._high_water.get(key, 0), envelope["producer_seq"])
        snapshots = self._stream_snapshot.setdefault(stream_key(envelope) + (event_domain(envelope),), {})
        snapshots[envelope["producer_seq"]] = envelope["profile"]

    def _persist_current(self, *, clean: bool) -> None:
        journal_sizes = {p.name: p.stat().st_size for p in (self.root / "journal").glob("*.jsonl")}
        for name in self._prune_intent["segments"]:
            journal_sizes.setdefault(name, self._prune_journal_sizes[name])
        value = {"schema": "cdpd.spool-current/v2", "clean_shutdown": clean,
                 "last_journal_seq": self._next_seq - 1, "stats": dict(self._stats),
                 "retired_journal_seq": self._retired_journal_seq,
                 "journal_sizes": journal_sizes,
                 "retained_event_ids": sorted(self._id_ledger),
                 "retained_event_metadata": [_event_metadata(self._id_ledger[identifier]) for identifier in sorted(self._id_ledger)],
                 "prune_intent": self._prune_intent,
                 "integrity_faults": self._integrity_faults,
                 "dropped_event_ids": dict(self._dropped_ids),
                 "dropped_sequences": [{"key": list(k), "event_id": v} for k, v in sorted(self._dropped_sequences.items())],
                 "drop_identity_complete": self._drop_identity_complete,
                 "producer_high_water": [{"key": list(k), "sequence": v} for k, v in sorted(self._high_water.items())],
                 "retired_high_water": [{"key": list(k), "sequence": v} for k, v in sorted(self._retired_high_water.items())],
                 "uncertain_streams": [list(k) for k in sorted(self._uncertain, key=repr)],
                 "stream_losses": [{"stream": list(k), **v} for k, v in sorted(self._losses.items(), key=lambda x: repr(x[0]))]}
        value["record_sha256"] = _row_hash(value)
        encoded = canonical_json(value)
        path = self.root / "current.json"
        # The old current remains present until the fully fsynced temporary
        # replacement is atomically published. Reserve both copies at peak.
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(".lock"))
        if used + len(encoded) > self.limits.max_bytes:
            raise SpoolError("SPOOL_BACKPRESSURE")
        _write_file(path, encoded, replace=True, fault=self.fault_hook, tag="current")

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def _check_open(self) -> None:
        if self._closed or self._faulted:
            raise SpoolError("CURRENT_NOT_DURABLE")

    def _loss(self, envelope: dict[str, Any]) -> None:
        if event_domain(envelope) == "LIVENESS_SAMPLE":
            return
        key = stream_key(envelope)
        entry = self._losses.setdefault(key, {"lower_bound": 0, "exact": True})
        if envelope["event_id"] not in self._dropped_ids:
            if (self._drop_identity_complete and len(self._dropped_ids) < self.limits.max_records
                    and entry["lower_bound"] < MAX_SAFE_INTEGER and self._stats["spool_dropped"] < MAX_SAFE_INTEGER):
                sequence = _sequence_key(envelope)
                # _append_locked checked both ledgers before calling _loss.
                # Keep the identities together in one atomic current snapshot.
                self._dropped_ids[envelope["event_id"]] = payload_sha256(envelope)
                self._dropped_sequences[sequence] = envelope["event_id"]
                entry["lower_bound"] += 1
                self._stats["spool_dropped"] += 1
            else:
                # Do not count a unique rejection we cannot remember. This
                # marker remains sticky even after restart or a larger limit.
                self._drop_identity_complete = False
                entry["exact"] = False
                self._uncertain.add(key)
        self._persist_current(clean=False)

    def _consistent_snapshots(self, envelope: dict[str, Any]) -> bool:
        snapshots = self._stream_snapshot.get(stream_key(envelope) + (event_domain(envelope),), {})
        sequence, profile = envelope["producer_seq"], envelope["profile"]
        for old_seq, old in snapshots.items():
            before, after = (old, profile) if old_seq < sequence else (profile, old)
            if after["loss_lower_bound"] < before["loss_lower_bound"] or (not before["loss_count_exact"] and after["loss_count_exact"]):
                return False
        return True

    def _publish_ack(self, row: dict[str, Any]) -> dict[str, Any]:
        receipt = _receipt(row["envelope"], "ACK_DURABLE", row=row)
        path = self.root / "receipts" / (row["event_id"] + ".json")
        if path.exists():
            try:
                existing = strict_json(_bounded_read(path, 4096), max_bytes=4096)
            except (OSError, SpoolError):
                raise SpoolError("JOURNAL_HASH_MISMATCH") from None
            if not verify_receipt(existing, row["envelope"], row):
                raise SpoolError("JOURNAL_HASH_MISMATCH")
            return existing
        encoded = canonical_json(receipt)
        # Recovery may recreate an absent ACK without adding a journal row;
        # it must reserve both staged and linked pathnames under the quota lock.
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(".lock"))
        if used + 2 * len(encoded) > self.limits.max_bytes:
            raise SpoolError("SPOOL_BACKPRESSURE")
        _write_file(path, encoded, fault=self.fault_hook, tag="receipt")
        return receipt

    def append(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Append one already ordered event; never wait for a missing sequence.

        ``process_pending`` supplies the bounded reordering policy for mailbox
        traffic. Direct append is an explicit supervisor ingestion boundary and
        records any gap immediately, without claiming earlier missing history.
        """
        envelope = validate_envelope(envelope, max_bytes=self.limits.event_max_bytes)
        with self._mutex:
            quota = _FileLock(self.root / "quota.lock")
            try:
                return self._append_locked(envelope)
            finally:
                quota.close()

    def _append_locked(self, envelope: dict[str, Any]) -> dict[str, Any]:
        with self._mutex:
            self._check_open()
            old = self._id_ledger.get(envelope["event_id"])
            if old is not None:
                if old["payload_sha256"] != payload_sha256(envelope):
                    self._stats["conflicts"] += 1
                    self._persist_current(clean=False)
                    return _receipt(envelope, "REJECTED", failure="EVENT_ID_CONFLICT")
                # Existing complete rows were fsynced before their first ACK.
                # After a process crash re-fsync before recreating a lost ACK.
                self._stats["duplicates"] += 1
                self._sync_all_segments()
                self._persist_current(clean=False)
                return self._publish_ack(old)
            if _sequence_key(envelope) in self._seq_ledger:
                self._stats["conflicts"] += 1
                self._persist_current(clean=False)
                return _receipt(envelope, "REJECTED", failure="EVENT_ID_CONFLICT")
            sequence_key = _sequence_key(envelope)
            if envelope["producer_seq"] <= self._retired_high_water.get(sequence_key[:2], 0):
                self._stats["conflicts"] += 1
                self._persist_current(clean=False)
                return _receipt(envelope, "REJECTED", failure="EVENT_ID_CONFLICT")
            if envelope["event_id"] in self._dropped_ids:
                if self._dropped_ids[envelope["event_id"]] != payload_sha256(envelope):
                    self._stats["conflicts"] += 1
                    self._persist_current(clean=False)
                    return _receipt(envelope, "REJECTED", failure="EVENT_ID_CONFLICT")
                self._stats["duplicates"] += 1
                self._persist_current(clean=False)
                return _receipt(envelope, "REJECTED", failure="SPOOL_BACKPRESSURE")
            if _sequence_key(envelope) in self._dropped_sequences:
                self._stats["conflicts"] += 1
                self._persist_current(clean=False)
                return _receipt(envelope, "REJECTED", failure="EVENT_ID_CONFLICT")
            if not self._consistent_snapshots(envelope):
                self._stats["rejected"] += 1
                self._persist_current(clean=False)
                return _receipt(envelope, "REJECTED", failure="INPUT_INVALID")
            key = _sequence_key(envelope)[:2]
            previous = self._high_water.get(key, 0)
            sequence = envelope["producer_seq"]
            missing = []
            if event_domain(envelope) == "DIAGNOSTIC_EVENT" and sequence > previous + 1:
                missing = [[previous + 1, sequence - 1]]
            now = self.wall_time()
            if not math.isfinite(now) or now < 0:
                raise SpoolError("INPUT_INVALID")
            row = {"journal_seq": self._next_seq, "event_id": envelope["event_id"],
                   "payload_sha256": payload_sha256(envelope), "envelope": envelope,
                   "ordering": {"domain": event_domain(envelope), "late": sequence < previous, "missing_ranges": missing},
                   "recorded_at_utc": now, "retention_not_before_utc": now + self.limits.retention_seconds}
            row["record_sha256"] = _row_hash(row)
            encoded = canonical_json(row) + b"\n"
            receipt_bytes = len(canonical_json(_receipt(envelope, "ACK_DURABLE", row=row)))
            used, event_ids = _disk_usage(self.root)
            critical = is_critical(envelope)
            byte_limit = self.limits.max_bytes - (0 if critical else self.limits.critical_reserved_bytes)
            record_limit = self.limits.max_records - (0 if critical else self.limits.critical_reserved_records)
            adds_record = envelope["event_id"] not in event_ids
            if (adds_record and len(event_ids) + 1 > record_limit) or used + len(encoded) + 2 * receipt_bytes + 2048 > byte_limit:
                self._stats["rejected"] += 1
                self._loss(envelope)
                return _receipt(envelope, "REJECTED", failure="SPOOL_BACKPRESSURE")
            try:
                self._fault("journal_before_write")
                # Two writes make a deterministic actual torn-record fault point.
                split = max(1, len(encoded) // 2)
                self._handle.write(encoded[:split])
                self._fault("journal_mid_write")
                self._handle.write(encoded[split:])
                self._fault("journal_before_flush")
                self._handle.flush()
                self._fault("journal_before_fsync")
                os.fsync(self._handle.fileno())
                self._fault("journal_after_fsync")
                self._rows.append(row)
                self._index(row)
                self._next_seq += 1
                self._stats["accepted"] += 1
                self._persist_current(clean=False)
                self._fault("journal_before_ack")
                return self._publish_ack(row)
            except BaseException:
                # No successful ACK is fabricated.  Keep the original partial
                # segment; next writer recovers only verified complete rows.
                self._faulted = True
                self._uncertain.add(stream_key(envelope))
                raise

    def _sync_all_segments(self) -> None:
        self._handle.flush()
        for path in (self.root / "journal").glob("*.jsonl"):
            # On Windows, _commit() on the CRT fd requires write access.
            # r+b opens an EXISTING segment without truncation/append and
            # fsyncs its bytes; no ACK is minted on any open/fsync failure.
            with path.open("r+b") as handle:
                os.fsync(handle.fileno())

    def process_pending(self) -> list[dict[str, Any]]:
        """Poll ready files once, waiting at most two seconds across caller polls.

        Missing-sequence waiting never sleeps and never runs on a Web operation
        thread. Liveness does not fill diagnostic gaps. Invalid raw input is not
        copied into errors, receipts or evidence; its ready file remains for an
        authorized operator to inspect/remove and is never claimed accepted.
        """
        with self._mutex:
            self._check_open()
            pending: list[tuple[Path, dict[str, Any]]] = []
            for directory in ("inbox", "heartbeat"):
                for path in sorted((self.root / directory).glob("*.ready.json")):
                    try:
                        wrapper = strict_json(_bounded_read(path, self.limits.event_max_bytes + 256), max_bytes=self.limits.event_max_bytes + 256)
                        _keys(wrapper, {"payload_sha256", "envelope"})
                        envelope = validate_envelope(wrapper["envelope"], max_bytes=self.limits.event_max_bytes)
                        if wrapper["payload_sha256"] != payload_sha256(envelope):
                            raise SpoolError("INPUT_INVALID")
                        pending.append((path, envelope))
                    except (OSError, SpoolError, KeyError, TypeError):
                        self._stats["rejected"] += 1
                        with _FileLock(self.root / "quota.lock"):
                            self._persist_current(clean=False)
            pending.sort(key=lambda item: (_sequence_key(item[1]), item[0].name))
            results = []
            for path, envelope in pending:
                seq_key = _sequence_key(envelope)
                instance_domain = seq_key[:2]
                previous = self._high_water.get(instance_domain, 0)
                if event_domain(envelope) == "DIAGNOSTIC_EVENT" and envelope["producer_seq"] > previous + 1:
                    started = self._gap_started.setdefault(instance_domain, self.monotonic())
                    if self.monotonic() - started < self.limits.gap_wait_seconds:
                        continue
                result = self.append(envelope)
                results.append(result)
                self._gap_started.pop(instance_domain, None)
                # Keep a latest liveness sample available when the journal is
                # backpressured. A producer can replace it while append runs;
                # compare under the same publication lock before removing only
                # the acknowledged sample, never a newer replacement.
                if event_domain(envelope) == "LIVENESS_SAMPLE":
                    if result["stage"] == "ACK_DURABLE":
                        with _FileLock(self.root / "quota.lock"):
                            try:
                                current = strict_json(_bounded_read(path, self.limits.event_max_bytes + 256),
                                                      max_bytes=self.limits.event_max_bytes + 256)
                            except FileNotFoundError:
                                continue
                            if current == {"payload_sha256": payload_sha256(envelope), "envelope": envelope}:
                                path.unlink()
                else:
                    # Ready bytes already exist in the journal on ACK; rejected
                    # diagnostics have no durability promise. Never delete on I/O error.
                    path.unlink()
            return results

    def prune_expired(self, *, now_utc: float | None = None,
                      protected_event_ids: frozenset[str] = frozenset()) -> dict[str, int]:
        """Explicit retention boundary; remove only whole expired closed streams.

        Requires clean producer checkpoints, no unprocessed messages, no unknown
        history and no protected event IDs (e.g. active L06 S0/incident pins).
        Consumers supply pins; this L05 module does not inspect/create incidents.
        Clock rollback never shortens retention.  Uncertain post-crash streams
        cannot be automatically pruned by this conservative Candidate.
        """
        with self._mutex, _FileLock(self.root / "quota.lock"):
            self._check_open()
            now = self.wall_time() if now_utc is None else now_utc
            if type(now) not in (int, float) or not math.isfinite(now):
                raise SpoolError("INPUT_INVALID")
            if any(next((self.root / name).glob("*.ready.json"), None) is not None for name in ("inbox", "heartbeat")):
                return {"segments_removed": 0, "records_removed": 0}
            data = read_journal(self.root)
            candidates: list[Path] = []
            removed: set[str] = set()
            clean_streams = set()
            for row in self._rows:
                key = stream_key(row["envelope"])
                history = read_history(self.root, stream=key, now_utc=now)
                if history["history_coverage"]["loss_count_exact"]:
                    clean_streams.add(key)
            for path in sorted((self.root / "journal").glob("*.jsonl")):
                if path == self._segment:
                    continue
                try:
                    rows = [strict_json(line, max_bytes=65536) for line in path.read_bytes().splitlines()]
                    if not rows:
                        candidates.append(path)
                        continue
                    for row in rows:
                        _validate_row(row)
                    if any(row["retention_not_before_utc"] > now or row["event_id"] in protected_event_ids or stream_key(row["envelope"]) not in clean_streams for row in rows):
                        continue
                    candidates.append(path)
                    removed.update(row["event_id"] for row in rows)
                except (OSError, SpoolError):
                    continue
            if candidates:
                # First persist a conservative current state before destructive
                # expiration. No unexpired key is ever dropped to admit traffic.
                for row in data["rows"]:
                    if row["event_id"] in removed:
                        key = _sequence_key(row["envelope"])[:2]
                        self._retired_high_water[key] = max(self._retired_high_water.get(key, 0), row["envelope"]["producer_seq"])
                        self._retired_journal_seq = max(self._retired_journal_seq, row["journal_seq"])
                self._prune_intent = {"segments": sorted(path.name for path in candidates), "event_ids": sorted(removed)}
                self._prune_journal_sizes = {path.name: path.stat().st_size for path in candidates}
                self._persist_current(clean=False)
                self._fault("prune_after_intent")
                for path in candidates:
                    path.unlink()
                    self._fault("prune_after_unlink")
                for event_id in removed:
                    (self.root / "receipts" / (event_id + ".json")).unlink(missing_ok=True)
                self._rows = [row for row in self._rows if row["event_id"] not in removed]
                self._id_ledger.clear()
                self._seq_ledger.clear()
                self._stream_snapshot.clear()
                # Keep instance high-water: expiry does not authorize a seq reset.
                for row in self._rows:
                    self._index(row)
                self._prune_intent = {"segments": [], "event_ids": []}
                self._prune_journal_sizes = {}
                self._persist_current(clean=False)
            return {"segments_removed": len(candidates), "records_removed": len(removed)}

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                try:
                    with _FileLock(self.root / "quota.lock"):
                        self._handle.flush()
                        os.fsync(self._handle.fileno())
                        if not self._faulted:
                            self._persist_current(clean=True)
                finally:
                    self._handle.close()
                    self._lock.close()
                    self._closed = True

    def __enter__(self) -> "JournalSpool":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
