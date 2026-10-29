"""Optional metadata instrumentation. Never exports prompts/results/debug dictionaries."""
from __future__ import annotations

from queue import Empty, Full, Queue
from threading import Lock
from typing import Any, Mapping, Protocol
from uuid import uuid4

from cdpd import DiagnosticEvent
from chatgpt_web_api.api import _binding_postcheck_failed_result, _binding_postcheck_reason
from chatgpt_web_api.facade_contract import validate_facade_contract
from chatgpt_web_api.target_guard import GuardError

from .l03_binding import L03BindingBridge


class EventSink(Protocol):
    def try_emit(self, event: DiagnosticEvent) -> bool:
        """Must not block. Built-in BoundedEventQueue provides this guarantee."""
        ...


class BoundedEventQueue:
    def __init__(self, capacity: int = 256) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 10000:
            raise ValueError('capacity must be 1..10000')
        self._queue: Queue[DiagnosticEvent] = Queue(maxsize=capacity)
        self.dropped = 0

    def try_emit(self, event: DiagnosticEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            self.dropped += 1
            return False

    def drain(self) -> list[DiagnosticEvent]:
        result = []
        # Bounded iteration even when a producer continues emitting.
        for _ in range(self._queue.maxsize):
            try:
                result.append(self._queue.get_nowait())
            except Empty:
                break
        return result


_FAILURE_MAP = {
    'SESSION_BINDING_FAILURE': 'TARGET_MISSING', 'TARGET_IDENTITY_MISMATCH': 'TARGET_MISSING',
    'AMBIGUOUS_OBSERVATION': 'TARGET_AMBIGUOUS', 'TIMEOUT': 'TIMEOUT', 'DISCONNECTED': 'DISCONNECTED',
    'RECONCILE_REQUIRED': 'RECONCILE_REQUIRED', 'POSTCONDITION_NOT_ESTABLISHED': 'POSTCONDITION_FAILED',
}


class InstrumentedFacade:
    """Wrap the three explicit facade operations; call each provider method exactly once.

    Diagnostic failure never changes a result or causes a retry. This candidate has
    no automatic capture worker and no heartbeat. Drain events in another process
    or in a consumer-controlled boundary. `operation_id` is an injected stable ID.
    """
    def __init__(self, provider: Any, sink: EventSink, *, producer_id: str = 'chatgpt-web',
                 producer_instance_id: str | None = None, correlation: Mapping[str, str] | None = None) -> None:
        # This validator belongs to the original provider. The wrapper's extra
        # operation_id request field intentionally does not claim facade/v2.
        validate_facade_contract(provider)
        self.provider = provider
        self.sink = sink
        self.producer_id = producer_id
        self.instance_id = producer_instance_id or str(uuid4())
        self.correlation = dict(correlation or {})
        self._lock = Lock()
        self._active = False
        self._reconciliation_required = False
        self._attempts: dict[tuple[str, str], dict[str, Any]] = {}
        # No provider/browser operation occurs in the constructor.
        DiagnosticEvent(self.producer_id, self.instance_id, 'configuration', 'MANUAL', correlation=self.correlation)

    @property
    def operation_records(self) -> tuple[dict[str, Any], ...]:
        """Safe metadata only; never includes provider requests/results/errors."""
        with self._lock:
            return tuple(dict(record) for record in self._attempts.values())

    def _emit(self, operation_id: str, kind: str, failure: str = 'NONE') -> bool:
        try:
            return self.sink.try_emit(DiagnosticEvent(self.producer_id, self.instance_id, operation_id, kind,
                                                       failure_code=failure, correlation=self.correlation)) is True
        except BaseException:
            return False

    def _session(self) -> Any:
        session = getattr(self.provider, 'session', None)
        if not all(callable(getattr(session, name, None)) for name in (
            'check_operation', 'begin_instrumented_operation',
            'end_instrumented_operation', 'begin_instrumented_provider',
            'end_instrumented_provider', 'require_reconciliation',
        )):
            raise GuardError('BINDING_REQUIRED')
        return session

    def _reconcile(self, session: Any) -> None:
        # The local latch remains effective even if the injected notification
        # itself fails. No later operation_id can turn an uncertain send into a
        # fresh send. A new binding/work boundary is an external responsibility.
        self._reconciliation_required = True
        try:
            session.require_reconciliation()
        except BaseException:
            pass

    def _call(self, method: str, operation_id: str, request: dict[str, Any], success_field: str) -> Any:
        # Reject malformed metadata before any action, rather than send without a valid operation ID.
        DiagnosticEvent(self.producer_id, self.instance_id, operation_id, 'ACTION_STARTED', correlation=self.correlation)
        key = (method, operation_id)
        with self._lock:
            if self._active or self._reconciliation_required or key in self._attempts or len(self._attempts) >= 10000:
                raise GuardError('RECONCILIATION_REQUIRED')
            self._active = True
            record: dict[str, Any] = {
                'operation_id': operation_id,
                'method': method,
                'provider_calls': 0,
                'provider_dispatched': False,
                'send_state': 'NOT_DISPATCHED',
                'outcome': 'ADMITTED',
                'postcheck_failed': False,
                'hook_failed': False,
            }
            self._attempts[key] = record
        session: Any = None
        reservation: Any = None
        try:
            try:
                session = self._session()
                # The session owns admission across all wrappers. A new wrapper
                # cannot replay an ID or enter recursively from an event hook.
                reservation = session.begin_instrumented_operation(method, operation_id)
                session.check_operation(mutation=True)
                record['hook_failed'] = not self._emit(operation_id, 'ACTION_STARTED')
                # A custom hook may have changed state. Never dispatch based on
                # the check that preceded invoking that hook.
                session.check_operation(mutation=True)
            except BaseException:
                record['outcome'] = 'GUARD_REJECTED'
                raise
            try:
                invoke = getattr(self.provider, method)
                session.begin_instrumented_provider(reservation)
                try:
                    record['provider_calls'] = 1
                    record['provider_dispatched'] = True
                    record['send_state'] = 'UNKNOWN'
                    result = invoke(**request)
                finally:
                    # A diagnostic hook can call the wrapper or original
                    # Facade, but neither is authorized outside this one phase.
                    try:
                        session.end_instrumented_provider(reservation)
                    except BaseException:
                        record['provider_phase_end_failed'] = True
                        self._reconcile(session)
            except BaseException:
                record['outcome'] = 'PROVIDER_EXCEPTION'
                self._reconcile(session)
                record['hook_failed'] = not self._emit(operation_id, 'ACTION_FAILED', 'UNKNOWN') or record['hook_failed']
                raise  # Preserve the exact original exception object and traceback.
            record['outcome'] = 'PROVIDER_RETURNED'
            postcheck_error: BaseException | None = None
            try:
                session.check_operation(mutation=False)
            except BaseException as error:
                postcheck_error = error
                record['postcheck_failed'] = True
                self._reconcile(session)
            try:
                # The direct Facade may have observed a transient failure that
                # this second read-only check cannot reproduce. Its explicit
                # failure marker remains authoritative for this operation.
                prior_postcheck_reason = _binding_postcheck_reason(result)
                if postcheck_error is not None or prior_postcheck_reason is not None:
                    postcheck_error = (GuardError(prior_postcheck_reason) if prior_postcheck_reason
                                       else postcheck_error)
                    code_error = (postcheck_error if isinstance(postcheck_error, GuardError)
                                  else GuardError('TARGET_UNVERIFIED'))
                    record['postcheck_failed'] = True
                    record['postcheck_reason'] = code_error.code
                    record['postcheck_failure_kind'] = code_error.failure_kind.value
                    self._reconcile(session)
                    result = _binding_postcheck_failed_result(result, code_error, operation_id=operation_id)
                success = bool(getattr(result, success_field, False))
                failure = getattr(result, 'failure_kind', None)
                failure = getattr(failure, 'value', failure)
                code = 'NONE' if success else _FAILURE_MAP.get(failure, 'UNKNOWN')
                if record['postcheck_failed']:
                    success = False
                    code = _FAILURE_MAP.get(record['postcheck_failure_kind'], 'UNKNOWN')
                if method == 'execute_prompt_and_wait':
                    submission = getattr(result, 'submission', None)
                    attempted = bool(getattr(submission, 'activation_attempted', False))
                    established = bool(getattr(submission, 'web_submit_established', False))
                    record['activation_attempted'] = attempted
                    record['web_submit_established'] = established
                    record['send_state'] = 'ESTABLISHED' if established else ('UNKNOWN' if attempted else 'NOT_ESTABLISHED')
                    if not success and (attempted or bool(getattr(submission, 'ok', False))):
                        self._reconcile(session)
                else:
                    record['send_state'] = 'PROVIDER_RETURNED'
                if failure == 'RECONCILE_REQUIRED':
                    self._reconcile(session)
            except BaseException:
                # Result introspection is diagnostics, not a second provider
                # call. Preserve the returned object even when a property fails.
                self._reconcile(session)
                record['observation_failed'] = True
                record['hook_failed'] = not self._emit(operation_id, 'OBSERVATION_FAILED', 'UNKNOWN') or record['hook_failed']
            else:
                record['hook_failed'] = not self._emit(operation_id, 'ACTION_FINISHED' if success else 'ACTION_FAILED', code) or record['hook_failed']
            if record['hook_failed']:
                self._reconcile(session)
            return result
        finally:
            if reservation is not None:
                try:
                    session.end_instrumented_operation(reservation)
                except BaseException:
                    self._reconcile(session)
            with self._lock:
                self._active = False

    def open_project_chat(self, *, operation_id: str, project_name: str, project_key: str, client_ref: str, reuse: bool) -> Any:
        return self._call('open_project_chat', operation_id, dict(project_name=project_name, project_key=project_key, client_ref=client_ref, reuse=reuse), 'ok')

    def execute_prompt_and_wait(self, *, operation_id: str, chat: Any, prompt: str, attachments: tuple) -> Any:
        return self._call('execute_prompt_and_wait', operation_id, dict(chat=chat, prompt=prompt, attachments=attachments), 'completed')

    def download_response_zip(self, *, operation_id: str, response: Any, expected_filename: str, destination_dir: Any) -> Any:
        return self._call('download_response_zip', operation_id, dict(response=response, expected_filename=expected_filename, destination_dir=destination_dir), 'ok')
