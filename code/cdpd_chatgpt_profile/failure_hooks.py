"""L05 metadata-only hooks and bounded asynchronous producer history.

``try_emit`` is an EventSink for the *unchanged* accepted L04 InstrumentedFacade.
It performs bounded in-memory work only. A True result is MEMORY_ACCEPTED, never
an operation-admission or durable ACK. File publication happens on an explicitly
started writer thread (or an explicit ``drain_once`` lifecycle/test boundary).
No function in this module calls a Web provider, a Target resolver, or a capture
worker. Opaque caller IDs are preserved; they are not sanitised or reinterpreted.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from threading import Event, Lock, Thread
import time
from typing import Any, Callable
from uuid import UUID, uuid4

from cdpd import DiagnosticEvent

from .spool import canonical_json, event_domain, stream_key, validate_envelope


MAX_SAFE_INTEGER = 9007199254740991

# Explicit unambiguous adapter mappings. Other ambiguous generic pairs require
# record(..., reason=...) from a trusted observing caller; never guess a Target
# failure from a locator failure or vice versa.
_DEFAULT_REASONS = {
    ('ACTION_STARTED', 'NONE'): 'OPERATION_ARMED',
    ('ACTION_FINISHED', 'NONE'): 'OPERATION_FINISHED',
    ('ACTION_FAILED', 'UNKNOWN'): 'PROVIDER_FAILURE_UNCLASSIFIED',
    ('ACTION_FAILED', 'DISCONNECTED'): 'PROVIDER_DISCONNECTED',
    ('ACTION_FAILED', 'TIMEOUT'): 'ACTION_TIMEOUT',
    ('ACTION_FAILED', 'NOT_INTERACTABLE'): 'ELEMENT_NOT_INTERACTABLE',
    ('ACTION_FAILED', 'RECONCILE_REQUIRED'): 'SUBMISSION_UNCERTAIN',
    ('ACTION_FAILED', 'POSTCONDITION_FAILED'): 'POSTCONDITION_FAILED',
    ('OBSERVATION_FAILED', 'UNKNOWN'): 'OBSERVATION_UNDETERMINED',
    ('HEARTBEAT', 'NONE'): 'HEARTBEAT_SAMPLE',
}


def _positive_int(value: Any, maximum: int, name: str, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(name)


@dataclass(frozen=True)
class HookLimits:
    """Production defaults pinned to L02; smaller limits support local fixtures."""
    queue_total: int = 256
    queue_critical_reserved: int = 16
    history_seconds: float = 120.0
    history_max_events: int = 1000
    history_max_bytes: int = 1048576
    event_max_bytes: int = 32768
    stream_capacity: int = 4096

    def __post_init__(self) -> None:
        _positive_int(self.queue_total, 256, 'QUEUE_LIMIT')
        _positive_int(self.queue_critical_reserved, min(16, self.queue_total),
                      'CRITICAL_RESERVE', minimum=0)
        _positive_int(self.history_max_events, 1000, 'HISTORY_EVENT_LIMIT')
        _positive_int(self.history_max_bytes, 1048576, 'HISTORY_BYTE_LIMIT')
        _positive_int(self.event_max_bytes, 32768, 'EVENT_BYTE_LIMIT')
        _positive_int(self.stream_capacity, 4096, 'STREAM_LIMIT')
        if (isinstance(self.history_seconds, bool) or
            not isinstance(self.history_seconds, (int, float)) or
            not math.isfinite(self.history_seconds) or not 0 < self.history_seconds <= 120):
            raise ValueError('HISTORY_TIME_LIMIT')


@dataclass(frozen=True)
class BindingContext:
    """Metadata snapshot supplied by the owning caller; not binding authority."""
    binding_id: str | None = None
    binding_sha256: str | None = None
    control_generation: int = 0

    def __post_init__(self) -> None:
        _positive_int(self.control_generation, MAX_SAFE_INTEGER, 'GENERATION', minimum=0)
        if self.binding_id is None:
            if self.binding_sha256 is not None or self.control_generation != 0:
                raise ValueError('BINDING_CONTEXT')
        else:
            try:
                parsed = UUID(self.binding_id)
            except (TypeError, ValueError, AttributeError):
                raise ValueError('BINDING_CONTEXT') from None
            if (parsed.version != 4 or str(parsed) != self.binding_id or
                not isinstance(self.binding_sha256, str) or len(self.binding_sha256) != 64 or
                any(c not in '0123456789abcdef' for c in self.binding_sha256) or
                self.control_generation < 1):
                raise ValueError('BINDING_CONTEXT')


@dataclass
class _Stream:
    loss: int = 0
    exact: bool = True
    diagnostic_high_water: int = 0
    liveness_high_water: int = 0
    history_omitted: int = 0
    sampled: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class FailureHooks:
    """One producer-instance lifecycle, with explicit metadata stream changes.

    Construct/start/close outside Web operation calls. ``publisher`` should be a
    FileMailbox.publish bound method. Pass its publish_checkpoint bound method as
    ``checkpoint_publisher`` to record dirty/clean producer lifecycle markers.
    A missing checkpoint deliberately prevents a spool reader proving completeness.

    The caller uses exactly the same producer_id/producer_instance_id on this sink
    and InstrumentedFacade. heartbeat requires the current caller-owned operation
    ID; this object never creates an operation ID. IDs and opaque correlation are
    a trusted-caller contract, not an arbitrary-string secret detector.
    """
    def __init__(self, publisher: Callable[[dict], dict], *, producer_id: str,
                 producer_instance_id: str, binding: BindingContext | None = None,
                 checkpoint_publisher: Callable[[dict], Any] | None = None,
                 limits: HookLimits | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], str] = _utc_now) -> None:
        if not callable(publisher):
            raise TypeError('PUBLISHER_REQUIRED')
        if checkpoint_publisher is not None and not callable(checkpoint_publisher):
            raise TypeError('CHECKPOINT_PUBLISHER_REQUIRED')
        # Validate identities without emitting a record or manufacturing a real
        # operation ID. Identity validation is entirely in memory.
        for value in (producer_id, producer_instance_id):
            if (not isinstance(value, str) or not value.strip() or len(value) > 256 or
                any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError('PRODUCER_ID_INVALID')
        self.producer_id = producer_id
        self.producer_instance_id = producer_instance_id
        self._publisher = publisher
        self._checkpoint_publisher = checkpoint_publisher
        self.limits = limits or HookLimits()
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._binding = binding or BindingContext()
        if not isinstance(self._binding, BindingContext):
            raise TypeError('BINDING_CONTEXT_REQUIRED')
        self._lock = Lock()
        self._writer_lock = Lock()
        self._queue: deque[dict] = deque()
        self._heartbeat: dict | None = None
        self._history: deque[tuple[float, tuple, int, dict]] = deque()
        self._history_bytes = 0
        self._diagnostic_seq = 0
        self._liveness_seq = 0
        self._streams: dict[tuple, _Stream] = {self._current_key(): _Stream()}
        self._closed = False
        self._contention_unknown = False
        self._wake = Event()
        self._stop = Event()
        self._thread: Thread | None = None
        self._inflight = False
        self._inflight_diagnostic = False
        self._dirty_written: set[tuple] = set()
        self._counts = {'memory_accepted': 0, 'published': 0, 'queue_dropped': 0,
                        'publish_failed': 0, 'invalid_rejected': 0,
                        'heartbeat_coalesced': 0, 'heartbeat_publish_failed': 0,
                        'checkpoint_failed': 0}

    def _current_key(self) -> tuple:
        b = self._binding
        return (self.producer_id, self.producer_instance_id,
                b.binding_id, b.binding_sha256, b.control_generation)

    def _lose(self, state: _Stream, *, exact: bool = True) -> None:
        if state.loss < MAX_SAFE_INTEGER:
            state.loss += 1
        else:
            exact = False
        state.exact = state.exact and exact

    def set_binding(self, binding: BindingContext) -> None:
        """Explicit lifecycle metadata change; never performs or authorises Preflight.

        Domain sequences keep increasing across Binding/generation changes. Returning
        to a previously used stream restores its sticky loss/unknown state.
        """
        if not isinstance(binding, BindingContext):
            raise TypeError('BINDING_CONTEXT_REQUIRED')
        with self._lock:
            if self._closed:
                raise RuntimeError('HOOKS_CLOSED')
            key = (self.producer_id, self.producer_instance_id, binding.binding_id,
                   binding.binding_sha256, binding.control_generation)
            if key not in self._streams:
                if len(self._streams) >= self.limits.stream_capacity:
                    raise RuntimeError('STREAM_CAPACITY')
                self._streams[key] = _Stream()
            self._binding = binding

    def try_emit(self, event: DiagnosticEvent) -> bool:
        """Bounded in-memory acceptance. Never blocks on a mutex or storage."""
        return self.record(event)

    def record(self, event: DiagnosticEvent, *, reason: str | None = None,
               dispatch_state: str | None = None, ui_context: str = 'UNDETERMINED',
               document_marker: dict | None = None) -> bool:
        # Never wait for a writer/snapshot thread that owns the lock. Under
        # contention exact loss is unknowable; the next accepted carrier inherits
        # a sticky unknown flag. No raw input/exception is put in an error record.
        if not self._lock.acquire(blocking=False):
            self._contention_unknown = True
            return False
        try:
            if self._closed:
                return False
            state = self._streams[self._current_key()]
            if self._contention_unknown:
                for affected in self._streams.values():
                    affected.exact = False
                self._contention_unknown = False
            try:
                if type(event) is not DiagnosticEvent:
                    raise ValueError('INPUT_INVALID')
                if (event.producer_id != self.producer_id or
                    event.producer_instance_id != self.producer_instance_id):
                    raise ValueError('PRODUCER_MISMATCH')
                pair = (event.kind, event.failure_code)
                selected_reason = reason if reason is not None else _DEFAULT_REASONS.get(pair)
                if selected_reason is None:
                    raise ValueError('TRUSTED_REASON_REQUIRED')
                liveness = pair == ('HEARTBEAT', 'NONE') and selected_reason == 'HEARTBEAT_SAMPLE'
                # Assign a sequence before trying queue admission. A rejected
                # event never reuses a sequence. Heartbeat has its own domain.
                if liveness:
                    if self._liveness_seq >= MAX_SAFE_INTEGER:
                        raise ValueError('SEQUENCE_EXHAUSTED')
                    self._liveness_seq += 1
                    seq = self._liveness_seq
                    state.liveness_high_water = seq
                else:
                    if self._diagnostic_seq >= MAX_SAFE_INTEGER:
                        raise ValueError('SEQUENCE_EXHAUSTED')
                    self._diagnostic_seq += 1
                    seq = self._diagnostic_seq
                    state.diagnostic_high_water = seq
                selected_dispatch = dispatch_state
                if selected_dispatch is None:
                    selected_dispatch = ('NOT_DISPATCHED' if event.kind == 'ACTION_STARTED'
                                         else 'NOT_APPLICABLE' if liveness else 'UNKNOWN')
                # An unclassified action failure cannot prove that no dispatch
                # occurred. A known CONFIRMED/POSSIBLE state is retained.
                if (pair in {('ACTION_FAILED', 'UNKNOWN'), ('ACTION_FAILED', 'DISCONNECTED')}
                    and selected_dispatch not in {'POSSIBLE', 'CONFIRMED', 'UNKNOWN'}):
                    selected_dispatch = 'UNKNOWN'
                b = self._binding
                envelope = {'schema': 'cdpd.chatgpt-event-envelope/v1',
                            'event_id': str(uuid4()), 'producer_seq': seq,
                            'occurred_at_utc': self._utc_now(),
                            'binding_id': b.binding_id, 'binding_sha256': b.binding_sha256,
                            'control_generation': b.control_generation, 'event': event.to_dict(),
                            'profile': {'reason': selected_reason, 'dispatch_state': selected_dispatch,
                                        'ui_context': ui_context, 'document_marker': document_marker,
                                        'loss_lower_bound': state.loss, 'loss_count_exact': state.exact}}
                envelope = validate_envelope(envelope)
                size = len(canonical_json(envelope))
                if size > self.limits.event_max_bytes:
                    raise ValueError('EVENT_LIMIT')
                now = self._monotonic()
                if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
                    raise ValueError('CLOCK_INVALID')
            except BaseException:
                self._counts['invalid_rejected'] += 1
                # Invalid event identity cannot be reinterpreted as another
                # stream. Conservatively report uncertainty in this owner.
                state.exact = False
                return False
            critical = event.kind in {'ACTION_STARTED', 'ACTION_FAILED', 'OBSERVATION_FAILED'}
            if liveness:
                if self._heartbeat is not None:
                    self._counts['heartbeat_coalesced'] += 1
                self._heartbeat = envelope
            else:
                capacity = (self.limits.queue_total if critical else
                            self.limits.queue_total - self.limits.queue_critical_reserved)
                if len(self._queue) + int(self._inflight_diagnostic) >= capacity:
                    self._counts['queue_dropped'] += 1
                    self._lose(state)
                    return False
                self._queue.append(envelope)
            self._append_history(now, envelope, size)
            self._counts['memory_accepted'] += 1
            self._wake.set()
            return True
        finally:
            self._lock.release()

    def heartbeat(self, *, operation_id: str, correlation: dict[str, str] | None = None) -> bool:
        """One caller-driven heartbeat sample, no scheduler or watchdog implied."""
        try:
            event = DiagnosticEvent(self.producer_id, self.producer_instance_id, operation_id,
                                    'HEARTBEAT', correlation=correlation or {})
        except BaseException:
            return False
        return self.record(event)

    def observation_failed(self, *, operation_id: str, failure_code: str = 'UNKNOWN',
                           reason: str = 'OBSERVATION_UNDETERMINED',
                           dispatch_state: str = 'UNKNOWN',
                           correlation: dict[str, str] | None = None) -> bool:
        """Explicit trusted observation failure without exception text."""
        try:
            event = DiagnosticEvent(self.producer_id, self.producer_instance_id, operation_id,
                                    'OBSERVATION_FAILED', failure_code=failure_code,
                                    correlation=correlation or {})
        except BaseException:
            return False
        return self.record(event, reason=reason, dispatch_state=dispatch_state)

    def _append_history(self, now: float, envelope: dict, size: int) -> None:
        self._expire_history(now)
        key = stream_key(envelope)
        self._history.append((now, key, size, envelope))
        self._history_bytes += size
        while (len(self._history) > self.limits.history_max_events or
               self._history_bytes > self.limits.history_max_bytes):
            _, old_key, old_size, old = self._history.popleft()
            self._history_bytes -= old_size
            state = self._streams[old_key]
            state.sampled = True
            if event_domain(old) == 'DIAGNOSTIC_EVENT':
                state.history_omitted = min(MAX_SAFE_INTEGER, state.history_omitted + 1)

    def _expire_history(self, now: float) -> None:
        while self._history and now - self._history[0][0] > self.limits.history_seconds:
            _, _, size, _ = self._history.popleft()
            self._history_bytes -= size

    def history(self) -> dict:
        """Metadata snapshot for the current basic stream; never a Bundle PASS."""
        with self._lock:
            self._expire_history(self._monotonic())
            key = self._current_key()
            state = self._streams[key]
            if self._contention_unknown:
                for affected in self._streams.values():
                    affected.exact = False
                self._contention_unknown = False
            rows = [envelope for _, row_key, _, envelope in self._history if row_key == key]
            sequences = sorted({row['producer_seq'] for row in rows
                                if event_domain(row) == 'DIAGNOSTIC_EVENT'})
            gap = sum(max(0, b-a-1) for a, b in zip(sequences, sequences[1:]))
            lower = max(state.loss, state.history_omitted, gap,
                        max((row['profile']['loss_lower_bound'] for row in rows), default=0))
            exact = (state.exact and not self._contention_unknown and
                     all(row['profile']['loss_count_exact'] for row in rows) and
                     not (state.history_omitted and state.loss))
            # canonical roundtrip through validate_envelope creates defensive
            # copies: callers cannot mutate queued bytes through a snapshot.
            return {'events': [validate_envelope(row) for row in rows],
                    'history_dropped_count': lower,
                    'history_coverage': {'loss_lower_bound': lower, 'loss_count_exact': exact,
                                         'sampled': state.sampled,
                                         'complete': bool(rows) and lower == 0 and exact and
                                                     not state.sampled and gap == 0},
                    'diagnostic_internal_gap': gap,
                    'stage': 'MEMORY_ACCEPTED'}

    @property
    def stats(self) -> dict:
        with self._lock:
            result = dict(self._counts)
            result.update(queue_depth=len(self._queue), heartbeat_pending=self._heartbeat is not None,
                          history_events=len(self._history), history_bytes=self._history_bytes,
                          diagnostic_high_water=self._diagnostic_seq,
                          liveness_high_water=self._liveness_seq,
                          writer_inflight=self._inflight, closed=self._closed)
            return result

    def _checkpoint(self, key: tuple, *, clean: bool) -> dict:
        with self._lock:
            state = self._streams[key]
            if self._contention_unknown:
                state.exact = False
            return {'schema': 'cdpd.producer-checkpoint/v1', 'producer_id': key[0],
                    'producer_instance_id': key[1], 'binding_id': key[2],
                    'binding_sha256': key[3], 'control_generation': key[4],
                    'diagnostic_high_water': state.diagnostic_high_water,
                    'liveness_high_water': state.liveness_high_water,
                    'loss_lower_bound': state.loss, 'loss_count_exact': state.exact,
                    'clean_shutdown': clean}

    def _write_checkpoint(self, key: tuple, *, clean: bool) -> bool:
        if self._checkpoint_publisher is None:
            return False
        try:
            self._checkpoint_publisher(self._checkpoint(key, clean=clean))
            return True
        except BaseException:
            with self._lock:
                self._streams[key].exact = False
                self._counts['checkpoint_failed'] += 1
            return False

    def _drain_once(self) -> int:
        with self._writer_lock:
            return self._drain_owned()

    def _drain_owned(self) -> int:
        # Only one writer invocation is supported. Dequeue one at a time and
        # include the in-flight diagnostic in queue admission accounting, so a
        # stalled publisher does not double the approved memory-queue bound.
        count = 0
        for index in range(self.limits.queue_total + 1):
            with self._lock:
                if index == 0 and self._heartbeat is not None:
                    envelope = self._heartbeat
                    self._heartbeat = None
                elif self._queue:
                    envelope = self._queue.popleft()
                elif self._heartbeat is not None:
                    envelope = self._heartbeat
                    self._heartbeat = None
                else:
                    break
                self._inflight = True
                self._inflight_diagnostic = event_domain(envelope) == 'DIAGNOSTIC_EVENT'
            key = stream_key(envelope)
            if key not in self._dirty_written:
                if self._write_checkpoint(key, clean=False):
                    self._dirty_written.add(key)
            try:
                published = self._publisher(validate_envelope(envelope))
                if (not isinstance(published, dict) or
                    published.get('stage') not in {'LOCAL_PUBLISHED', 'ACK_DURABLE'} or
                    published.get('ack_purpose') != 'EVENT_DURABILITY'):
                    raise ValueError('PUBLICATION_NOT_CONFIRMED')
            except BaseException:
                with self._lock:
                    state = self._streams[key]
                    # It may have reached storage before the error. This is
                    # unknown loss, not proof that one event was lost; never
                    # inflate the known lower bound by counting that event.
                    state.exact = False
                    if event_domain(envelope) == 'DIAGNOSTIC_EVENT':
                        self._counts['publish_failed'] += 1
                    else:
                        self._counts['heartbeat_publish_failed'] += 1
            else:
                count += 1
                with self._lock:
                    self._counts['published'] += 1
            finally:
                with self._lock:
                    self._inflight = False
                    self._inflight_diagnostic = False
        return count

    def drain_once(self) -> int:
        """Explicit off-Web lifecycle/test boundary; may perform blocking file IO."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('WRITER_ALREADY_RUNNING')
        return self._drain_once()

    def start(self) -> None:
        """Start the dedicated writer; never called automatically by try_emit."""
        with self._lock:
            if self._closed:
                raise RuntimeError('HOOKS_CLOSED')
            if self._thread is not None:
                raise RuntimeError('WRITER_ALREADY_STARTED')
            self._thread = Thread(target=self._run, name='cdpd-l05-metadata-writer', daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait(0.25)
            self._wake.clear()
            self._drain_once()
            with self._lock:
                drained = not self._queue and self._heartbeat is None
            if self._stop.is_set() and drained:
                # An explicit close seals high-water counters after publication.
                # Any unknown/loss remains in the checkpoint; clean process exit
                # does not turn an uncertain stream back into complete history.
                for key in tuple(self._streams):
                    self._write_checkpoint(key, clean=True)
                return

    def close(self, *, timeout: float = 2.0) -> bool:
        """Bounded explicit lifecycle join, separate from every Web operation.

        True means the writer terminated; it does not mean all records are durable
        or loss-free. Inspect stats and independently verify journal receipts.
        A stuck publisher leaves the daemon alive and returns False at the deadline.
        """
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or timeout < 0 or timeout > 60):
            raise ValueError('CLOSE_TIMEOUT')
        with self._lock:
            if self._thread is None:
                self._thread = Thread(target=self._run, name='cdpd-l05-metadata-writer', daemon=True)
                self._thread.start()
            self._closed = True
            self._stop.set()
            self._wake.set()
            thread = self._thread
        thread.join(timeout)
        return not thread.is_alive()
