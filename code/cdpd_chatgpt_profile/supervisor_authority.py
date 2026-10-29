"""L07 immediate local restriction with bounded, exact-scope shared notice.

This adapter depends on the unchanged, source-pinned L04 BindingGuard private
state. No I/O occurs in the local latch. The accepted public setters cannot be
used on the liveness tick because their external callbacks may block. Shared
acknowledgement requires readback, is deadline-sealed, and grants no authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
import threading
import time

from chatgpt_web_api.target_guard import AuthoritySnapshot, BindingGuard


@dataclass(frozen=True)
class _Scope:
    binding_id: str
    generation: int
    record_hash: str


class PublicationGate:
    """Linearize local invalidation against the final COMMIT publication step.

    Long payload writes and fsyncs happen outside this gate.  The only action
    executed while holding the lock is the caller supplied atomic publication
    primitive (normally os.replace of an already-fsynced staged COMMIT).
    Therefore an invalidation that returns before publish() starts makes the
    publish fail; an invalidation that returns after publish() wins only after
    COMMIT has become visible.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._invalidated = False
        self._published = False
        self._reason = 'NONE'
        self._metadata_publications = {}

    @property
    def invalidated(self):
        with self._lock:
            return self._invalidated

    @property
    def published(self):
        with self._lock:
            return self._published

    @property
    def reason(self):
        with self._lock:
            return self._reason

    def invalidate(self, reason='LOCAL_INVALIDATION'):
        # No filesystem/shared-authority I/O belongs in this critical section.
        with self._lock:
            self._invalidated = True
            if self._reason == 'NONE':
                self._reason = str(reason)
            return 'AFTER_PUBLISH' if self._published else 'BEFORE_PUBLISH'

    def publish(self, action):
        if not callable(action):
            raise ValueError('PUBLICATION_ACTION_INVALID')
        with self._lock:
            if self._invalidated:
                return False
            action()
            self._published = True
            return True

    def metadata_only_view(self, job, files, destination):
        """Issue one exact diagnostic-publication capability, never a permit.

        This additional path serves an originally METADATA_ONLY matrix event
        such as confirmed Chrome exit. Content-origin requests, even after
        their result is downgraded, continue to use publish() and its sticky
        revocation veto. No existing gate state is cleared or replaced.
        """
        if type(self) is not PublicationGate:
            raise ValueError('METADATA_PUBLICATION_DENIED')
        from .evidence_store import encode_profile_bundle
        _, data = _metadata_request(job)
        blob = encode_profile_bundle(files, data['capture_id'])
        raw, capture_id, manifest, fingerprint = _metadata_blob_contract(job, blob)
        target = _metadata_destination(destination, capture_id)
        state = _MetadataPublicationState(raw, capture_id, manifest, fingerprint, target)
        # Classification, hashing and validation above perform no work while
        # holding the liveness/publication lock. A capture cannot mint a second
        # view, including after a rejected or interrupted first attempt.
        with self._lock:
            if capture_id in self._metadata_publications:
                raise ValueError('METADATA_PUBLICATION_DENIED')
            self._metadata_publications[capture_id] = state
        return _MetadataOnlyPublicationView(self, state)


@dataclass
class _MetadataPublicationState:
    job_raw: bytes
    capture_id: str
    manifest_sha256: str
    blob_sha256: str
    destination: str
    attempted: bool = False
    published: bool = False


def _metadata_destination(destination, capture_id):
    target = os.fspath(destination)
    if (type(target) is not str or not os.path.isabs(target)
            or os.path.basename(os.path.normpath(target)) != capture_id):
        raise ValueError('METADATA_PUBLICATION_DENIED')
    return os.path.normcase(os.path.abspath(target))


