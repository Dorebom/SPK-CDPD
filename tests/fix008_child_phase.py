"""Opt-in FIX008 *test fixture only* child phase milestones.

Disabled by default. When enabled, creates an independent bounded JSONL phase
sidecar, never a protocol frame. No PID, file path, nonce, request, pipe
payload, stderr, or browser/DOM data is stored. Diagnostic timing cannot be
used to assert the 3-second formal performance gate.
"""
from __future__ import annotations

import json
import os
import time

_ZERO = time.monotonic()
_ALLOWED = frozenset({
    'fixture_start', 'product_import_begin', 'product_import_end',
    'stdin_request_read', 'first_challenge_write', 'fixture_serve_begin',
    'fixture_serve_end',
})
_written = set()


def enabled() -> bool:
    name = os.environ.get('CDPD_FIX008_CHILD_PHASE_LOG', '')
    return bool(name) and os.path.isabs(name)


def milestone(phase: str) -> None:
    if phase not in _ALLOWED or phase in _written or not enabled():
        return
    _written.add(phase)
    raw = json.dumps({'phase': phase,
        'elapsed_ms': round(max(0, (time.monotonic() - _ZERO) * 1000), 3)},
        sort_keys=True).encode('ascii') + b'\n'
    name = os.environ['CDPD_FIX008_CHILD_PHASE_LOG']
    # This sidecar is owned by the *local synthetic test*; the production
    # Worker CLI is never instrumented. Fail silently, never block IPC.
    try:
        fd = os.open(name, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
    except OSError:
        pass


def instrument_fixture_worker(worker_cli):
    """Instrument only in a test fixture with explicit diagnostic opt-in."""
    if not enabled():
        return
    original_next = worker_cli._LineReader.next
    received = [False]

    def observed_next(self, deadline):
        item = original_next(self, deadline)
        if not received[0] and item[0] == 'LINE':
            received[0] = True
            milestone('stdin_request_read')
        return item
    worker_cli._LineReader.next = observed_next
