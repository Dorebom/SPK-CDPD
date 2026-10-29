"""Explicit live-owner bridge from accepted L03 to the L04 strict interface.

Create this bridge BEFORE the owner's explicit Preflight. It starts the clock
continuity keeper first, calls the unchanged L03 owner exactly once, and admits
only that live owner's current immutable record. An old JSON file, a positive
result dictionary, or an owner whose Preflight predates the keeper cannot admit
a Binding. This module implements no L05 spool, L06 Worker or L07 watchdog.
"""
from __future__ import annotations

from datetime import timedelta
import threading
from typing import Any
import weakref

from chatgpt_web_api.target_guard import (
    AuthoritySnapshot, BindingGuard, Clock, GuardError, ParsedBinding,
    SystemClock, _sample_clock, check_continuity, timestamp,
)

_REGISTRY: weakref.WeakKeyDictionary[Any, weakref.ReferenceType[L03BindingBridge]] = weakref.WeakKeyDictionary()
_REGISTRY_LOCK = threading.Lock()


class L03BindingBridge:
    """Single-consumer memory authority; all consumers share its generation.

    Accepted L03 has no public lease-reader API. This narrowly scoped bridge
    reads its existing lock/lease method and current_record; it does not replace
    or patch L03 code. This dependency is pinned to the accepted L03 source hash.
    No method can register an arbitrary serialized record.
    """

    def __init__(self, owner: Any, *, clock: Clock | None = None) -> None:
        if (not callable(getattr(owner, "preflight", None)) or
                not callable(getattr(owner, "_check_lease", None)) or
                not hasattr(owner, "_lock") or not hasattr(owner, "current_record") or
                not hasattr(owner, "state")):
            raise GuardError("BINDING_RECORD_INVALID")
        if owner.current_record is not None or owner.state != "UNBOUND":
            # No continuity assertion can retroactively cover that generation.
            raise GuardError("CLOCK_DISCONTINUITY")
        with _REGISTRY_LOCK:
            existing_ref = _REGISTRY.get(owner)
            if existing_ref is not None and existing_ref() is not None:
                raise GuardError("OWNER_BUSY")
            self._owner = owner
            self.clock: Clock = clock if clock is not None else SystemClock()
            self._created = _sample_clock(self.clock)
            self._lock = threading.RLock()
            self._record: ParsedBinding | None = None
            self._owner_record: Any = None
            self._generation = 0
            self._reason = "BINDING_REQUIRED"
            self._state = "UNBOUND"
            self._mode = "NORMAL"
            self._tombstones: dict[str, str] = {}
            self._anchor = self._created
            self._deadline: float | None = None
            self._guard_refs: list[weakref.ReferenceType[BindingGuard]] = []
            self._operation_token: object | None = None
            self._operation_thread: int | None = None
            self._operation_depth = 0
            self._claimed_operations: set[tuple[str, str]] = set()
            _REGISTRY[owner] = weakref.ref(self)

    @property
    def current_record(self) -> ParsedBinding | None:
        return self._record

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def generation(self) -> int:
        return self._generation

    def _owner_record_checked(self) -> Any:
        acquired = self._owner._lock.acquire(blocking=False)
        if not acquired:
            raise GuardError("OWNER_BUSY")
        try:
            if self._owner.state != "BOUND" or self._owner.current_record is None:
                raise GuardError("BINDING_REVOKED")
            try:
                self._owner._check_lease()
            except Exception as error:
                code = getattr(error, "code", "CLOCK_DISCONTINUITY")
                if code not in {"BINDING_EXPIRED", "CLOCK_DISCONTINUITY", "BINDING_REQUIRED"}:
                    code = "CLOCK_DISCONTINUITY"
                raise GuardError(code) from None
            return self._owner.current_record
        finally:
            self._owner._lock.release()

    def preflight(self, config: Any, *, policy_allowed: bool = False,
                  work_settled: bool = False) -> dict[str, Any]:
        """The sole admission entry; explicit invocation, never used by bind."""
        if not self._lock.acquire(blocking=False):
            raise GuardError("OWNER_BUSY")
        entered = False
        try:
            if policy_allowed is not True:
                raise GuardError("POLICY_DENIED")
            if work_settled is not True or self._operation_token is not None:
                raise GuardError("WORK_NOT_SETTLED")
            try:
                before = _sample_clock(self.clock)
            except Exception:
                # This fault is already an observed loss of clock authority,
                # even though replacement has not entered the L03 owner yet.
                # Poison the existing generation before returning: a later
                # apparently healthy sample cannot revive cached references.
                # Policy/work/active-reservation denials above do not sample
                # the clock and must not revoke an otherwise valid Binding.
                self.invalidate("CLOCK_DISCONTINUITY")
                raise GuardError("CLOCK_DISCONTINUITY") from None
            if self._record is not None:
                self.invalidate("BINDING_REVOKED")
            self._state, self._reason = "PREFLIGHT", "BINDING_REQUIRED"
            entered = True
            self._record = None
            self._owner_record = None
            self._mode = "NORMAL"
            # Exactly one delegation. No retry after either result or exception.
            result = self._owner.preflight(config, policy_allowed=True, work_settled=True)
            after = _sample_clock(self.clock)
            check_continuity(before, after)
            if type(result) is not dict or result.get("status") != "PASS":
                self._state = "UNBOUND"
                self._reason = GuardError(result.get("reason_code", "BINDING_RECORD_INVALID") if type(result) is dict else "BINDING_RECORD_INVALID").code
                return result
            if result.get("state") != "BOUND":
                raise GuardError("BINDING_RECORD_INVALID")
            record = ParsedBinding.parse(result.get("binding"))
            if result.get("binding_sha256") != record.record_hash:
                raise GuardError("BINDING_RECORD_INVALID")
            if record.binding_id in self._tombstones:
                raise GuardError("BINDING_REVOKED")
            owner_record = self._owner_record_checked()
            if not callable(getattr(owner_record, "to_bytes", None)):
                raise GuardError("BINDING_RECORD_INVALID")
            live_record = ParsedBinding.parse(owner_record.to_bytes())
            if live_record != record:
                raise GuardError("BINDING_RECORD_INVALID")
            # L03 truncates issued UTC to seconds. It must have issued inside this
            # explicit call, not returned an older still-valid record.
            issued, expires = timestamp(record.issued_at_utc), timestamp(record.expires_at_utc)
            if not before.utc - timedelta(seconds=1) < issued <= after.utc or not issued <= after.utc < expires:
                raise GuardError("BINDING_RECORD_INVALID")
            self._record, self._owner_record = record, owner_record
            self._generation += 1
            self._anchor = after
            self._deadline = after.monotonic + (expires - after.utc).total_seconds()
            self._state, self._reason = "BOUND", "NONE"
            return result
        except GuardError as error:
            if entered:
                self.invalidate(error.code)
            raise
        except Exception:
            if entered:
                self.invalidate("BINDING_RECORD_INVALID")
            raise GuardError("BINDING_RECORD_INVALID") from None
        finally:
            self._lock.release()

    def snapshot(self) -> AuthoritySnapshot:
        with self._lock:
            record = self._record
            if record is None:
                raise GuardError(self._reason)
            if self._state != "BOUND" or record.binding_id in self._tombstones:
                raise GuardError(self._tombstones.get(record.binding_id, self._reason))
            try:
                # This method is also called directly by create_guard. Its
                # sampling failure must revoke the shared generation without
                # depending on an outer BindingGuard to normalize the error.
                current = _sample_clock(self.clock)
                check_continuity(self._anchor, current)
                if (not timestamp(record.issued_at_utc) <= current.utc < timestamp(record.expires_at_utc) or
                        self._deadline is None or current.monotonic >= self._deadline):
                    raise GuardError("BINDING_EXPIRED")
                owner_record = self._owner_record_checked()
                if owner_record is not self._owner_record:
                    raise GuardError("BINDING_REVOKED")
                current_record = ParsedBinding.parse(owner_record.to_bytes())
                if current_record.binding_id != record.binding_id:
                    raise GuardError("BINDING_REVOKED")
                if current_record.record_hash != record.record_hash:
                    raise GuardError("BINDING_RECORD_INVALID")
            except GuardError as error:
                self.invalidate(error.code)
                raise GuardError(self._reason) from None
            return AuthoritySnapshot(record.binding_id, record.record_hash, self._generation, mode=self._mode)

    def acquire_operation(self, token: object) -> None:
        with self._lock:
            self.snapshot()
            if token is None:
                raise GuardError("OWNER_BUSY")
            thread = threading.get_ident()
            if self._operation_token is not None and (
                    self._operation_token is not token or self._operation_thread != thread):
                raise GuardError("OWNER_BUSY")
            if self._operation_token is None:
                self._operation_token, self._operation_thread = token, thread
            self._operation_depth += 1

    def release_operation(self, token: object) -> None:
        with self._lock:
            if (token is None or self._operation_token is not token or
                    self._operation_thread != threading.get_ident() or self._operation_depth < 1):
                raise GuardError("OWNER_BUSY")
            self._operation_depth -= 1
            if self._operation_depth == 0:
                self._operation_token, self._operation_thread = None, None

    def claim_operation_id(self, method: str, operation_id: str) -> None:
        with self._lock:
            self.snapshot()
            if method not in {"open_project_chat", "execute_prompt_and_wait", "download_response_zip"}:
                raise GuardError("BINDING_RECORD_INVALID")
            if type(operation_id) is not str or not operation_id or len(operation_id) > 512:
                raise GuardError("BINDING_RECORD_INVALID")
            if self._operation_token is None or self._operation_thread != threading.get_ident() or self._operation_depth < 1:
                raise GuardError("OWNER_BUSY")
            key = (method, operation_id)
            if key in self._claimed_operations or len(self._claimed_operations) >= 10000:
                raise GuardError("RECONCILIATION_REQUIRED")
            # Retained across explicit Preflights for this live owner lifetime.
            # Consumed before Provider dispatch; never rolled back on failure.
            self._claimed_operations.add(key)

    def create_guard(self) -> BindingGuard:
        with self._lock:
            self.snapshot()
            if self._record is None:  # checked above; explicit for type narrowing
                raise GuardError("BINDING_REQUIRED")
            guard = BindingGuard(self._record, self, clock=self.clock)
            self._guard_refs.append(weakref.ref(guard))
            return guard

    def target_projection(self) -> Any:
        """Generic TargetBinding receives only the four-field target projection."""
        with self._lock:
            self.snapshot()
            from cdpd import TargetBinding
            assert self._record is not None
            return TargetBinding.from_dict(self._record.to_dict()["target"])

    def invalidate(self, reason: str, *, binding_id: str | None = None, generation: int | None = None) -> None:
        with self._lock:
            if binding_id is not None and (self._record is None or binding_id != self._record.binding_id or
                                           (generation is not None and generation != self._generation)):
                self._tombstones.setdefault(binding_id, GuardError(reason).code)
                return
            if self._state in {"INVALIDATED", "EXPIRED", "RELEASED"}:
                return
            safe = GuardError(reason).code
            self._reason = safe
            self._state = "EXPIRED" if safe == "BINDING_EXPIRED" else "INVALIDATED"
            if self._record is not None:
                self._tombstones.setdefault(self._record.binding_id, safe)

    def set_mode(self, mode: str, *, binding_id: str | None = None, generation: int | None = None) -> None:
        with self._lock:
            if binding_id is not None and (self._record is None or binding_id != self._record.binding_id or
                                           (generation is not None and generation != self._generation)):
                raise GuardError("BINDING_REVOKED")
            if mode not in {"DIAGNOSTIC_ONLY", "RECONCILIATION_REQUIRED"}:
                self.invalidate("BINDING_REVOKED")
                raise GuardError("BINDING_REVOKED")
            if mode == "RECONCILIATION_REQUIRED" or self._mode != "RECONCILIATION_REQUIRED":
                self._mode = mode

    def close(self) -> None:
        """Release owner authority; never launch/close Chrome or change files."""
        self.invalidate("BINDING_REVOKED")
        close_clock = getattr(self.clock, "close", None)
        if callable(close_clock):
            close_clock()
