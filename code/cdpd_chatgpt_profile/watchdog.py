"""L07 deterministic liveness and one-attempt decisions; no I/O or Web authority.

The caller supplies its *own* receiving monotonic time.  Envelope UTC, file
mtime and another process's clock never participate in a liveness decision.
The process/transport adapter must authenticate an observation before creating
a corresponding trigger.  An enum, persisted heartbeat or event receipt is
not authentication, an operation-start permission, or a fresh keeper anchor.

Persistent spawn admission and worker ownership belong to ``supervisor`` and
``owned_process``.  The in-memory deduper here cannot prove restart safety.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Any, Iterable

from .spool import MAX_SAFE_INTEGER, SpoolError, validate_envelope


PRODUCER_INTERVAL_SECONDS = 2.0
HEARTBEAT_EXPIRY_SECONDS = 10.0
MAX_SUPERVISOR_POLL_SECONDS = 0.25
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class WatchdogError(ValueError):
    """Fixed-code errors; input values and exception text are never included."""

    def __init__(self, code: str = "INPUT_INVALID") -> None:
        allowed = {"INPUT_INVALID", "MONOTONIC_INVALID", "MONOTONIC_FAILED",
                   "DEDUPE_CAPACITY", "SCOPE_MISMATCH"}
        self.code = code if code in allowed else "INPUT_INVALID"
        super().__init__(self.code)


def _text(value: Any, *, maximum: int = 256) -> None:
    if (type(value) is not str or not value or not value.strip()
            or len(value) > maximum or _CONTROL.search(value)):
        raise WatchdogError()


def _monotonic(value: Any) -> float:
    if type(value) not in (float, int):
        raise WatchdogError("MONOTONIC_INVALID")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise WatchdogError("MONOTONIC_INVALID") from None
    if not math.isfinite(number) or number < 0:
        raise WatchdogError("MONOTONIC_INVALID")
    return number


@dataclass(frozen=True)
class OperationScope:
    """Exact immutable identity of the one registered liveness stream.

    ``binding_sha256`` and keeper ownership are checked by the admission
    boundary against its original binding bytes; this key does not replace
    that check.  Opaque IDs are preserved without trimming or normalizing.
    """

    run_id: str
    operation_id: str
    binding_id: str
    control_generation: int
    supervisor_instance_id: str
    producer_id: str
    producer_instance_id: str

    def __post_init__(self) -> None:
        _text(self.run_id, maximum=1024)
        for value in (self.operation_id, self.producer_id, self.producer_instance_id):
            _text(value)
        if (type(self.binding_id) is not str or not _UUID.fullmatch(self.binding_id)
                or type(self.supervisor_instance_id) is not str
                or not _UUID.fullmatch(self.supervisor_instance_id)
                or type(self.control_generation) is not int
                or not 1 <= self.control_generation <= MAX_SAFE_INTEGER):
            raise WatchdogError()

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any], *,
                      supervisor_instance_id: str,
                      run_id: str | None = None) -> "OperationScope":
        """Decode a validated envelope; owner ID is supplied by current owner.

        No owner/keeper identity is recovered from an untrusted heartbeat.
        An explicit registration ``run_id`` supplies an absent correlation;
        if both are present they must match.  A missing ID is never inferred
        from the producer instance, which would change the dedupe namespace.
        """
        try:
            value = validate_envelope(envelope)
            event = value["event"]
            declared_run = event["correlation"].get("run_id")
            if run_id is not None and declared_run is not None and run_id != declared_run:
                raise WatchdogError("SCOPE_MISMATCH")
            selected_run = run_id if run_id is not None else declared_run
            return cls(run_id=selected_run,
                       operation_id=event["operation_id"],
                       binding_id=value["binding_id"],
                       control_generation=value["control_generation"],
                       supervisor_instance_id=supervisor_instance_id,
                       producer_id=event["producer_id"],
                       producer_instance_id=event["producer_instance_id"])
        except (SpoolError, KeyError, TypeError, ValueError):
            raise WatchdogError() from None

    @property
    def dedupe_key(self) -> tuple[str, str, str, int]:
        """Capture identity excludes producer and Supervisor instance IDs."""
        return (self.run_id, self.operation_id, self.binding_id,
                self.control_generation)

    @property
    def operation_key(self) -> tuple[str, str]:
        """Conservative once-per-run/operation admission, across generations."""
        return (self.run_id, self.operation_id)


class HeartbeatDisposition(str, Enum):
    FRESH = "FRESH"
    FRESH_DIAGNOSTIC_ONLY = "FRESH_DIAGNOSTIC_ONLY"
    REPLAY = "REPLAY"
    STALE = "STALE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    INVALID_SEQUENCE = "INVALID_SEQUENCE"


class TriggerKind(str, Enum):
    HEARTBEAT_EXPIRED = "HEARTBEAT_EXPIRED"
    CONSUMER_EXITED = "CONSUMER_EXITED"
    FAILURE_EVENT = "FAILURE_EVENT"
    CHROME_EXITED = "CHROME_EXITED"
    CDP_DISCONNECTED = "CDP_DISCONNECTED"
    WORKER_FAILED = "WORKER_FAILED"
    WORKER_DEADLINE = "WORKER_DEADLINE"
    CLOCK_OR_KEEPER_LOST = "CLOCK_OR_KEEPER_LOST"


class ObservationSource(str, Enum):
    LOCAL_MONOTONIC = "LOCAL_MONOTONIC"
    CONFIRMED_PROCESS_EXIT = "CONFIRMED_PROCESS_EXIT"
    AUTHENTICATED_TRANSPORT = "AUTHENTICATED_TRANSPORT"
    VERIFIED_DURABLE_EVENT = "VERIFIED_DURABLE_EVENT"
    OWNED_WORKER = "OWNED_WORKER"
    TRUSTED_KEEPER = "TRUSTED_KEEPER"


_OBSERVATION_SOURCES = {
    TriggerKind.HEARTBEAT_EXPIRED: ObservationSource.LOCAL_MONOTONIC,
    TriggerKind.CONSUMER_EXITED: ObservationSource.CONFIRMED_PROCESS_EXIT,
    TriggerKind.FAILURE_EVENT: ObservationSource.VERIFIED_DURABLE_EVENT,
    TriggerKind.CHROME_EXITED: ObservationSource.CONFIRMED_PROCESS_EXIT,
    TriggerKind.CDP_DISCONNECTED: ObservationSource.AUTHENTICATED_TRANSPORT,
    TriggerKind.WORKER_FAILED: ObservationSource.OWNED_WORKER,
    TriggerKind.WORKER_DEADLINE: ObservationSource.OWNED_WORKER,
    TriggerKind.CLOCK_OR_KEEPER_LOST: ObservationSource.TRUSTED_KEEPER,
}
_METADATA_ONLY = frozenset({TriggerKind.CHROME_EXITED, TriggerKind.CDP_DISCONNECTED,
                          TriggerKind.WORKER_FAILED, TriggerKind.WORKER_DEADLINE,
                          TriggerKind.CLOCK_OR_KEEPER_LOST})
# This is only the diagnostic-action priority *within one observation tick*.
# It cannot rewrite the temporal first cause, already committed evidence, or a
# worker's authenticated first-cause receipt.  Keep all observations separately.
_ACTION_PRIORITY = {
    TriggerKind.CLOCK_OR_KEEPER_LOST: 80,
    TriggerKind.CHROME_EXITED: 70,
    TriggerKind.CDP_DISCONNECTED: 70,
    TriggerKind.WORKER_DEADLINE: 60,
    TriggerKind.WORKER_FAILED: 60,
    TriggerKind.FAILURE_EVENT: 30,
    TriggerKind.CONSUMER_EXITED: 20,
    TriggerKind.HEARTBEAT_EXPIRED: 10,
}


@dataclass(frozen=True)
class WatchdogTrigger:
    scope: OperationScope
    kind: TriggerKind
    observed_monotonic: float
    source: ObservationSource

    def __post_init__(self) -> None:
        if (type(self.scope) is not OperationScope or type(self.kind) is not TriggerKind
                or type(self.source) is not ObservationSource
                or self.source is not _OBSERVATION_SOURCES[self.kind]):
            raise WatchdogError()
        _monotonic(self.observed_monotonic)

    @property
    def reason_code(self) -> str:
        # FAILURE_EVENT retains its original envelope outside this selector.
        # Do not replace it with a fabricated provider/identity diagnosis.
        return self.kind.value

    @property
    def metadata_only(self) -> bool:
        return self.kind in _METADATA_ONLY


def select_trigger(triggers: Iterable[WatchdogTrigger]) -> WatchdogTrigger | None:
    """Choose this tick's action, without claiming an inferred first cause."""
    values = tuple(triggers)
    if not values:
        return None
    if any(type(value) is not WatchdogTrigger for value in values):
        raise WatchdogError()
    if any(value.scope != values[0].scope for value in values[1:]):
        raise WatchdogError("SCOPE_MISMATCH")
    return min(values, key=lambda value: (-_ACTION_PRIORITY[value.kind],
                                         value.observed_monotonic, value.kind.value))