def _metadata_request(job):
    from .bundle_validation import capture_rule
    from .worker import WorkerRequest
    if type(job) is not WorkerRequest or type(job._raw) is not bytes:
        raise ValueError('METADATA_PUBLICATION_DENIED')
    frozen = WorkerRequest.from_dict(job.to_dict())
    if frozen._raw != job._raw:
        raise ValueError('METADATA_PUBLICATION_DENIED')
    data = frozen.to_dict()
    if capture_rule(data['trigger_envelope'])['capture_mode'] != 'METADATA_ONLY':
        raise ValueError('METADATA_PUBLICATION_DENIED')
    return frozen, data


def _metadata_blob_contract(job, blob):
    """Validate exact original intent and a complete metadata-only byte set.

    The trusted coordinator supplies the immutable request. Payload flags are
    constraints on its capability, not independent authority. This function
    is called before issue and again on the store's actual persisted bytes.
    """
    from .bundle_validation import canonical, verify_report_bundle
    frozen, data = _metadata_request(job)
    if type(blob) is not dict or any(type(k) is not str or type(v) is not bytes for k, v in blob.items()):
        raise ValueError('METADATA_PUBLICATION_DENIED')
    observed = dict(blob)
    manifest, payload = verify_report_bundle(observed)
    result = payload['RESULT.json']
    profile = result['profile']
    if (set(payload) != {'TRIGGER.json', 'EVENTS.json', 'RESULT.json'}
            or result['capture_status'] not in {'PARTIAL', 'REJECTED'}
            or result['assessment_status'] != 'NOT_PERFORMED'
            or result['failure_code'] in {None, 'NONE'}
            or profile['capture_id'] != data['capture_id']
            or profile['trigger_envelope'] != data['trigger_envelope']
            or profile['binding_record_raw_json'] != data['binding_record_raw_json']
            or profile['binding_record_sha256'] != data['binding_record_sha256']
            or profile['control_generation'] != data['control_generation']
            or profile['supervisor_instance_id'] != data['supervisor_instance_id']
            or profile['policy_allowed'] is not data['policy_allowed']
            or profile['worker_termination'] != 'NOT_STARTED'
            or profile['timing']['capture_started_at_utc'] is not None
            or profile['timing']['latency_ms'] is not None
            or any(value is not False for value in profile['identity_checks'].values())
            or profile['sanitization']['passed'] is not False
            or profile['coverage_complete'] is not False
            or profile['observation_relation'] != 'UNKNOWN'
            or profile['document_markers']['before'] is not None
            or profile['document_markers']['after'] is not None
            or profile['report_context']['context_verified'] is not False
            or any(profile['stages'][name]['status'] != 'NOT_RUN'
                   or profile['stages'][name]['duration_ms'] != 0 for name in ('S1', 'S2'))):
        raise ValueError('METADATA_PUBLICATION_DENIED')
    fingerprint = hashlib.sha256(canonical({name: hashlib.sha256(raw).hexdigest()
                                           for name, raw in sorted(observed.items())})).hexdigest()
    return frozen._raw, data['capture_id'], manifest, fingerprint


class _MetadataOnlyPublicationView:
    """One attempted publication of one frozen diagnostic to one destination.

    A shallow copy shares the parent's registered state, so copying a view
    cannot reset its single-attempt latch. Direct publish() has no payload or
    job proof and therefore never invokes its action.
    """
    def __init__(self, gate, state):
        self._gate, self._state = gate, state

    def _consume(self):
        gate, state = self._gate, self._state
        if type(gate) is not PublicationGate or type(state) is not _MetadataPublicationState:
            return False
        with gate._lock:
            if gate._metadata_publications.get(state.capture_id) is not state or state.attempted:
                return False
            state.attempted = True
            return True

    def publish(self, action):
        self._consume()
        return False

    def publish_verified(self, action, actual_blob, job, destination):
        if not self._consume():
            return False
        state, gate = self._state, self._gate
        # A rejected proof consumes the capability; validation cannot be used
        # as a trial-and-retry admission API. All work remains outside the lock.
        try:
            if not callable(action):
                return False
            raw, capture_id, manifest, fingerprint = _metadata_blob_contract(job, actual_blob)
            if (raw != state.job_raw or capture_id != state.capture_id
                    or manifest != state.manifest_sha256 or fingerprint != state.blob_sha256
                    or _metadata_destination(destination, capture_id) != state.destination):
                return False
        except Exception:
            return False
        with gate._lock:
            if gate._metadata_publications.get(state.capture_id) is not state or state.published:
                return False
            # A metadata audit never clears the content gate's invalidation,
            # reason, BindingGuard terminal state, stop or worker restrictions.
            # The only blocking operation here is the store's atomic rename.
            action()
            state.published = True
            gate._published = True
            return True


