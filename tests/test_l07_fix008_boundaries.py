"""FIX008 synthetic-only regression: parent lock and EOF/exit boundary.

Do not weaken real ProfileEvidenceStore, Permit, deadline, or RESULT validity.
"""
from __future__ import annotations

import hashlib
import subprocess
import time

import pytest

from cdpd_chatgpt_profile.process_runner import _settle_failed_child
from cdpd_chatgpt_profile.spool import _FileLock
from test_l06_capture_service import assert_parent_capture_lock_held, snapshot_child_invariance


class FakeOwnedChild:
    def __init__(self, *, already_exited=False, exit_during_wait=False):
        self.exited = already_exited
        self.exit_during_wait = exit_during_wait
        self.calls = []

    def poll(self):
        self.calls.append('poll')
        return 73 if self.exited else None

    def wait(self, *, timeout):
        self.calls.append(('wait', timeout))
        if self.exit_during_wait:
            self.exited = True
            return 73
        if self.exited:
            return 73
        raise subprocess.TimeoutExpired('fake', timeout)

    def kill(self):
        self.calls.append('kill')
        self.exited = True


def test_fix008_eof_allows_natural_exit_without_kill():
    child = FakeOwnedChild(exit_during_wait=True)
    result = _settle_failed_child(child, eof_observed=True, deadline=time.monotonic() + 1.0)
    assert result == 'EXITED_ERROR'
    assert 'kill' not in child.calls
    assert any(type(x) is tuple and 0 < x[1] <= 0.10 for x in child.calls)


def test_fix008_eof_still_live_is_killed_without_extra_deadline():
    child = FakeOwnedChild()
    result = _settle_failed_child(child, eof_observed=True, deadline=time.monotonic() + 1.0)
    assert result == 'KILLED'
    assert 'kill' in child.calls
    assert any(type(x) is tuple and x[1] <= 0.10 for x in child.calls)


def test_fix008_no_eof_never_waits_for_natural_exit():
    child = FakeOwnedChild()
    result = _settle_failed_child(child, eof_observed=False, deadline=time.monotonic() + 1.0)
    assert result == 'KILLED'
    assert child.calls.count('kill') == 1
    assert all(type(x) is not tuple or x[1] == 0.5 for x in child.calls)


def test_fix008_expired_deadline_cannot_add_grace():
    child = FakeOwnedChild()
    result = _settle_failed_child(child, eof_observed=True, deadline=time.monotonic() - 0.1)
    assert result == 'KILLED'
    assert not any(type(x) is tuple and x[1] <= 0.10 for x in child.calls)


def test_fix008_already_exited_is_not_killed():
    child = FakeOwnedChild(already_exited=True)
    assert _settle_failed_child(child, eof_observed=False, deadline=time.monotonic() - 1) == 'EXITED_ERROR'
    assert 'kill' not in child.calls


def test_fix008_no_child_remains_not_started():
    assert _settle_failed_child(None, eof_observed=True, deadline=time.monotonic() + 1) == 'NOT_STARTED'


def test_fix008_own_parent_lock_excluded_but_spool_and_evidence_tracked(tmp_path):
    lock = tmp_path / 'state/capture-writer.lock'
    lock.parent.mkdir()
    lock.write_bytes(b'0')
    spool = tmp_path / 'spool/events'
    spool.parent.mkdir()
    spool.write_bytes(b'original')
    evidence = tmp_path / 'bundles/COMMIT.json'
    evidence.parent.mkdir()
    evidence.write_bytes(b'committed')
    with _FileLock(lock):
        assert_parent_capture_lock_held(tmp_path)
        before = snapshot_child_invariance(tmp_path)
        assert 'state/capture-writer.lock' not in before
        assert 'spool/events' in before and 'bundles/COMMIT.json' in before
        assert before == snapshot_child_invariance(tmp_path)
        spool.write_bytes(b'mutated')
        assert snapshot_child_invariance(tmp_path) != before
        spool.write_bytes(b'original')
        assert snapshot_child_invariance(tmp_path) == before
        evidence.write_bytes(b'mutated')
        assert snapshot_child_invariance(tmp_path) != before
        assert_parent_capture_lock_held(tmp_path)
    assert lock.read_bytes() == b'0'


def test_fix008_other_lock_file_is_not_excluded(tmp_path):
    lock = tmp_path / 'state/capture-writer.lock'
    lock.parent.mkdir()
    lock.write_bytes(b'0')
    other_lock = tmp_path / 'state/other.lock'
    other_lock.write_bytes(b'1')
    with _FileLock(lock):
        first = snapshot_child_invariance(tmp_path)
        assert 'state/other.lock' in first
        other_lock.write_bytes(b'2')
        assert first != snapshot_child_invariance(tmp_path)

