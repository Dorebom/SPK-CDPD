"""FIX002 publication-order closure for R2 F001/F002.

The tests pause after COORDINATOR_COMMIT permit has succeeded but while the
already-fsynced COMMIT marker is still staged.  Local invalidation must remain
non-blocking there and must prevent atomic publication.  A second control
pauses inside the atomic rename: invalidation cannot be *accepted* until the
rename linearization point has completed, after which the committed bytes stay
unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

import pytest

import cdpd_chatgpt_profile.evidence_store as storemod
from cdpd_chatgpt_profile.capture_service import CaptureServiceError
from cdpd_chatgpt_profile.evidence_store import verify_profile_bundle
from cdpd_chatgpt_profile.owned_process import RegisteredProcess, process_creation_identity
from test_l06_capture_service import snapshot
from test_l07_fix001_supervisor import (
    INVALIDATE, begin, durable_receive, event, subject,
)


def _run_capture(coordinator, job, box):
    try:
        box['outcome'] = coordinator.capture(job)
    except Exception as exc:  # inspected by the test
        box['error'] = exc


def _pause_staged_commit(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = storemod._write_new

    def blocked(path, raw):
        original(path, raw)
        if Path(path).name == '.COMMIT.staged':
            entered.set()
            assert release.wait(5), 'test failed to release staged COMMIT'
    monkeypatch.setattr(storemod, '_write_new', blocked)
    return entered, release


@pytest.mark.parametrize('row', INVALIDATE, ids=lambda row: row['case_id'])
def test_r2_matrix_invalidation_after_final_permit_prevents_actual_commit(tmp_path, monkeypatch, row):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    job = sup._job(initial)
    coordinator = sup._coordinator(sup._scoped_permit(operation.context))
    entered, release = _pause_staged_commit(monkeypatch)
    box = {}
    worker = threading.Thread(target=_run_capture, args=(coordinator, job, box), daemon=True)
    worker.start()
    assert entered.wait(5)

    # This is the accepted invalidation.  It must complete while storage is
    # paused, proving that the watchdog/local latch does not wait on that I/O.
    durable_receive(sup, event(row=row, seq=2))
    sup.tick()
    assert operation.context.publication_gate.invalidated is True
    assert worker.is_alive()

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert isinstance(box.get('error'), CaptureServiceError)
    assert box['error'].code == 'PUBLISH_GUARD_DENIED'
    bundle = sup.root / 'bundles' / job.to_dict()['capture_id']
    assert not (bundle / 'COMMIT.json').exists()
    assert not (bundle / 'STRUCTURE.json').exists()
    assert not (bundle / '.COMMIT.staged').exists()
    assert verify_profile_bundle(bundle)['publication_state'] == 'INCOMPLETE'


def test_r2_keeper_loss_after_final_permit_prevents_actual_commit(tmp_path, monkeypatch):
    ref = RegisteredProcess.open(os.getpid(), process_creation_identity(os.getpid()))
    try:
        sup, guard, authority, now = subject(tmp_path, process=ref)
        initial, operation = begin(sup)
        job = sup._job(initial)
        coordinator = sup._coordinator(sup._scoped_permit(operation.context))
        entered, release = _pause_staged_commit(monkeypatch)
        box = {}
        worker = threading.Thread(target=_run_capture, args=(coordinator, job, box), daemon=True)
        worker.start()
        assert entered.wait(5)

        ref.close()
        sup.tick()
        assert operation.context.publication_gate.invalidated is True
        assert worker.is_alive()

        release.set()
        worker.join(5)
        assert isinstance(box.get('error'), CaptureServiceError)
        assert box['error'].code == 'PUBLISH_GUARD_DENIED'
        bundle = sup.root / 'bundles' / job.to_dict()['capture_id']
        assert not (bundle / 'COMMIT.json').exists()
        assert not (bundle / 'STRUCTURE.json').exists()
    finally:
        ref.close()


def test_r2_commit_linearization_wins_before_later_invalidation(tmp_path, monkeypatch):
    sup, guard, authority, now = subject(tmp_path)
    initial, operation = begin(sup)
    job = sup._job(initial)
    coordinator = sup._coordinator(sup._scoped_permit(operation.context))

    entered = threading.Event()
    release = threading.Event()
    original_replace = storemod.os.replace

    def blocked_replace(src, dst):
        if Path(src).name == '.COMMIT.staged' and Path(dst).name == 'COMMIT.json':
            entered.set()
            assert release.wait(5), 'test failed to release atomic publication'
        return original_replace(src, dst)
    monkeypatch.setattr(storemod.os, 'replace', blocked_replace)

    box = {}
    worker = threading.Thread(target=_run_capture, args=(coordinator, job, box), daemon=True)
    worker.start()
    assert entered.wait(5)

    invalidation_done = threading.Event()
    def invalidate():
        durable_receive(sup, event(row=INVALIDATE[0], seq=2))
        sup.tick()
        invalidation_done.set()
    invalidator = threading.Thread(target=invalidate, daemon=True)
    invalidator.start()
    time.sleep(.05)
    # The only wait is the tiny publication linearization section.  The
    # invalidation is not yet accepted while COMMIT rename owns that point.
    assert not invalidation_done.is_set()

    release.set()
    worker.join(5)
    invalidator.join(5)
    assert 'error' not in box
    assert invalidation_done.is_set()
    bundle = Path(box['outcome'].bundle_path)
    assert (bundle / 'COMMIT.json').is_file()
    before = snapshot(bundle)
    # Invalidation has now been accepted after publication; it cannot rewrite it.
    sup.tick()
    assert before == snapshot(bundle)
    assert operation.context.publication_gate.published is True
    assert operation.context.publication_gate.invalidated is True