class NonblockingRestriction:
    """One original guard; at most one mode notice and one confirmed upgrade.

An unconfirmed notification is never retried. Upgrading a pending/unknown mode
notice immediately revokes the local guard and seals UNCONFIRMED, without
concurrent or replacement shared calls. A confirmed mode may be upgraded with
one exact-scope invalidation. This is a restriction, never operation admission.
"""

    def __init__(self, guard: BindingGuard, timeout_seconds: float = .2):
        if type(guard) is not BindingGuard:
            raise ValueError('EXACT_PINNED_BINDING_GUARD_REQUIRED')
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= .25):
            raise ValueError('INVALID_RESTRICTION_TIMEOUT')
        if type(guard.generation) is not int or guard.generation < 1:
            raise ValueError('INVALID_GUARD_GENERATION')
        self.guard = guard
        self._authority = guard._authority
        self._record = guard.record
        self._scope = _Scope(guard.record.binding_id, guard.generation, guard.record.record_hash)
        self.timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._status = 'NOT_IMPOSED'
        self._target = None
        self._reason = 'NONE'
        self._serial = 0
        self._deadline = None
        self._attempts = 0

    def _same_local_scope(self):
        return (self.guard.record is self._record and self.guard._authority is self._authority
                and self.guard._generation == self._scope.generation
                and type(self.guard._generation) is int)

    def _expire_locked(self):
        if self._status == 'PENDING' and time.monotonic() >= self._deadline:
            self._status, self._reason = 'UNCONFIRMED', 'NOTIFICATION_TIMEOUT'
            self._done.set()

    @property
    def status(self):
        with self._lock:
            self._expire_locked()
            return self._status

    @property
    def confirmed(self):
        return self.status == 'CONFIRMED'

    @property
    def reason_code(self):
        with self._lock:
            self._expire_locked()
            return self._reason

    @property
    def notification_attempts(self):
        with self._lock: return self._attempts

    @property
    def target(self):
        with self._lock: return self._target

    def impose(self, revoke: bool = False):
        if type(revoke) is not bool:
            raise ValueError('INVALID_RESTRICTION_KIND')
        with self._lock:
            self._expire_locked()
            if not self._same_local_scope():
                self._status, self._reason = 'UNCONFIRMED', 'LOCAL_SCOPE_CHANGED'
                self._done.set()
                return self._status
            # Exact accepted L04 local state transitions, with all external
            # calls removed from this synchronous boundary. Reconciliation and
            # an already-terminal original cause/state are never overwritten.
            if self.guard._mode != 'RECONCILIATION_REQUIRED':
                self.guard._mode = 'DIAGNOSTIC_ONLY'
            if revoke and not self.guard._terminal:
                self.guard._reason = 'BINDING_REVOKED'
                self.guard._state = 'INVALIDATED'
                self.guard._terminal = True
            target = 'REVOKE' if revoke or self.guard._terminal else 'MODE'
            if self._target == 'REVOKE' or self._target == target:
                return self._status
            prior = self._status
            self._target = target
            if prior in {'PENDING', 'UNCONFIRMED'}:
                # A late result for the weaker request cannot acknowledge the
                # stronger local state, nor initiate a retry to a newer owner.
                self._serial += 1
                self._status, self._reason = 'UNCONFIRMED', 'UPGRADE_WITH_UNCONFIRMED_NOTICE'
                self._done.set()
                return self._status
            self._serial += 1
            serial = self._serial
            self._deadline = time.monotonic() + self.timeout_seconds
            self._status, self._reason = 'PENDING', 'AWAITING_EXACT_SHARED_READBACK'
            self._done = threading.Event()
            self._attempts += 1
            mode = self.guard._mode
            reason = self.guard._reason if self.guard._terminal else 'BINDING_REVOKED'
            deadline = self._deadline
        # Neither callback execution nor a blocked snapshot holds our latch
        # lock. Timer and callback have separate threads; both are daemon-only.
        timer = threading.Timer(max(0.0, deadline-time.monotonic()), self._seal_timeout, args=(serial,))
        timer.daemon = True
        try:
            timer.start()
            threading.Thread(target=self._notify, args=(serial, target, mode, reason),
                name='cdpd-l07-shared-restriction', daemon=True).start()
        except Exception:
            self._complete(serial, False, 'NOTIFICATION_THREAD_UNAVAILABLE')
        return self.status

    def _seal_timeout(self, serial):
        with self._lock:
            if serial == self._serial:
                self._expire_locked()

    def _readback(self, target):
        scope = self._scope
        try:
            snapshot = self._authority.snapshot()
        except Exception:
            if target != 'REVOKE':
                return False
            # The pinned accepted bridge deliberately raises snapshot() after
            # revocation. Only that exact class gets its established locked
            # state fallback, with no live preflight/clock/admission calls.
            from cdpd_chatgpt_adapter.l03_binding import L03BindingBridge
            authority = self._authority
            if type(authority) is not L03BindingBridge or not authority._lock.acquire(blocking=False):
                return False
            try:
                record = authority._record
                return (record is not None and record.binding_id == scope.binding_id
                    and record.record_hash == scope.record_hash
                    and type(authority._generation) is int and authority._generation == scope.generation
                    and authority._state in {'INVALIDATED', 'EXPIRED'})
            finally:
                authority._lock.release()
        if (type(snapshot) is not AuthoritySnapshot or snapshot.binding_id != scope.binding_id
                or type(snapshot.generation) is not int or snapshot.generation != scope.generation
                or snapshot.record_hash != scope.record_hash):
            return False
        if target == 'REVOKE':
            return snapshot.revoked is True or snapshot.state in {'INVALIDATED','EXPIRED'}
        return (snapshot.state == 'BOUND' and snapshot.revoked is False
                and snapshot.superseded is False and snapshot.continuity_known is True
                and snapshot.mode in {'DIAGNOSTIC_ONLY','RECONCILIATION_REQUIRED'})

    def _notify(self, serial, target, mode, reason):
        with self._lock:
            self._expire_locked()
            if serial != self._serial or self._status != 'PENDING':
                return
        try:
            if target == 'REVOKE':
                self._authority.invalidate(reason, binding_id=self._scope.binding_id,
                                           generation=self._scope.generation)
            else:
                self._authority.set_mode(mode, binding_id=self._scope.binding_id,
                                         generation=self._scope.generation)
            confirmed = self._readback(target)
        except Exception:
            self._complete(serial, False, 'SHARED_NOTIFICATION_FAILED')
        else:
            self._complete(serial, confirmed, 'EXACT_SHARED_READBACK' if confirmed else 'SHARED_READBACK_UNCONFIRMED')

    def _complete(self, serial, confirmed, reason):
        with self._lock:
            self._expire_locked()
            if serial != self._serial or self._status != 'PENDING':
                return
            if not self._same_local_scope():
                confirmed, reason = False, 'LOCAL_SCOPE_CHANGED'
            self._status = 'CONFIRMED' if confirmed else 'UNCONFIRMED'
            self._reason = reason
            self._done.set()

    def wait(self, timeout: float = .25):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 <= timeout <= .25):
            raise ValueError('INVALID_RESTRICTION_WAIT')
        with self._lock:
            self._expire_locked()
            if self._status != 'PENDING':
                return self._status
            event = self._done
        event.wait(timeout)
        return self.status
