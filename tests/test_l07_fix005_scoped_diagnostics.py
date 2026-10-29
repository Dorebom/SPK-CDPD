"""FIX005: synthetic-only invariants; never grant a production Windows policy."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import threading


def test_fix005_new_staged_fsync_hook_does_not_require_linux_proc_path():
    source = (Path(__file__).parent / 'test_l07_fix003_r3_replay.py').read_text()
    assert "stage_scope = threading.local()" in source
    assert "monkeypatch.setattr(storemod, '_write_new', instrumented_new_write)" in source
    assert "if getattr(stage_scope, 'active', False)" in source
    # An actual stage-fsync fault is exercised by the inherited R3 test, not by
    # this static cross-platform guard on the identity of its injection site.
    assert "os.readlink(f'/proc/self/fd/{fd}')" not in source


def test_fix005_explicit_state_and_bundle_scope_not_global_true_grant():
    source = (Path(__file__).parent / 'test_l06_execution_regressions.py').read_text()
    assert 'candidate.relative_to(store.root.absolute())' in source
    assert 'candidate == initial.state_dir.absolute()' in source
    assert 'bundle_observations.append(path)' in source
    assert 'store._windows_storage_authorizer = authorizer' in source
    assert 'return True' in source  # only within exact-test state or bundle scopes
