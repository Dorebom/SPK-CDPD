"""Independent L07 watchdog, accepted L05 journal, and accepted L06 capture.

No saved Binding, heartbeat, EVENT_DURABILITY ACK, or bootstrap JSON grants
live authority. A trusted same-process bootstrap may register an actual L04
BindingGuard. Restart recovery uses the original S0 and never starts a Worker.
All persistent I/O is outside the liveness loop. Worker deadlines have their
own OS ownership boundary and do not depend on a journal fsync returning.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib
import math
import os
from pathlib import Path
import queue
import re
import signal
import threading
import time
from typing import Callable, Any
from uuid import uuid4

from cdpd import CaptureRequest, DiagnosticEvent, TargetBinding
from chatgpt_web_api.target_guard import BindingGuard, ParsedBinding
from .capture_service import CaptureCoordinator, CaptureServiceError, L04CapturePermitAuthority
from .evidence_store import (ProfileEvidenceStore, _safe_directory, _read_regular,
                             _write_new, _sync_directory, verify_profile_bundle)
from .spool import (JournalSpool, FileMailbox, SpoolError, _FileLock,
                    canonical_json, strict_json, validate_envelope, read_journal,
                    read_history, stream_key)
from .worker import WorkerRequest, metadata_worker_result
from .watchdog import (HeartbeatTracker, OperationScope, WatchdogError,
                       TriggerKind, ObservationSource, WatchdogTrigger, select_trigger)
from .watchdog_runner import WatchdogWorkerRunner
from .owned_process import RegisteredProcess
from .supervisor_authority import NonblockingRestriction, PublicationGate
from .terminal_failure import is_terminal
from .bundle_validation import _contracts


class SupervisorError(RuntimeError):
    def __init__(self, code: str):
        allowed = {'INPUT_INVALID', 'SUPERVISOR_ALREADY_RUNNING', 'PROFILE_ALREADY_OWNED',
                   'STORAGE_FAILED', 'STORAGE_UNAVAILABLE', 'STARTUP_TIMEOUT',
                   'WINDOWS_STORAGE_POLICY_UNVERIFIED', 'SUPERVISOR_IN_CONSUMER_JOB',
                   'CLOCK_OR_KEEPER_LOST', 'NO_ACTIVE_BINDING', 'SCOPE_MISMATCH',
                   'WORKER_TERMINATION_UNCONFIRMED', 'SPOOL_BACKPRESSURE',
                   'SUPERVISOR_STOP_UNCONFIRMED'}
        self.code = code if code in allowed else 'STORAGE_FAILED'
        super().__init__(self.code)


def _opaque(value: Any, maximum: int = 256) -> str:
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise SupervisorError('INPUT_INVALID')
    return value


def _number(value: Any, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise SupervisorError('INPUT_INVALID')
    return float(value)


def _pin(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass(frozen=True)
class SupervisorConfig:
    root: Path
    profile_directory: Path
    run_id: str
    poll_seconds: float = 0.25
    heartbeat_expiry_seconds: float = 10.0
    capture_timeout_seconds: float = 10.0
    # Smaller expiry values are for isolated synthetic injection. Production CLI
    # always uses the approved 10-second receiver timeout.
    max_operations: int = 32
    storage_wait_seconds: float = 2.0
    lock_registry: Path | None = None

    def __post_init__(self):
        _opaque(self.run_id, 1024)
        for name in ('root', 'profile_directory'):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise SupervisorError('INPUT_INVALID')
            object.__setattr__(self, name, value)
        _number(self.poll_seconds, 0.01, 0.25)
        _number(self.heartbeat_expiry_seconds, 0.01, 10.0)
        _number(self.capture_timeout_seconds, 0.1, 20.0)
        _number(self.storage_wait_seconds, 0.1, 2.0)
        if type(self.max_operations) is not int or not 1 <= self.max_operations <= 32:
            raise SupervisorError('INPUT_INVALID')


@dataclass(frozen=True)
class _LiveContext:
    raw: str
    generation: int
    producer_id: str
    producer_instance_id: str
    guard: BindingGuard
    permit: L04CapturePermitAuthority
    restriction: Any
    publication_gate: Any
    consumer: Any = None
    chrome: Any = None


@dataclass
class _Operation:
    scope: OperationScope
    envelope: dict
    tracker: HeartbeatTracker
    context: _LiveContext | None
    finished: bool = False
    claimed: bool = False
    dispatch_state: str = 'NOT_APPLICABLE'
    pending_events: list = field(default_factory=list)
    observed_exits: set = field(default_factory=set)


class _JournalPump:
    """The only L05 writer, isolated from time-critical liveness supervision."""
    def __init__(self, root: Path, poll: float, monotonic: Callable[[], float], *, fault_hook=None):
        self.root, self.poll, self.monotonic = root, poll, monotonic
        self.fault_hook = fault_hook
        self.events: queue.Queue = queue.Queue(maxsize=512)
        self.commands: queue.Queue = queue.Queue(maxsize=256)
        self.ready, self.stop = threading.Event(), threading.Event()
        self.error: str | None = None
        self.poll_unavailable_count=0
        self.thread = threading.Thread(target=self._run, name='cdpd-journal-writer', daemon=True)

    def _run(self):
        try:
            with JournalSpool(self.root, fault_hook=self.fault_hook) as writer:
                # Historical ACTION_STARTED/HB rows are not fresh liveness and
                # cannot arm work after a Supervisor restart.
                seen = {row['event_id'] for row in read_journal(self.root)['rows']}
                self.ready.set()
                unavailable_since=None
                while not self.stop.is_set():
                    for _ in range(256):
                        try:
                            envelope, done, box, command_deadline = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        deferred=False
                        try:
                            if time.monotonic()>=command_deadline:
                                raise SupervisorError('STORAGE_UNAVAILABLE')
                            box['receipt'] = writer.append(envelope)
                        except SpoolError as error:
                            if error.code=='CURRENT_NOT_DURABLE' and not writer._faulted:
                                # This code does not expose whether locking or
                                # the OS failed. Keep the exact metadata event
                                # pending briefly; never manufacture an ACK.
                                self.commands.put_nowait((envelope,done,box,command_deadline))
                                deferred=True
                            else:
                                box['error']='STORAGE_FAILED'
                        except Exception as error:
                            box['error']=getattr(error,'code','STORAGE_FAILED')
                        finally:
                            if not deferred:done.set()
                        if deferred:break
                    try:
                        writer.process_pending()
                    except SpoolError as error:
                        if error.code!='CURRENT_NOT_DURABLE' or writer._faulted:
                            raise
                        self.poll_unavailable_count+=1
                        if unavailable_since is None:unavailable_since=time.monotonic()
                        if time.monotonic()-unavailable_since>=2.0:
                            raise SupervisorError('STORAGE_UNAVAILABLE') from None
                        self.stop.wait(self.poll)
                        continue
                    else:
                        unavailable_since=None
                    for row in read_journal(self.root)['rows']:
                        if row['event_id'] not in seen:
                            try:
                                self.events.put_nowait((row['envelope'], self.monotonic()))
                            except queue.Full:
                                # Retry delivery of the durable event on a later
                                # poll; never drop or replay a capture here.
                                break
                            seen.add(row['event_id'])
                    self.stop.wait(self.poll)
        except Exception as error:
            self.error = 'STORAGE_UNAVAILABLE' if getattr(error,'code',None)=='STORAGE_UNAVAILABLE' else 'STORAGE_FAILED'
            self.ready.set()

    def append(self, envelope: dict, timeout: float) -> dict:
        done, box = threading.Event(), {}
        try:
            self.commands.put_nowait((validate_envelope(envelope), done, box,time.monotonic()+timeout))
        except queue.Full:
            raise SupervisorError('SPOOL_BACKPRESSURE') from None
        if not done.wait(timeout):
            # A late EVENT_DURABILITY receipt never starts a capture by itself.
            raise SupervisorError('STORAGE_UNAVAILABLE')
        receipt = box.get('receipt')
        if box.get('error') or not receipt or receipt.get('stage') != 'ACK_DURABLE':
            raise SupervisorError('STORAGE_FAILED')
        return receipt


class Supervisor:
    """One profile owner and at most one capture task; no Web mutation API."""
    def __init__(self, config: SupervisorConfig, *, runner=None,
                 monotonic: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] | None = None,
                 storage_authorizer: Callable[[Path], bool] | None = None,
                 journal_fault_hook=None):
        if not isinstance(config, SupervisorConfig):
            raise SupervisorError('INPUT_INVALID')
        self.config, self.monotonic = config, monotonic
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.instance_id = str(uuid4())
        self.runner = runner if runner is not None else WatchdogWorkerRunner()
        self._storage_authorizer = storage_authorizer
        self._journal_fault_hook = journal_fault_hook
        self._owner_lock = self._profile_lock = None
        self._pump: _JournalPump | None = None
        self._contexts: dict[tuple, _LiveContext] = {}
        self._binding_metadata: dict[tuple, dict] = {}
        self._operations: dict[tuple, _Operation] = {}
        self._pending: deque = deque()
        self._results: queue.Queue = queue.Queue()
        self._task: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._seq = 0
        self._claimed: set[tuple] = set()
        self._block_new_workers = False
        self._block_reason = None
        self._writer_error = None
        # Load the unchanged approved matrix before the liveness loop. Exact
        # event triples, not a partial reason list, own INVALIDATE semantics.
        self._invalidating_events = frozenset(
            (row['event_kind'], row['failure_code'], row['profile_reason'])
            for row in _contracts()[2]['rules'] if row['binding_effect'] == 'INVALIDATE')
        self._last_clock: float | None = None
        self.diagnostic_only = False
        self.observations: list[dict] = []
        self.results: list[dict] = []
        self.rejections = 0
        if isinstance(self.runner,WatchdogWorkerRunner):
            original_factory=self.runner.process_factory
            owner=self
            class CombinedCancellation:
                def __init__(self, local): self.local=local
                def is_set(self):
                    return owner._stop.is_set() or owner.worker_blocked or (self.local is not None and self.local.is_set())
            def owned_factory(command, **kwargs):
                # A stop before runner._active exists still reaches the actual
                # OS creation boundary and every delayed resume check.
                kwargs['cancellation']=CombinedCancellation(kwargs.get('cancellation'))
                return original_factory(command,**kwargs)
            self.runner.process_factory=owned_factory

    @property
    def root(self) -> Path:
        return self.config.root

    @property
    def worker_blocked(self) -> bool:
        return (self._block_new_workers
                or bool(getattr(self.runner, 'termination_unconfirmed', False))
                or bool(getattr(self.runner, 'resource_cleanup_unconfirmed', False)))

    def _binding_key(self, envelope: dict) -> tuple:
        return envelope['binding_id'], envelope['control_generation'], envelope['binding_sha256']

    def start(self) -> None:
        if self._started:
            raise SupervisorError('SUPERVISOR_ALREADY_RUNNING')
        _safe_directory(self.root, allow_missing=True)
        _safe_directory(self.config.profile_directory, allow_missing=False)
        if os.name == 'nt':
            from .owned_process import current_process_in_job
            from .supervisor_policy import WindowsStoragePolicy
            if current_process_in_job():
                raise SupervisorError('SUPERVISOR_IN_CONSUMER_JOB')
            policy = WindowsStoragePolicy(self.root, self.config.profile_directory)
            policy.validate_startup()
            self._storage_authorizer = policy
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        registry = self.config.lock_registry
        if registry is None:
            if os.name == 'nt':
                registry = Path(os.environ['LOCALAPPDATA'])/'CDPD'/'SupervisorLocks'
            else:
                registry = Path('/tmp')/('cdpd-supervisor-locks-' + str(os.getuid()))
        registry = Path(registry)
        _safe_directory(registry, allow_missing=True)
        registry.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_identity = os.path.normcase(str(self.config.profile_directory.resolve()))
        try:
            lease_path=registry/(_pin(profile_identity)+'.lock')
            if lease_path.exists(): _read_regular(lease_path,4096)
            self._profile_lock = _FileLock(lease_path)
        except Exception:
            raise SupervisorError('PROFILE_ALREADY_OWNED') from None
        try:
            if (self.root/'supervisor.lock').exists(): _read_regular(self.root/'supervisor.lock',4096)
            self._owner_lock = _FileLock(self.root/'supervisor.lock')
            for name in ('operations', 'binding_metadata', 'sessions', 'observations'):
                (self.root/name).mkdir(mode=0o700, exist_ok=True)
            self._load_records()
            session = {'schema':'cdpd.supervisor-session/v1', 'supervisor_instance_id':self.instance_id,
                       'run_id':self.config.run_id, 'created_utc':_stamp(), 'pid':os.getpid(),
                       'profile_sha256':_pin(profile_identity), 'live_keeper_restored':False}
            _write_new(self.root/'sessions'/(self.instance_id+'.json'), canonical_json(session))
            _sync_directory(self.root/'sessions')
            self._pump = _JournalPump(self.root/'spool', self.config.poll_seconds, self.monotonic,
                                      fault_hook=self._journal_fault_hook)
            # A startup fsync may outlive the caller's readiness timeout. Keep
            # profile ownership reachable from the still-running writer thread.
            self._pump.ownership = (self._owner_lock,self._profile_lock)
            self._pump.thread.start()
            if not self._pump.ready.wait(2.0):
                raise SupervisorError('STARTUP_TIMEOUT')
            if self._pump.error:
                raise SupervisorError(self._pump.error)
            self._started = True
        except BaseException:
            if self._pump:
                self._pump.stop.set()
            if not self._pump or not self._pump.thread.is_alive():
                if self._owner_lock:
                    self._owner_lock.close(); self._owner_lock = None
                self._profile_lock.close(); self._profile_lock = None
            raise

    def _load_records(self) -> None:
        self._block_new_workers = (self.root/'WORKER_BLOCKED.json').exists()
        for path in (self.root/'binding_metadata').glob('*.json'):
            value = strict_json(_read_regular(path, 16384), max_bytes=16384)
            raw = value['binding_record_raw_json']
            record = ParsedBinding.parse(raw)
            key = (record.binding_id, value['control_generation'], hashlib.sha256(raw.encode()).hexdigest())
            if value['binding_record_sha256'] != key[2] or path.stem != _pin(list(key)):
                raise SupervisorError('INPUT_INVALID')
            self._binding_metadata[key] = value
        for directory in (self.root/'operations').iterdir():
            _safe_directory(directory, allow_missing=False)
            claim = strict_json(_read_regular(directory/'CLAIM.json', 196608), max_bytes=196608)
            job = WorkerRequest.from_dict(claim['job'])
            if claim.get('job_sha256') != hashlib.sha256(job._raw).hexdigest():
                raise SupervisorError('INPUT_INVALID')
            key = (claim['run_id'], job.to_dict()['trigger_envelope']['event']['operation_id'])
            if directory.name != _pin(list(key)):
                raise SupervisorError('INPUT_INVALID')
            self._claimed.add(key)
            outcome_path = directory/'OUTCOME.json'
            if outcome_path.exists():
                outcome = strict_json(_read_regular(outcome_path,262144),max_bytes=262144)
                if (type(outcome) is not dict or outcome.get('schema') != 'cdpd.supervisor-outcome/v1'
                        or outcome.get('operation_id') != key[1]
                        or type(outcome.get('worker_blocked')) is not bool
                        or not isinstance(outcome.get('status'),str)):
                    raise SupervisorError('INPUT_INVALID')
                process = outcome.get('process_metadata')
                if type(process) is not dict:
                    raise SupervisorError('INPUT_INVALID')
                count = process.get('spawn_count')
                termination = process.get('termination')
                pid = process.get('pid')
                if ('spawn_count' not in process or 'pid' not in process
                        or not (count is None or type(count) is int and count in (0,1))
                        or termination not in {'NOT_STARTED','NORMAL','EXITED_ERROR','KILLED','UNCONFIRMED'}
                        or (count == 0 and pid is not None)
                        or (count == 1 and (type(pid) is not int or pid <= 0))
                        or (pid is not None and (type(pid) is not int or pid <= 0))):
                    raise SupervisorError('INPUT_INVALID')
                for flag in ('new_worker_prohibited','termination_unconfirmed','spawn_count_known',
                             'resource_cleanup_unconfirmed'):
                    if flag in process and type(process[flag]) is not bool:
                        raise SupervisorError('INPUT_INVALID')
                attempt = process.get('spawn_attempt_count',0)
                if type(attempt) is not int or attempt not in (0,1):
                    raise SupervisorError('INPUT_INVALID')
                facts = process.get('termination_facts')
                if facts is not None and (type(facts) is not dict or type(facts.get('confirmed')) is not bool):
                    raise SupervisorError('INPUT_INVALID')
                if (count != 0 or attempt != 0 or termination != 'NOT_STARTED') and facts is None:
                    raise SupervisorError('INPUT_INVALID')
                resources = process.get('resource_cleanup')
                if resources is not None:
                    count_names = ('pending_handle_count', 'pending_attribute_list_count', 'pending_stream_count')
                    if (type(resources) is not dict or set(resources) != {'confirmed', *count_names}
                            or type(resources.get('confirmed')) is not bool
                            or any(type(resources[name]) is not int or resources[name] < 0 for name in count_names)
                            or resources['confirmed'] != all(resources[name] == 0 for name in count_names)):
                        raise SupervisorError('INPUT_INVALID')
                resource_uncertain = (process.get('resource_cleanup_unconfirmed') is True
                                      or resources is not None and not resources['confirmed'])
                # An incomplete/contradictory lifetime record never proves that
                # another owned Worker may start, even if its top-level flag is
                # false or the global block marker could not be persisted.
                uncertain = (count is None or process.get('spawn_count_known') is False
                             or (count == 1 and termination == 'NOT_STARTED')
                             or (facts is not None and (not facts['confirmed']
                                 or facts.get('active_processes') != 0)))
                if (outcome['worker_blocked'] or process.get('new_worker_prohibited')
                        or process.get('termination_unconfirmed')
                        or outcome.get('shared_authority_notification') == 'UNCONFIRMED'
                        or termination == 'UNCONFIRMED' or uncertain or resource_uncertain):
                    self._block_new_workers = True
                    reason = ('WORKER_RESOURCE_CLEANUP_UNCONFIRMED'
                        if resource_uncertain and not uncertain and termination != 'UNCONFIRMED'
                        and not process.get('termination_unconfirmed')
                        and outcome.get('shared_authority_notification') != 'UNCONFIRMED'
                        else 'WORKER_TERMINATION_UNCONFIRMED')
                    if self._block_reason is None or reason == 'WORKER_TERMINATION_UNCONFIRMED':
                        self._block_reason = reason
            else:
                # An interrupted spawn/cleanup has unknown lifetime. Native Job
                # close is protection, not retrospective confirmation evidence.
                self._block_new_workers = True
                self._block_reason = 'INTERRUPTED_ATTEMPT_LIFETIME_UNCONFIRMED'
                self._pending.append(('recover', directory, job, None))

    def register_binding(self, guard: BindingGuard, *, producer_id: str,
                         producer_instance_id: str, raw_json: str | None = None,
                         consumer=None, chrome=None) -> None:
        """Trusted bootstrap only; never called by mailbox messages or JSON.

        A binding recorded by another Supervisor instance cannot be restored as
        live. A restart needs a fresh independently authorised preflight/Binding.
        """
        if not self._started or not isinstance(guard, BindingGuard):
            raise SupervisorError('INPUT_INVALID')
        for process in (consumer,chrome):
            if process is None:
                continue
            if (type(process) is not RegisteredProcess
                    or type(process.pid) is not int or process.pid <= 0
                    or type(process.identity) is not str or not process.identity):
                raise SupervisorError('INPUT_INVALID')
            try:
                alive = process.is_alive()
            except Exception:
                raise SupervisorError('INPUT_INVALID') from None
            if type(alive) is not bool:
                raise SupervisorError('INPUT_INVALID')
        _opaque(producer_id); _opaque(producer_instance_id)
        raw = guard.record.to_bytes().decode() if raw_json is None else raw_json
        if type(raw) is not str or ParsedBinding.parse(raw) != guard.record:
            raise SupervisorError('INPUT_INVALID')
        guard.check(mutation=False)
        key = (guard.record.binding_id, guard.generation, hashlib.sha256(raw.encode()).hexdigest())
        old = self._binding_metadata.get(key)
        if old and old['supervisor_instance_id'] != self.instance_id:
            raise SupervisorError('CLOCK_OR_KEEPER_LOST')
        context = _LiveContext(raw, guard.generation, producer_id, producer_instance_id,
                              guard, L04CapturePermitAuthority(guard, supervisor_instance_id=self.instance_id,
                                                             binding_record_raw_json=raw),
                              NonblockingRestriction(guard), PublicationGate(), consumer, chrome)
        if key in self._contexts and self._contexts[key] != context:
            raise SupervisorError('SCOPE_MISMATCH')
        # A permanent writer failure also constrains a newly supplied guard
        # before metadata I/O can block or fail. Registration cannot repair
        # journal durability or create an interval of local mutation authority.
        writer_error = self._writer_error or (self._pump.error if self._pump is not None else None)
        if writer_error:
            self._latch_writer_failure(writer_error)
            self._restrict(context)
        metadata = {'schema':'cdpd.supervisor-binding-metadata/v1',
                    'binding_record_raw_json':raw, 'binding_record_sha256':key[2],
                    'control_generation':guard.generation, 'supervisor_instance_id':self.instance_id,
                    'meaning':'METADATA_ONLY_NOT_LIVE_KEEPER_AUTHORITY'}
        path = self.root/'binding_metadata'/(_pin(list(key))+'.json')
        if not path.exists():
            _write_new(path, canonical_json(metadata)); _sync_directory(path.parent)
        self._binding_metadata[key] = metadata
        self._contexts[key] = context

    def publish(self, envelope: dict) -> dict:
        """Consumer-side mailbox convenience; LOCAL_PUBLISHED is not permission."""
        if not self._started:
            raise SupervisorError('INPUT_INVALID')
        return FileMailbox(self.root/'spool').publish(envelope)

    def _context(self, envelope: dict) -> _LiveContext | None:
        value = self._contexts.get(self._binding_key(envelope))
        event = envelope['event']
        if value and (value.producer_id, value.producer_instance_id) == (event['producer_id'],event['producer_instance_id']):
            return value
        return None

    def _derived(self, operation: _Operation, trigger: WatchdogTrigger) -> dict:
        reasons = {
            TriggerKind.HEARTBEAT_EXPIRED:('TIMEOUT','HEARTBEAT_EXPIRED'),
            TriggerKind.CONSUMER_EXITED:('UNKNOWN','CONSUMER_EXITED'),
            TriggerKind.CHROME_EXITED:('DISCONNECTED','CHROME_EXITED'),
            TriggerKind.CDP_DISCONNECTED:('DISCONNECTED','CDP_CHANNEL_LOST'),
            TriggerKind.WORKER_FAILED:('UNKNOWN','WORKER_FAILED'),
            TriggerKind.WORKER_DEADLINE:('TIMEOUT','WORKER_DEADLINE'),
            TriggerKind.CLOCK_OR_KEEPER_LOST:('UNKNOWN','CLOCK_OR_KEEPER_LOST')}
        failure, reason = reasons[trigger.kind]
        self._seq += 1
        original = operation.envelope
        return validate_envelope({
            'schema':'cdpd.chatgpt-event-envelope/v1', 'event_id':str(uuid4()),
            'producer_seq':self._seq, 'occurred_at_utc':self.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'binding_id':original['binding_id'], 'binding_sha256':original['binding_sha256'],
            'control_generation':original['control_generation'],
            'event':DiagnosticEvent('cdpd-supervisor',self.instance_id,operation.scope.operation_id,
                                    'OBSERVATION_FAILED',failure,{'run_id':self.config.run_id}).to_dict(),
            'profile':{'reason':reason, 'dispatch_state':operation.dispatch_state,
                       'ui_context':'UNDETERMINED','document_marker':None,
                       # The Supervisor is a different producer. Do not copy a
                       # consumer's loss population into this new stream.
                       # Original consumer history/unknown is preserved in L07_S0.
                       'loss_lower_bound':0,'loss_count_exact':False}})

    @staticmethod
    def _join_dispatch(old: str, new: str) -> str:
        order = {'NOT_APPLICABLE':0,'NOT_DISPATCHED':1,'UNKNOWN':2,'POSSIBLE':3,'CONFIRMED':4}
        return max((old,new),key=lambda state:order[state])

    def _restrict(self, context: _LiveContext | None, *, revoke=False) -> None:
        if context is not None:
            # The local publication latch is updated before any shared notice.
            # This is memory-only and cannot wait on storage or authority I/O.
            if revoke:
                context.publication_gate.invalidate('LOCAL_INVALIDATION')
            context.restriction.impose(revoke=revoke)

    def _latch_writer_failure(self, error: str) -> None:
        self._writer_error = self._writer_error or error
        self._block_new_workers = True
        self._block_reason = self._block_reason or self._writer_error
        self.diagnostic_only = True
        for context in self._contexts.values():
            self._restrict(context)

    def _latch_trigger(self, operation: _Operation, trigger: WatchdogTrigger) -> None:
        """Restrict live work independently of whether capture was claimed."""
        operation.tracker.latch_diagnostic_only(trigger)
        revoke = trigger.kind in {TriggerKind.CHROME_EXITED, TriggerKind.CDP_DISCONNECTED,
                                  TriggerKind.CLOCK_OR_KEEPER_LOST}
        self._restrict(operation.context, revoke=revoke)
        self.diagnostic_only = True

    def _receive(self, envelope: dict, received: float) -> None:
        value = validate_envelope(envelope)
        if value['event']['producer_id'] == 'cdpd-supervisor':
            return  # Diagnostic output never recursively triggers a worker.
        scope = OperationScope.from_envelope(value, supervisor_instance_id=self.instance_id,
                                              run_id=self.config.run_id)
        key = scope.operation_key
        current = self._operations.get(key)
        kind = value['event']['kind']
        if kind == 'ACTION_STARTED':
            if current is not None or key in self._claimed:
                return
            if len(self._operations) >= self.config.max_operations:
                raise SupervisorError('SPOOL_BACKPRESSURE')
            context = self._context(value)
            self._operations[key] = _Operation(scope,value,HeartbeatTracker(scope,received,
                expiry_seconds=self.config.heartbeat_expiry_seconds),context,
                dispatch_state=value['profile']['dispatch_state'])
            return
        if current is not None:
            # Same operation with changed producer, binding bytes, generation or
            # owner cannot refresh liveness, disarm, or alter the first claim.
            if scope != current.scope or value['binding_sha256'] != current.envelope['binding_sha256']:
                raise SupervisorError('SCOPE_MISMATCH')
            current.dispatch_state = self._join_dispatch(current.dispatch_state,value['profile']['dispatch_state'])
            if current.finished and kind in {'HEARTBEAT', 'ACTION_FINISHED'}:
                # A timely finish disarms liveness exactly once. Later samples
                # cannot manufacture an expiry or resume an already latched one.
                return
            if kind == 'HEARTBEAT':
                current.tracker.observe(scope,value['producer_seq'],received)
                return
            if kind == 'ACTION_FINISHED':
                # Check receiver time before disarming. In particular a late HB
                # may already have latched expiry in this same queue drain.
                expired = current.tracker.poll(received)
                if expired is not None:
                    current.pending_events.append((expired, None))
                    self._latch_trigger(current, expired)
                current.finished = True
                return
        elif kind in {'ACTION_FAILED','OBSERVATION_FAILED','MANUAL'}:
            context = self._context(value)
            invalidates = (kind, value['event']['failure_code'], value['profile']['reason']) in self._invalidating_events
            # Even operation-capacity backpressure must not hide an accepted
            # restriction for an already registered exact Binding/generation.
            self._restrict(context, revoke=invalidates)
            if len(self._operations) >= self.config.max_operations:
                raise SupervisorError('SPOOL_BACKPRESSURE')
            current = _Operation(scope,value,HeartbeatTracker(scope,received,
                expiry_seconds=self.config.heartbeat_expiry_seconds),context,
                dispatch_state=value['profile']['dispatch_state'])
            self._operations[key] = current
        if current and kind in {'ACTION_FAILED','OBSERVATION_FAILED','MANUAL'}:
            invalidates = (kind, value['event']['failure_code'], value['profile']['reason']) in self._invalidating_events
            self._restrict(current.context,revoke=invalidates)
            trigger = WatchdogTrigger(scope,TriggerKind.FAILURE_EVENT,received,
                                       ObservationSource.VERIFIED_DURABLE_EVENT)
            # Aggregate this tick's failure and authenticated process observations
            # before choosing a diagnostic action; preserve original event bytes.
            current.pending_events.append((trigger,value))

    def _claim(self, operation: _Operation, trigger: WatchdogTrigger,
               envelope: dict | None = None, *, already_durable=False) -> None:
        self._latch_trigger(operation, trigger)
        key = operation.scope.operation_key
        self.observations.append({'operation_id':key[1], 'reason':trigger.reason_code,
                                  'observed_monotonic':trigger.observed_monotonic,
                                  'source':trigger.source.value, 'metadata_only':trigger.metadata_only})
        # Keep bounded telemetry; the accepted journal and immutable claims own
        # durable evidence, not this convenience observation ring.
        self.observations[:] = self.observations[-256:]
        if operation.claimed or key in self._claimed:
            return
        operation.claimed = True
        self._claimed.add(key)
        selected = envelope if envelope is not None else self._derived(operation,trigger)
        self._pending.append(('capture',operation,selected,(trigger,already_durable)))

    def tick(self) -> None:
        if not self._started or self._stop.is_set():
            return
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            self.results.append(result)
            if result.get('worker_blocked'):
                self._block_new_workers = True
        if self._pump.error:
            self._latch_writer_failure(self._pump.error)
        for _ in range(512):
            try:
                envelope, received = self._pump.events.get_nowait()
            except queue.Empty:
                break
            try:
                # This monitoring thread is the receiver-clock authority. Queue
                # publication/producer clocks never move its anchor backwards.
                self._receive(envelope,self.monotonic())
            except (SpoolError,SupervisorError,WatchdogError):
                self.rejections += 1
        now = self.monotonic()
        if type(now) not in (float,int) or not math.isfinite(now) or (self._last_clock is not None and now < self._last_clock):
            self._block_new_workers = True
            self._block_reason = 'CLOCK_OR_KEEPER_LOST'
            self.diagnostic_only = True
            for context in self._contexts.values():
                self._restrict(context,revoke=True)
            raise SupervisorError('CLOCK_OR_KEEPER_LOST')
        self._last_clock = now
        for operation in tuple(self._operations.values()):
            pending = operation.pending_events
            operation.pending_events = []
            if operation.finished and not operation.claimed and not pending:
                continue
            triggers = [pair[0] for pair in pending]
            context = operation.context
            if context:
                for process, kind in ((context.chrome,TriggerKind.CHROME_EXITED),
                                      (context.consumer,TriggerKind.CONSUMER_EXITED)):
                    if process is not None:
                        try:
                            alive = process.is_alive()
                        except Exception:
                            triggers.append(WatchdogTrigger(operation.scope,TriggerKind.CLOCK_OR_KEEPER_LOST,
                                            now,ObservationSource.TRUSTED_KEEPER))
                        else:
                            if alive is False and kind not in operation.observed_exits:
                                operation.observed_exits.add(kind)
                                self._restrict(context,revoke=kind is TriggerKind.CHROME_EXITED)
                                triggers.append(WatchdogTrigger(operation.scope,kind,now,
                                                                ObservationSource.CONFIRMED_PROCESS_EXIT))
            expired = None
            if not operation.finished and not operation.claimed:
                try:
                    expired = operation.tracker.poll(now)
                except WatchdogError:
                    expired = WatchdogTrigger(operation.scope,TriggerKind.CLOCK_OR_KEEPER_LOST,
                                               now,ObservationSource.TRUSTED_KEEPER)
            if expired:
                triggers.append(expired)
            selected = select_trigger(triggers)
            if selected:
                # Capturing at most once does not release the live safety
                # obligation. Keeper loss after claim still revokes every
                # remaining phase and final publication permit immediately.
                self._latch_trigger(operation, selected)
                original = next((env for trigger,env in pending if trigger is selected),None)
                if operation.claimed or operation.scope.operation_key in self._claimed:
                    # Original producer events are already durable. Newly observed
                    # process loss after COMMIT is separate metadata, no rewrite.
                    if original is None:
                        self._pending.append(('record_only',self._derived(operation,selected),None,None))
                else:
                    self._claim(operation,selected,original,already_durable=original is not None)
        if (self._task is None or not self._task.is_alive()) and self._pending:
            item = self._pending.popleft()
            self._task = threading.Thread(target=self._execute_item,args=(item,),
                                          name='cdpd-capture-finalizer',daemon=True)
            self._task.start()

    @staticmethod
    def _deny(challenge: dict) -> dict:
        return {**challenge,'schema':'cdpd.capture-worker-permit/v1','allowed':False,
                'remaining_lease_seconds':0.0,'reason_code':'CLOCK_OR_KEEPER_LOST'}

    def _coordinator(self, permit: Callable) -> CaptureCoordinator:
        store = ProfileEvidenceStore(self.root/'bundles',windows_storage_authorizer=self._storage_authorizer)
        publication_gate = getattr(permit, '_publication_gate', None)
        return CaptureCoordinator(self.root/'capture_state',self.root/'spool',store,
                    runner=self.runner,permit=permit,utcnow=self.utcnow,monotonic=self.monotonic,
                    windows_state_authorizer=self._storage_authorizer, publication_gate=publication_gate)

    def _job(self, envelope: dict) -> WorkerRequest:
        saved = self._binding_metadata.get(self._binding_key(envelope))
        if saved is None:
            raise SupervisorError('NO_ACTIVE_BINDING')
        record = ParsedBinding.parse(saved['binding_record_raw_json'])
        event = DiagnosticEvent.from_dict(envelope['event'])
        capture = CaptureRequest(event,TargetBinding.from_dict(record.to_dict()['target']),
                                 timeout_seconds=self.config.capture_timeout_seconds)
        return WorkerRequest.from_dict({'schema':'cdpd.capture-worker-request/v1',
                    'capture_request':capture.to_dict(),'trigger_envelope':envelope,
                    'binding_record_raw_json':saved['binding_record_raw_json'],
                    'binding_record_sha256':saved['binding_record_sha256'],
                    'control_generation':envelope['control_generation'],
                    'capture_id':'C-'+uuid4().hex,'supervisor_instance_id':self.instance_id,
                    'policy_allowed':True})

    def _freeze_consumer_s0(self, directory: Path, operation: _Operation, job: WorkerRequest) -> dict:
        """Preserve the original consumer population without falsifying its producer.

        L06's closed bundle deliberately contains exactly the trigger producer's
        stream. L07 adds this separate durable, pinned context before invoking
        L06; it never widens the accepted Bundle schema or treats two populations
        as one loss counter. Consumer events retain their original exact bytes.
        """
        at = self.utcnow()
        history = read_history(self.root/'spool',stream=stream_key(operation.envelope),
                               now_utc=at.timestamp())
        context = {'schema':'cdpd.l07-consumer-s0/v1','run_id':operation.scope.run_id,
                   'operation_id':operation.scope.operation_id,
                   'source_stream':list(stream_key(operation.envelope)),
                   'capture_id':job.to_dict()['capture_id'],'job_sha256':hashlib.sha256(job._raw).hexdigest(),
                   'source_cutoff_seq':history['source_cutoff_seq'],
                   'frozen_at_utc':at.isoformat().replace('+00:00','Z'),
                   'dispatch_state':operation.dispatch_state,'history':history,
                   'meaning':'SEPARATE_ORIGINAL_CONSUMER_CONTEXT_NOT_BUNDLE_HISTORY_REPLACEMENT'}
        raw=canonical_json(context)
        if len(raw)>1310720:
            raise SupervisorError('SPOOL_BACKPRESSURE')
        _write_new(directory/'L07_S0.json',raw)
        pin={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'file':'L07_S0.json'}
        _write_new(directory/'L07_S0_RECEIPT.json',canonical_json(pin))
        _sync_directory(directory)
        return pin

    @staticmethod
    def _verify_consumer_s0(directory: Path, job: WorkerRequest) -> dict:
        raw=_read_regular(directory/'L07_S0.json',1310720)
        pin=strict_json(_read_regular(directory/'L07_S0_RECEIPT.json',1024),max_bytes=1024)
        expected={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'file':'L07_S0.json'}
        context=strict_json(raw,max_bytes=1310720)
        if (pin!=expected or context.get('schema')!='cdpd.l07-consumer-s0/v1'
                or context.get('job_sha256')!=hashlib.sha256(job._raw).hexdigest()
                or context.get('capture_id')!=job.to_dict()['capture_id']):
            raise SupervisorError('INPUT_INVALID')
        return pin

    def _scoped_permit(self, context: _LiveContext | None) -> Callable:
        key = None if context is None else (
            ParsedBinding.parse(context.raw).binding_id,context.generation,
            hashlib.sha256(context.raw.encode()).hexdigest())
        def still_allowed():
            return (not self._stop.is_set() and not self.worker_blocked
                    and context is not None and self._contexts.get(key) is context
                    and context.restriction.confirmed and context.restriction.target!='REVOKE'
                    and not context.guard._terminal)
        def permit(challenge):
            if context is None:
                return self._deny(challenge)
            if context.publication_gate.invalidated:
                return self._deny(challenge)
            # A terminal observation is evidence, not an operation permit. Keep
            # the accepted first-cause/UNKNOWN notification path even on stop.
            if is_terminal(challenge):
                return context.permit(challenge)
            if self._stop.is_set() or self.worker_blocked:
                return self._deny(challenge)
            context.restriction.wait(.25)
            if not still_allowed():
                return self._deny(challenge)
            # Recheck after the wait: a stop during S0/permit cannot spawn later.
            if self._stop.is_set() or self.worker_blocked:
                return self._deny(challenge)
            response=context.permit(challenge)
            # The bridge itself may block. A late answer cannot cross a stop,
            # new terminal restriction, owner change, or cleanup uncertainty.
            if not still_allowed():
                return self._deny(challenge)
            return response
        permit._publication_gate = context.publication_gate if context is not None else None
        return permit

    def _execute_item(self, item) -> None:
        result = {'schema':'cdpd.supervisor-outcome/v1','status':'STORAGE_FAILED','bundle_path':None,'worker_blocked':False,
                  'supervisor_instance_id':self.instance_id}
        directory = None
        coordinator = None
        context = None
        ran_coordinator = False
        try:
            if item[0] == 'record_only':
                self._pump.append(item[1],self.config.storage_wait_seconds)
                result.update(status='RECORDED_METADATA',operation_id=item[1]['event']['operation_id'])
                return
            if item[0] == 'recover':
                _,directory,job,_ = item
                result.update(recovery=True,operation_id=job.to_dict()['trigger_envelope']['event']['operation_id'])
                s0 = self.root/'capture_state/incidents'/job.to_dict()['capture_id']/'S0.json'
                if not s0.is_file():
                    result.update(status='PARTIAL',failure_code='SUPERVISOR_LOST',
                                  evidence_scope='CLAIM_METADATA_ONLY_S0_WAS_NOT_DURABLE',worker_blocked=True)
                    return
                result['consumer_s0']=self._verify_consumer_s0(directory,job)
                coordinator = self._coordinator(self._deny)
            else:
                _,operation,envelope,(trigger,durable) = item
                result.update(recovery=False,operation_id=operation.scope.operation_id,
                              trigger_reason=envelope['profile']['reason'])
                job = self._job(envelope)
                directory = self.root/'operations'/_pin(list(operation.scope.operation_key))
                if directory.exists():
                    result.update(status='DEDUPED',failure_code='ATTEMPT_ALREADY_CLAIMED')
                    return
                directory.mkdir(mode=0o700)
                claim = {'schema':'cdpd.supervisor-operation-claim/v1','run_id':operation.scope.run_id,
                         'job':job.to_dict(),'job_sha256':hashlib.sha256(job._raw).hexdigest(),
                         'scope':'ONCE_PER_RUN_OPERATION_ACROSS_PRODUCER_BINDING_GENERATION',
                         'spawn_may_have_occurred':True,'created_utc':_stamp()}
                _write_new(directory/'CLAIM.json',canonical_json(claim)); _sync_directory(directory)
                _sync_directory(directory.parent)
                if not durable:
                    self._pump.append(envelope,self.config.storage_wait_seconds)
                result['consumer_s0']=self._freeze_consumer_s0(directory,operation,job)
                context = operation.context
                coordinator = self._coordinator(self._scoped_permit(context))
            # Do not attach a previous capture's process facts to metadata-only
            # work or a restart recovery that never called the process runner.
            self.runner.last_process_metadata={}
            ran_coordinator=True
            outcome = coordinator.capture(job)
            result.update(status=outcome.capture_status,bundle_path=outcome.bundle_path,
                          failure_code=outcome.failure_code,capture_id=job.to_dict()['capture_id'],
                          s0_path=coordinator.last_s0_path,
                          capture_metadata=coordinator.last_capture_metadata)
        except (SupervisorError,CaptureServiceError) as error:
            result.update(status=error.code,failure_code=error.code)
            if isinstance(error,CaptureServiceError) and error.original_failure_code:
                result['original_failure_code']=error.original_failure_code
            if error.code=='BINDING_INVALIDATION_UNCONFIRMED':
                self._block_new_workers=True
        except Exception:
            result.update(status='STORAGE_FAILED',failure_code='STORAGE_FAILED')
        finally:
            metadata = (dict(getattr(self.runner,'last_process_metadata',{}) or {}) if ran_coordinator else {})
            if not metadata:
                metadata={'spawn_attempt_count':0,'spawn_count':0,'pid':None,'termination':'NOT_STARTED'}
            result['process_metadata'] = metadata
            if context is not None:
                context.restriction.wait(.25)
                result['shared_authority_notification']=context.restriction.status
                if context.restriction.status=='UNCONFIRMED':
                    self._block_new_workers=True
                    self._block_reason='SHARED_AUTHORITY_UNCONFIRMED'
            if self.worker_blocked or metadata.get('new_worker_prohibited'):
                self._block_new_workers=True
                result['worker_blocked']=True
                if self._block_reason is None:
                    resource_only = ((metadata.get('resource_cleanup_unconfirmed')
                                      or getattr(self.runner, 'resource_cleanup_unconfirmed', False))
                                     and not metadata.get('termination_unconfirmed')
                                     and not getattr(self.runner, 'termination_unconfirmed', False)
                                     and metadata.get('termination') != 'UNCONFIRMED')
                    self._block_reason = ('WORKER_RESOURCE_CLEANUP_UNCONFIRMED' if resource_only
                                          else 'WORKER_TERMINATION_UNCONFIRMED')
                try:
                    path=self.root/'WORKER_BLOCKED.json'
                    if not path.exists():
                        _write_new(path,canonical_json({'schema':'cdpd.supervisor-worker-block/v1',
                            'reason':self._block_reason or 'WORKER_TERMINATION_UNCONFIRMED','automatic_clear':False,
                            'supervisor_instance_id':self.instance_id}));_sync_directory(self.root)
                except Exception:
                    result['block_persistence']='FAILED_IN_MEMORY_BLOCK_RETAINED'
            if coordinator is not None:
                result['s0_path']=coordinator.last_s0_path
            if directory is not None and (directory/'CLAIM.json').is_file():
                try:
                    if not (directory/'OUTCOME.json').exists():
                        _write_new(directory/'OUTCOME.json',canonical_json(result));_sync_directory(directory)
                except Exception:
                    result['outcome_persistence']='FAILED_CLAIM_REMAINS_NO_REPLAY'
            self._results.put(result)

    def run(self, *, stop_event: threading.Event | None = None) -> None:
        if not self._started:
            self.start()
        while not self._stop.is_set() and not (stop_event and stop_event.is_set()):
            self.tick()
            self._stop.wait(self.config.poll_seconds)

    def stop(self) -> None:
        self._stop.set()
        cancel = getattr(self.runner,'cancel_active',None)
        if callable(cancel):
            cancel()

    def close(self) -> None:
        self.stop()
        if self._task:
            self._task.join(2.25)
        if self._pump:
            self._pump.stop.set(); self._pump.thread.join(2.0)
        if (self._task and self._task.is_alive()) or (self._pump and self._pump.thread.is_alive()):
            # Keep ownership until process exit; never advertise clean shutdown
            # while a blocked writer/finalizer still owns work.
            raise SupervisorError('SUPERVISOR_STOP_UNCONFIRMED')
        for context in self._contexts.values():
            for process in (context.consumer,context.chrome):
                if process is not None:
                    process.close()
        if self._owner_lock:
            self._owner_lock.close();self._owner_lock=None
        if self._profile_lock:
            self._profile_lock.close();self._profile_lock=None
        self._started=False


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description='Independent CDPD L07 Supervisor; never starts a consumer or browser.')
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--profile-directory',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--poll-seconds',type=float,default=.25)
    parser.add_argument('--bootstrap',help='Explicit trusted installed module:function receiving this Supervisor; no JSON authority.')
    args=parser.parse_args(argv)
    supervisor=None
    try:
        supervisor=Supervisor(SupervisorConfig(args.root,args.profile_directory,args.run_id,args.poll_seconds))
        supervisor.start()
        if args.bootstrap:
            if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*',args.bootstrap):
                raise SupervisorError('INPUT_INVALID')
            module,name=args.bootstrap.split(':')
            bootstrap=getattr(importlib.import_module(module),name)
            if not callable(bootstrap):
                raise SupervisorError('INPUT_INVALID')
            bootstrap(supervisor)
        signal.signal(signal.SIGINT,lambda *_:supervisor.stop())
        signal.signal(signal.SIGTERM,lambda *_:supervisor.stop())
        print(canonical_json({'status':'READY','supervisor_instance_id':supervisor.instance_id,
                              'live_binding_count':len(supervisor._contexts),
                              'saved_records_are_authority':False}).decode(),flush=True)
        supervisor.run()
        supervisor.close();return 0
    except Exception as error:
        code=getattr(error,'code','STORAGE_FAILED')
        if supervisor:
            try:supervisor.close()
            except Exception:pass
        print(canonical_json({'status':'BLOCKED','reason':code}).decode(),flush=True)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
