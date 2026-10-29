"""FIX004 contract tests; Win32 branches are *synthetic* on any host.

Test-only simulated os.name and scoped grants do not qualify real Windows
ACLs, directory FlushFileBuffers, native crash consistency or Job Objects.
"""
from __future__ import annotations

import os as real_os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cdpd_chatgpt_profile import evidence_store as es
from cdpd_chatgpt_profile.spool import JournalSpool
from test_l06_bundle_contract import _good, _id

pytestmark = pytest.mark.no_windows_synthetic_capability


class _SimulatedWindowsOS:
    name = 'nt'

    def __getattr__(self, name):
        return getattr(real_os, name)


@pytest.fixture
def win_branch(monkeypatch):
    # Patch module namespace only, not global os.name or pathlib semantics.
    monkeypatch.setattr(es, 'os', _SimulatedWindowsOS())
    # Successful directory sync is an explicit synthetic model for this scope.
    # It must never be mistaken for Win32 FlushFileBuffers acceptance.
    monkeypatch.setattr(es, '_flush_windows_directory', lambda path: es._safe_directory(Path(path), allow_missing=False))


def test_windows_unconfigured_store_denies_without_creating_directory(tmp_path, win_branch):
    target = tmp_path / 'no-authority'
    with pytest.raises(es.ProfileStorageError, match='WINDOWS_STORAGE_POLICY_UNVERIFIED'):
        es.ProfileEvidenceStore(target).commit(_good())
    assert not target.exists()


def test_windows_nonboolean_authority_is_denied(tmp_path, win_branch):
    target = tmp_path / 'non-bool'
    with pytest.raises(es.ProfileStorageError, match='WINDOWS_STORAGE_POLICY_UNVERIFIED'):
        es.ProfileEvidenceStore(target, windows_storage_authorizer=lambda path: 1).commit(_good())
    assert not target.exists()


@pytest.mark.parametrize('denied_check', [2, 3, 4])
def test_windows_policy_is_reverified_after_root_and_reservation_and_before_publish(tmp_path, win_branch, denied_check):
    root = tmp_path / 'store'
    paths = []
    def authorize(path):
        paths.append(Path(path))
        return len(paths) != denied_check
    store = es.ProfileEvidenceStore(root, windows_storage_authorizer=authorize)
    with pytest.raises(es.ProfileStorageError, match='WINDOWS_STORAGE_POLICY_UNVERIFIED'):
        store.commit(_good())
    dest = root / _id(_good())
    assert not (dest / 'COMMIT.json').exists()
    assert len(paths) == denied_check
    assert paths[0] == root
    if denied_check >= 2: assert paths[1] == root
    if denied_check >= 3: assert paths[2] == dest
    if denied_check >= 4: assert paths[3] == dest


def test_windows_postpublication_directory_flush_failure_cannot_rollback(tmp_path, monkeypatch, win_branch):
    root = tmp_path / 'store'
    files = _good()
    dest = root / _id(files)
    original = es._flush_windows_directory
    def fail_once_published(path):
        if Path(path) == dest and (dest / 'COMMIT.json').is_file():
            raise es.ProfileStorageError('WINDOWS_DIRECTORY_FLUSH_UNVERIFIED')
        return original(path)
    monkeypatch.setattr(es, '_flush_windows_directory', fail_once_published)
    store = es.ProfileEvidenceStore(root, windows_storage_authorizer=lambda path: True)  # test-scoped synthetic grant
    with pytest.raises(es.ProfileStorageError, match='STORAGE_FAILED'):
        store.commit(files)
    before = {p.name: p.read_bytes() for p in dest.iterdir()}
    assert 'COMMIT.json' in before and '.COMMIT.staged' not in before
    assert es.verify_profile_bundle(dest)['publication_state'] == 'COMMITTED_VALID'
    assert before == {p.name: p.read_bytes() for p in dest.iterdir()}


def test_windows_flush_unsupported_is_not_silent_success(monkeypatch, tmp_path):
    monkeypatch.setattr(es, 'os', _SimulatedWindowsOS())
    # Model the unsupported Win32 operation explicitly.  The propagation
    # requirement is host-independent; native API success/failure is separately
    # assessed and is never inferred from this synthetic oracle.
    def native_unavailable(path):
        raise es.ProfileStorageError('WINDOWS_DIRECTORY_FLUSH_UNVERIFIED')
    monkeypatch.setattr(es, '_flush_windows_directory', native_unavailable)
    with pytest.raises(es.ProfileStorageError, match='WINDOWS_DIRECTORY_FLUSH_UNVERIFIED'):
        es._sync_directory(tmp_path)


def test_spool_journal_refsync_does_not_truncate_or_rewrite_bytes(tmp_path):
    root = tmp_path / 'spool'
    journal = root / 'journal'
    journal.mkdir(parents=True)
    path = journal / '00000000000000000001.jsonl'
    original = b'{"safe":"exact","fixture":true}\n'
    path.write_bytes(original)
    class Handle:
        def flush(self): pass
    dummy = SimpleNamespace(root=root, _handle=Handle())
    JournalSpool._sync_all_segments(dummy)
    assert path.read_bytes() == original
