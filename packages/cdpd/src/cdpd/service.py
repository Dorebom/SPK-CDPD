"""Synchronous SDK slice. Hard process-kill supervision and durable spool are later work."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .collector import CaptureError
from .contracts import CaptureRequest, DiagnosticEvent
from .store import EvidenceStore


class StructureCollector(Protocol):
    def collect(self, request: CaptureRequest) -> dict[str, Any]: ...


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CaptureOutcome:
    capture_status: str
    bundle_path: str
    failure_code: str | None
    assessment_status: str = 'NOT_PERFORMED'


class DiagnosticService:
    """Generic, non-owning diagnostics. Does not control or replay application work."""
    def __init__(self, collector: StructureCollector, store: EvidenceStore, *, max_events: int = 1000, retention_seconds: float = 120) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 1000:
            raise ValueError('max_events must be 1..1000')
        if not 0 < retention_seconds <= 3600:
            raise ValueError('retention_seconds must be 0..3600')
        self.collector = collector
        self.store = store
        self.retention_seconds = retention_seconds
        self._events: deque[tuple[float, dict[str, Any]]] = deque(maxlen=max_events)
        self.dropped_events = 0

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {'contract': 'cdpd.diagnostics', 'major_version': 1, 'implementation_version': '0.2.1',
                'implemented': ['event.record.memory', 'capture.structure.sync', 'bundle.verify', 'cli.json'],
                'not_implemented': ['daemon.ipc', 'watchdog', 'durable.spool', 'baseline.diff', 'scheduler', 'raw.forensic', 'all.frames'],
                'production_ready': False}

    def record(self, event: DiagnosticEvent) -> bool:
        if not isinstance(event, DiagnosticEvent):
            raise TypeError('DiagnosticEvent required')
        now = time.monotonic()
        while self._events and now - self._events[0][0] > self.retention_seconds:
            self._events.popleft()
        if len(self._events) == self._events.maxlen:
            self.dropped_events += 1
        self._events.append((now, {'received_utc': _utc(), 'event': event.to_dict()}))
        return True

    def capture(self, request: CaptureRequest) -> CaptureOutcome:
        if not isinstance(request, CaptureRequest):
            raise TypeError('CaptureRequest required')
        start = _utc()
        frozen = [value for t, value in self._events if time.monotonic() - t <= self.retention_seconds and
                  value['event']['producer_id'] == request.event.producer_id and value['event']['producer_instance_id'] == request.event.producer_instance_id]
        failure = None
        structure = None
        status = 'COMPLETE'
        try:
            structure = self.collector.collect(request)
            # The built-in collector is trusted code. Third-party plugins are NOT sandboxed.
            if not isinstance(structure, dict) or structure.get('schema') != 'cdpd.structure/v1':
                raise CaptureError('STRUCTURE_INVALID')
            if structure.get('truncated'):
                status = 'PARTIAL'
        except CaptureError as exc:
            status = 'REJECTED' if exc.code == 'POLICY_DENIED' else 'PARTIAL'
            failure = exc.code
            structure = None
        except Exception:
            status, failure, structure = 'PARTIAL', 'COLLECTOR_EXCEPTION', None
        # No endpoint URL, raw exception, prompt, result text or provider debug dict is stored.
        files = {
            'TRIGGER.json': request.event.to_dict(),
            'RESULT.json': {'schema': 'cdpd.capture-result/v1', 'capture_status': status, 'failure_code': failure,
                            'assessment_status': 'NOT_PERFORMED', 'started_utc': start, 'finished_utc': _utc(),
                            'selected_scope': 'MAIN_FRAME_AND_OPEN_SHADOW', 'history_dropped_count': self.dropped_events,
                            'target_id': request.target.target_id},
            'EVENTS.json': {'schema': 'cdpd.events/v1', 'events': frozen},
        }
        if structure is not None:
            files['STRUCTURE.json'] = structure
        path = self.store.commit(files)  # Storage error propagates: never claim evidence was saved.
        return CaptureOutcome(status, str(path), failure)