class HeartbeatTracker:
    """New-sequence receiver clock anchor with sticky diagnostic-only state.

    The initial registration time supplies the first 10-second grace window.
    A heartbeat arriving after an elapsed window cannot erase that expiration
    even if the monitoring loop has not called ``poll`` yet.  Later fresh
    samples can be recorded for liveness telemetry, but never resume work.
    """

    def __init__(self, scope: OperationScope, armed_at_monotonic: float, *,
                 expiry_seconds: float = HEARTBEAT_EXPIRY_SECONDS) -> None:
        if type(scope) is not OperationScope:
            raise WatchdogError()
        now = _monotonic(armed_at_monotonic)
        expiry = _monotonic(expiry_seconds)
        if not 0 < expiry <= HEARTBEAT_EXPIRY_SECONDS:
            raise WatchdogError()
        self._scope = scope
        self._expiry_seconds = expiry
        self._last_now = now
        self._last_fresh_monotonic = now
        self._last_sequence = 0
        self._expiry_detected_at: float | None = None
        self._expiry_boundary: float | None = None
        self._expiry_emitted = False
        self._diagnostic_only = False
        self._clock_failed = False

    @property
    def scope(self) -> OperationScope:
        return self._scope

    @property
    def diagnostic_only(self) -> bool:
        return self._diagnostic_only

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def last_received_monotonic(self) -> float:
        return self._last_fresh_monotonic

    @property
    def expires_at_monotonic(self) -> float:
        if self._expiry_boundary is not None:
            return self._expiry_boundary
        return self._last_fresh_monotonic + self._expiry_seconds

    def _advance(self, now: Any) -> float:
        if self._clock_failed:
            raise WatchdogError("MONOTONIC_FAILED")
        try:
            value = _monotonic(now)
            if value < self._last_now:
                raise WatchdogError("MONOTONIC_INVALID")
        except WatchdogError:
            self._clock_failed = self._diagnostic_only = True
            raise
        self._last_now = value
        if self._expiry_detected_at is None and value >= self.expires_at_monotonic:
            self._expiry_boundary = self.expires_at_monotonic
            self._expiry_detected_at = value
            self._diagnostic_only = True
        return value

    def observe(self, scope: OperationScope, sequence: int,
                received_monotonic: float) -> HeartbeatDisposition:
        now = self._advance(received_monotonic)
        if type(scope) is not OperationScope or scope != self._scope:
            return HeartbeatDisposition.SCOPE_MISMATCH
        if type(sequence) is not int or not 1 <= sequence <= MAX_SAFE_INTEGER:
            return HeartbeatDisposition.INVALID_SEQUENCE
        if sequence == self._last_sequence:
            return HeartbeatDisposition.REPLAY
        if sequence < self._last_sequence:
            return HeartbeatDisposition.STALE
        self._last_sequence = sequence
        self._last_fresh_monotonic = now
        return (HeartbeatDisposition.FRESH_DIAGNOSTIC_ONLY if self._diagnostic_only
                else HeartbeatDisposition.FRESH)

    def poll(self, now: float) -> WatchdogTrigger | None:
        self._advance(now)
        if self._expiry_detected_at is None or self._expiry_emitted:
            return None
        self._expiry_emitted = True
        return WatchdogTrigger(self._scope, TriggerKind.HEARTBEAT_EXPIRED,
                               self._expiry_detected_at, ObservationSource.LOCAL_MONOTONIC)

    def latch_diagnostic_only(self, trigger: WatchdogTrigger) -> None:
        if type(trigger) is not WatchdogTrigger:
            raise WatchdogError()
        if trigger.scope != self._scope:
            raise WatchdogError("SCOPE_MISMATCH")
        # An authenticated observation can arrive late from the writer/worker.
        # Its original timestamp is evidence, not a new receiver-clock sample.
        self._diagnostic_only = True


class OncePerOperation:
    """Bounded, no-release in-memory dedupe; Supervisor must persist first.

    A different producer, generation or Supervisor cannot create a second
    attempt for the same run/operation.  Restart recovery is deliberately not
    inferred from this volatile set; durable attempt records remain required.
    """

    def __init__(self, *, max_operations: int = 4096) -> None:
        if type(max_operations) is not int or not 1 <= max_operations <= 4096:
            raise WatchdogError()
        self._max_operations = max_operations
        self._claimed: set[tuple[str, str]] = set()

    def claim(self, scope: OperationScope) -> bool:
        if type(scope) is not OperationScope:
            raise WatchdogError()
        if scope.operation_key in self._claimed:
            return False
        if len(self._claimed) >= self._max_operations:
            raise WatchdogError("DEDUPE_CAPACITY")
        self._claimed.add(scope.operation_key)
        return True
