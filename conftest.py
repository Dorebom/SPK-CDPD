"""Local/synthetic-only L05 execution and byte-pinned L04 regression fixtures."""
from __future__ import annotations

from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "tests"))
for source in (ROOT / "packages").glob("*/src"):
    sys.path.insert(0, str(source))


def pytest_collection_modifyitems(items):
    for item in items:
        if item.name.startswith("test_real_playwright_"):
            item.add_marker(pytest.mark.skip(reason="L04_SCOPE_FORBIDS_BROWSER_PROCESS_AND_REAL_CDP"))


@pytest.fixture(autouse=True)
def prohibit_real_network(monkeypatch):
    """All L04 execution tests are in-memory fixtures, including error paths."""
    def denied(*args, **kwargs):
        raise AssertionError("L04_SYNTHETIC_ONLY_NETWORK_PROHIBITED")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    try:
        from playwright.sync_api import BrowserType
    except ImportError:
        pass
    else:
        for name in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
            monkeypatch.setattr(BrowserType, name, denied)


@pytest.fixture
def evidence_case(request):
    """Record safe fixture outcomes; never pass content, credentials or raw URLs."""
    def record(*, expected, actual, commands=(), ac=(), evt=(), tgt=(), bind=()):
        request.node._l04_evidence = {
            "expected": expected, "actual": actual, "commands": list(commands),
            "ac": list(ac), "evt": list(evt), "tgt": list(tgt), "bind": list(bind),
        }
    return record


# FIX004: these hooks are TEST-ONLY, never imported by the product runtime.
# They preserve all baseline node IDs and never mint a native Windows product
# acceptance claim. Production WindowsStoragePolicy remains default DENY.
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Bound only PYTEST_CURRENT_TEST on Windows; retain full pytest nodeid.

    The Windows environment block rejects per-variable values >32767 UTF-16
    units. Pytest 8.x writes the entire oversized param ID at each phase. We
    replace only the *diagnostic env var* for that one case, not the test ID,
    test body, test selection or result recorded in the evidence ledger.
    """
    import os
    if os.name != "nt" or len(item.nodeid.encode("utf-16-le")) // 2 < 32000:
        yield
        return
    import hashlib
    import _pytest.runner as pytest_runner
    old = pytest_runner._update_current_test_var
    def bounded_env(node, when):
        if node is not item or when is None:
            return old(node, when)
        digest = hashlib.sha256(node.nodeid.encode("utf-8")).hexdigest()
        os.environ["PYTEST_CURRENT_TEST"] = "OVERSIZE_NODEID_SHA256_" + digest + " (" + when + ")"
    pytest_runner._update_current_test_var = bounded_env
    try:
        yield
    finally:
        pytest_runner._update_current_test_var = old


@pytest.fixture(autouse=True)
def windows_synthetic_storage_scope(request, monkeypatch):
    """Controlled synthetic-only test capability for the isolated pytest tmp_path.

    A synthetic unit suite is not a local NTFS DACL / crash-consistency
    certification. Inject a capability ONLY while the test is executing and
    ONLY for paths within that test's private pytest tmp_path. No environment
    variable, payload field, application API or installed package can activate
    this fixture. Production WindowsStoragePolicy is NOT replaced.
    """
    import os
    if os.name != "nt" or request.node.get_closest_marker("no_windows_synthetic_capability"):
        return
    from pathlib import Path
    import stat
    from cdpd_chatgpt_profile import evidence_store as es
    from cdpd_chatgpt_profile import supervisor as sv

    sandbox = request.getfixturevalue("tmp_path").absolute()
    old_store_init = es.ProfileEvidenceStore.__init__
    old_supervisor_init = sv.Supervisor.__init__
    old_flush = es._flush_windows_directory

    def within(path):
        try:
            candidate = Path(path).absolute()
            candidate.relative_to(sandbox)
            # Avoid symlink/reparse redirection of sandbox grants, including
            # links introduced after the constructor was called.
            for item in (candidate, *candidate.parents):
                if item == sandbox.parent:
                    break
                if item.exists() or item.is_symlink():
                    info = item.lstat()
                    if stat.S_ISLNK(info.st_mode) or (
                            getattr(info, "st_file_attributes", 0) & 0x400):
                        return False
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    def grant(path):
        return within(path) is True

    def store_init(self, root, *, max_bytes=es.BUNDLE_LIMIT, windows_storage_authorizer=None):
        if windows_storage_authorizer is None and within(root):
            windows_storage_authorizer = grant
        return old_store_init(self, root, max_bytes=max_bytes,
                              windows_storage_authorizer=windows_storage_authorizer)

    def supervisor_init(self, config, **kwargs):
        if kwargs.get("storage_authorizer") is None and within(config.root):
            kwargs["storage_authorizer"] = grant
        return old_supervisor_init(self, config, **kwargs)

    def synthetic_dir_flush(path):
        if not within(path):
            return old_flush(path)
        # Test-only deterministic namespace model. NO OS FLUSH IS CLAIMED.
        # A separate native assessment must exercise the real Win32 API.
        es._safe_directory(Path(path), allow_missing=False)

    monkeypatch.setattr(es.ProfileEvidenceStore, "__init__", store_init)
    monkeypatch.setattr(sv.Supervisor, "__init__", supervisor_init)
    monkeypatch.setattr(es, "_flush_windows_directory", synthetic_dir_flush)
