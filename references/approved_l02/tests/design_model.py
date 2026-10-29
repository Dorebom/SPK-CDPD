"""Finite design oracles ONLY. Not a CDP client, durable writer, or product runtime.

These pure functions make the L02 decision rules testable before implementation.
They do NOT prove that a real fsync, Windows process kill, or live identity check
has happened; those belong to FLT product tests (all NOT_RUN here).
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def strict_json(raw: bytes, maximum: int = 32768) -> Any:
    if len(raw) > maximum:
        raise ValueError("INPUT_TOO_LARGE")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result
    def invalid_constant(_: str) -> None:
        raise ValueError("NONFINITE_JSON")
    def finite_float(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("NONFINITE_JSON")
        return value
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid_constant, parse_float=finite_float)


def ack_stage(*, memory: bool = False, ready: bool = False,
              journal_written: bool = False, journal_fsynced: bool = False,
              receipt_published: bool = False, receipt_hash_matches: bool = False,
              s0_fsynced: bool = False, journal_record_hash_matches: bool = False,
              operation_start: bool = False, current_written: bool = False,
              current_fsynced: bool = False, current_published: bool = False,
              current_hash_matches: bool = False, generation_matches: bool = False,
              admission_timely: bool = False) -> str:
    durable = (journal_written and journal_fsynced and receipt_published
               and receipt_hash_matches and journal_record_hash_matches)
    if operation_start:
        durable = durable and all((current_written, current_fsynced, current_published,
                                   current_hash_matches, generation_matches, admission_timely))
    if durable and s0_fsynced:
        return "CAPTURE_ACCEPTED"
    if durable:
        return "ACK_DURABLE"
    if ready:
        return "LOCAL_PUBLISHED"
    if memory:
        return "MEMORY_ACCEPTED"
    return "UNKNOWN"


@dataclass
class LedgerModel:
    """Models an already-fsynced journal prefix, not filesystem persistence."""
    rows: dict[str, tuple[str, int]] = field(default_factory=dict)
    sequences: dict[tuple[str, int], str] = field(default_factory=dict)

    def accept_committed(self, event_id: str, payload: Any,
                         producer: str, sequence: int) -> tuple[str, int | None]:
        digest = hashlib.sha256(canonical(payload)).hexdigest()
        previous = self.rows.get(event_id)
        if previous:
            return ("DUPLICATE", previous[1]) if previous[0] == digest else ("CONFLICT", None)
        if (producer, sequence) in self.sequences:
            return "CONFLICT", None
        journal_seq = len(self.rows) + 1
        self.rows[event_id] = (digest, journal_seq)
        self.sequences[(producer, sequence)] = event_id
        return "ACCEPTED", journal_seq


def classify_matrix(envelope: dict[str, Any], matrix: dict[str, Any]) -> dict[str, Any] | None:
    event = envelope["event"]
    matches = [rule for rule in matrix["rules"]
               if (rule["event_kind"], rule["failure_code"], rule["profile_reason"])
               == (event["kind"], event["failure_code"], envelope["profile"]["reason"])]
    return matches[0] if len(matches) == 1 else None


def capture_permission(*, policy: bool = True, binding: bool = True,
                       same_epoch: bool = True, same_target: bool = True,
                       same_origin: bool = True, single_page: bool = True,
                       same_generation: bool = True, lease: bool = True,
                       keeper: bool = True, clock_continuity: bool = True,
                       known_cdp_disconnect: bool = False,
                       consumer_alive: bool = True) -> bool:
    # Consumer death alone does not imply Chrome death; independent keeper required.
    _ = consumer_alive
    return all((policy, binding, same_epoch, same_target, same_origin,
                single_page, same_generation, lease, keeper, clock_continuity)) and not known_cdp_disconnect


def capture_result(*, input_valid: bool = True, storage: bool = True,
                   policy: bool = True, precheck: bool = True,
                   postcheck: bool = True, worker_normal: bool = True,
                   privacy: bool = True, stages_complete: bool = True,
                   coverage_complete: bool = True, history_complete: bool = True,
                   failure_latched: bool = False) -> tuple[str, bool]:
    """Return status and whether any staged structure may be published."""
    if not input_valid:
        return "INPUT_REJECTED", False
    if not storage:
        return "STORAGE_FAILED", False
    if not policy:
        return "REJECTED", False
    if failure_latched or not all((precheck, postcheck, worker_normal, privacy, stages_complete)):
        return "PARTIAL", False
    if not coverage_complete or not history_complete:
        return "PARTIAL", True
    return "COMPLETE", True


def report_verdict(*, unknown_conditions: list[str] | None = None,
                   required_failure: bool = False, significant_drift: bool = False) -> str:
    if unknown_conditions:
        return "UNKNOWN"
    if required_failure:
        return "BREAKING"
    if significant_drift:
        return "REVIEW"
    return "PASS"


def transition(contract: dict[str, Any], state: str, event: str) -> str:
    if state in contract["terminal_states"]:
        return "DENY"
    interrupts = sorted(contract["interrupts"], key=lambda x: x["priority"])
    for row in interrupts:
        if event == row["event"] and state in row["applies_to"]:
            return row["to"]
    for row in contract["transitions"]:
        if row["from"] == state and row["event"] == event:
            return row["to"]
    return "DENY"


def receive_heartbeat(last_seq: int, last_received: float, incoming_seq: int,
                      now: float) -> tuple[int, float]:
    return (incoming_seq, now) if incoming_seq > last_seq else (last_seq, last_received)


def heartbeat_expired(now: float, last_received: float, threshold: float = 10) -> bool:
    return now - last_received >= threshold


def budget_allows(sizes: list[int], maximum: int) -> bool:
    return all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in sizes) and sum(sizes) <= maximum
