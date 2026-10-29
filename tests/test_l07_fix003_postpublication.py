"""FIX003: published marker/evidence are immutable across late storage failures.

These are deterministic synthetic fault-injection tests; they do not assert
native NTFS, real browser, durable-power-loss, or formal L07 acceptance.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
import cdpd_chatgpt_profile.evidence_store as evidence_store
from cdpd_chatgpt_profile.capture_service import CaptureServiceError
from cdpd_chatgpt_profile.evidence_store import ProfileEvidenceStore, ProfileStorageError
from cdpd_chatgpt_profile.owned_process import RegisteredProcess, process_creation_identity
from test_l06_bundle_contract import _good, _id
from test_l06_capture_service import snapshot
from test_l07_fix001_supervisor import INVALIDATE, begin, durable_receive, event, subject

_TIMEOUT = 5


def _runner(coordinator, job, box):
    try:
        box['outcome'] = coordinator.capture(job)
    except Exception as exc:
        box['error'] = exc


@pytest.mark.parametrize('failure', ['bundle_fsync', 'root_fsync', 'invalid_readback', 'readback_raises'])
@pytest.mark.parametrize('revoke', ['INVALIDATE', 'KEEPER_OBSERVATION_UNAVAILABLE'])
def test_published_bundle_immutable_after_failure_and_late_revoke(tmp_path, monkeypatch, failure, revoke):
    process = (RegisteredProcess.open(os.getpid(), process_creation_identity(os.getpid()))
               if revoke == 'KEEPER_OBSERVATION_UNAVAILABLE' else None)
    try:
        sup, _, _, _ = subject(tmp_path, process=process)
        initial, operation = begin(sup)
        job = sup._job(initial)
        coordinator = sup._coordinator(sup._scoped_permit(operation.context))
        bundle = sup.root / 'bundles' / job.to_dict()['capture_id']
        reached, release = threading.Event(), threading.Event()
        orig_sync = evidence_store._sync_directory
        orig_read = evidence_store.verify_profile_bundle

        def sync_fault(path):
            p = Path(path)
            target = bundle if failure == 'bundle_fsync' else bundle.parent
            if (failure in ('bundle_fsync', 'root_fsync') and p == target
                    and (bundle / 'COMMIT.json').exists() and not reached.is_set()):
                reached.set()
                assert release.wait(_TIMEOUT)
                raise OSError('SYNTHETIC_POST_PUBLICATION_DIRECTORY_FSYNC')
            return orig_sync(path)

        def read_fault(path):
            if (failure in ('invalid_readback', 'readback_raises')
                    and Path(path) == bundle and not reached.is_set()):
                assert (bundle / 'COMMIT.json').is_file()
                reached.set()
                assert release.wait(_TIMEOUT)
                if failure == 'invalid_readback':
                    return {'integrity_valid': False, 'profile_valid': False}
                raise OSError('SYNTHETIC_POST_PUBLICATION_READBACK_ERROR')
            return orig_read(path)

        monkeypatch.setattr(evidence_store, '_sync_directory', sync_fault)
        monkeypatch.setattr(evidence_store, 'verify_profile_bundle', read_fault)
        box = {}
        thread = threading.Thread(target=_runner, args=(coordinator, job, box), daemon=True)
        thread.start()
        assert reached.wait(_TIMEOUT), 'postpublication injection must be reached'
        assert operation.context.publication_gate.published is True
        assert (bundle / 'COMMIT.json').is_file()
        assert not (bundle / '.COMMIT.staged').exists()
        before = snapshot(bundle)
        if process:
            process.close()
            sup.tick()
        else:
            durable_receive(sup, event(row=INVALIDATE[0], seq=2))
            sup.tick()
        assert operation.context.publication_gate.invalidated is True
        assert before == snapshot(bundle)
        release.set()
        thread.join(_TIMEOUT)
        assert not thread.is_alive()
        assert isinstance(box.get('error'), CaptureServiceError)
        assert box['error'].code == 'STORAGE_FAILED'
        assert before == snapshot(bundle), 'reported failure must not rollback published COMMIT'
    finally:
        if process:
            process.close()


@pytest.mark.parametrize('failure', ['root_fsync', 'invalid_readback', 'readback_exception'])
def test_no_gate_postpublication_failure_does_not_unpublish(tmp_path, monkeypatch, failure):
    files = _good()
    root = tmp_path / 'bundles'
    dest = root / _id(files)
    orig_sync = evidence_store._sync_directory
    orig_read = evidence_store.verify_profile_bundle
    before = []

    def fail_sync(path):
        if failure == 'root_fsync' and Path(path) == root and (dest / 'COMMIT.json').is_file():
            before.append(snapshot(dest))
            raise OSError('SYNTHETIC_ROOT_FSYNC_FAILURE')
        return orig_sync(path)

    def fail_readback(path):
        if Path(path) == dest:
            before.append(snapshot(dest))
            if failure == 'invalid_readback':
                return {'integrity_valid': False, 'profile_valid': False}
            if failure == 'readback_exception':
                raise OSError('SYNTHETIC_READBACK_FAILURE')
        return orig_read(path)

    monkeypatch.setattr(evidence_store, '_sync_directory', fail_sync)
    monkeypatch.setattr(evidence_store, 'verify_profile_bundle', fail_readback)
    with pytest.raises(ProfileStorageError) as err:
        ProfileEvidenceStore(root).commit(files)
    assert err.value.code == 'STORAGE_FAILED'
    assert len(before) == 1
    assert before[0] == snapshot(dest)
    assert (dest / 'COMMIT.json').is_file()
    assert not (dest / '.COMMIT.staged').exists()
    assert orig_read(dest)['publication_state'] == 'COMMITTED_VALID'


def test_atomic_replace_exception_after_visible_commit_is_non_destructive(tmp_path, monkeypatch):
    """An ambiguous exception after replace must prefer preserve over unlink."""
    files = _good()
    original = evidence_store.os.replace

    def ambiguous_replace(source, dest):
        original(source, dest)
        raise OSError('SYNTHETIC_AMBIGUOUS_POST_RENAME_EXCEPTION')

    monkeypatch.setattr(evidence_store.os, 'replace', ambiguous_replace)
    with pytest.raises(ProfileStorageError, match='STORAGE_FAILED'):
        ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    dest = tmp_path / 'bundles' / _id(files)
    assert (dest / 'COMMIT.json').is_file()
    assert not (dest / '.COMMIT.staged').exists()


def test_prepublication_staging_failure_rolls_back_only_unpublished_marker(tmp_path, monkeypatch):
    files = _good()
    original = evidence_store._write_new

    def stage_fault(path, raw):
        original(path, raw)
        if Path(path).name == '.COMMIT.staged':
            raise OSError('SYNTHETIC_PRE_PUBLICATION_STAGE_ERROR')

    monkeypatch.setattr(evidence_store, '_write_new', stage_fault)
    with pytest.raises(ProfileStorageError, match='STORAGE_FAILED'):
        ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    dest = tmp_path / 'bundles' / _id(files)
    assert not (dest / 'COMMIT.json').exists()
    assert not (dest / '.COMMIT.staged').exists()
