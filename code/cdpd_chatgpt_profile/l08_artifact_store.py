"""Separate immutable L08 artifacts; accepted capture bundles are read-only.

This writer reuses the accepted filesystem checks and flush primitives. A
successful COMMIT rename is irreversible: a subsequent failure reports an
uncertain durability/readback result without removing or changing any bytes.
Windows admission requires trusted application code, never a saved JSON flag.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Callable

from .bundle_validation import canonical, strict_json
from .evidence_store import (
    _is_reparse, _read_regular, _safe_directory, _sync_directory, _write_new,
)

REPORT_LIMIT = 1048576
BASELINE_LIMIT = 10485760 + 1048576
BASELINE_ROOT_LIMIT = 64 * 1024 * 1024
REPORT_ROOT_LIMIT = 1024 * 1024 * 1024
_KINDS = {"REPORT": "R", "BASELINE_CANDIDATE": "B", "MONITOR_STATUS": "M"}
_ERRORS = frozenset({
    "INPUT_INVALID", "ARTIFACT_INVALID", "ARTIFACT_ALREADY_EXISTS",
    "ARTIFACT_INCOMPLETE", "STORAGE_PATH_INVALID", "STORAGE_READ_FAILED",
    "STORAGE_FAILED", "POST_PUBLICATION_UNCONFIRMED", "STORE_BUSY",
    "STORAGE_CAPACITY_EXCEEDED", "WINDOWS_STORAGE_POLICY_UNVERIFIED",
})


class ArtifactError(OSError):
    """Only a fixed code crosses the public diagnostic boundary."""
    def __init__(self, code: str):
        self.code = code if code in _ERRORS else "STORAGE_FAILED"
        super().__init__(self.code)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _windows_required() -> bool:
    return os.name == "nt"


def absolute_path(value: str | Path) -> Path:
    try:
        raw = os.fspath(value)
        if (type(raw) is not str or not raw or any(ord(c) < 32 for c in raw)
                or ".." in Path(raw).parts or not Path(raw).is_absolute()):
            raise ValueError()
        return Path(raw)
    except (TypeError, ValueError, OSError):
        raise ArtifactError("INPUT_INVALID") from None


def _member(name: str) -> str:
    if (type(name) is not str or not re.fullmatch(r"[A-Za-z0-9_./-]{1,160}", name)
            or PurePosixPath(name).is_absolute() or "\\" in name
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ArtifactError("ARTIFACT_INVALID")
    return name


def _encoded(payloads: dict[str, bytes], kind: str) -> dict[str, bytes]:
    if kind not in _KINDS or type(payloads) is not dict or not payloads:
        raise ArtifactError("ARTIFACT_INVALID")
    if len(payloads) > 12 or set(payloads) & {"MANIFEST.json", "COMMIT.json"}:
        raise ArtifactError("ARTIFACT_INVALID")
    if any(type(v) is not bytes for v in payloads.values()):
        raise ArtifactError("ARTIFACT_INVALID")
    names = [_member(n) for n in payloads]
    if len({n.casefold() for n in names}) != len(names):
        raise ArtifactError("ARTIFACT_INVALID")
    manifest = {"schema": "cdpd.l08-artifact-manifest/v1", "kind": kind,
                "files": [{"path": n, "bytes": len(payloads[n]),
                           "sha256": sha256(payloads[n])} for n in sorted(names)]}
    result = dict(payloads)
    result["MANIFEST.json"] = canonical(manifest)
    result["COMMIT.json"] = canonical({"schema": "cdpd.l08-artifact-commit/v1",
        "manifest_sha256": sha256(result["MANIFEST.json"])})
    limit = BASELINE_LIMIT if kind == "BASELINE_CANDIDATE" else REPORT_LIMIT
    if sum(map(len, result.values())) > limit:
        raise ArtifactError("STORAGE_CAPACITY_EXCEEDED")
    return result


def read_artifact(path: str | Path, *, provisional_commit: bytes | None = None) -> dict[str, bytes]:
    """Strict artifact byte read, with no source-bundle repair or extraction."""
    root = absolute_path(path)
    try:
        _safe_directory(root, allow_missing=False)
        found = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            _safe_directory(parent, allow_missing=False)
            for name in dirs:
                _safe_directory(parent / name, allow_missing=False)
            for name in files:
                relative = (parent / name).relative_to(root).as_posix()
                _member(relative)
                if relative in found or len(found) >= 14:
                    raise ArtifactError("ARTIFACT_INVALID")
                found[relative] = _read_regular(parent / name, BASELINE_LIMIT)
                if sum(map(len, found.values())) > BASELINE_LIMIT:
                    raise ArtifactError("STORAGE_CAPACITY_EXCEEDED")
        if provisional_commit is not None:
            if "COMMIT.json" in found:
                raise ArtifactError("ARTIFACT_ALREADY_EXISTS")
            found["COMMIT.json"] = provisional_commit
        if not {"MANIFEST.json", "COMMIT.json"} <= set(found):
            raise ArtifactError("ARTIFACT_INCOMPLETE")
        manifest = strict_json(found["MANIFEST.json"], 65536)
        commit = strict_json(found["COMMIT.json"], 4096)
        if (type(manifest) is not dict or set(manifest) != {"schema", "kind", "files"}
                or manifest["schema"] != "cdpd.l08-artifact-manifest/v1"
                or manifest["kind"] not in _KINDS or type(manifest["files"]) is not list
                or type(commit) is not dict or set(commit) != {"schema", "manifest_sha256"}
                or commit["schema"] != "cdpd.l08-artifact-commit/v1"
                or commit["manifest_sha256"] != sha256(found["MANIFEST.json"])):
            raise ArtifactError("ARTIFACT_INVALID")
        payloads = {n: raw for n, raw in found.items() if n not in {"MANIFEST.json", "COMMIT.json"}}
        if _encoded(payloads, manifest["kind"]) != found:
            raise ArtifactError("ARTIFACT_INVALID")
        return found
    except ArtifactError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ArtifactError("STORAGE_READ_FAILED") from None


class ArtifactStore:
    """Create-only, capacity-bounded store; never prunes adopted/historical data.

    Report roots share the approved 1 GiB / 250 artifact bounds. Baseline roots
    use the separate 64 MiB budget. Seven-day retention is an operator duty;
    reaching capacity rejects new admission rather than deleting any baseline.
    """
    def __init__(self, root: str | Path, *, kind: str,
                 windows_storage_authorizer: Callable[[Path], bool] | None = None,
                 fault_hook: Callable[[str], None] | None = None):
        self.root = absolute_path(root)
        if kind not in _KINDS or (windows_storage_authorizer is not None
                                 and not callable(windows_storage_authorizer)):
            raise ArtifactError("INPUT_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise ArtifactError("INPUT_INVALID")
        self.kind, self._authorizer, self._fault = kind, windows_storage_authorizer, fault_hook

    def _policy(self, path: Path) -> None:
        if not _windows_required():
            return
        try:
            allowed = self._authorizer is not None and self._authorizer(path) is True
        except Exception:
            allowed = False
        if not allowed:
            raise ArtifactError("WINDOWS_STORAGE_POLICY_UNVERIFIED")

    def _point(self, name: str) -> None:
        if self._fault is not None:
            self._fault(name)

    def _capacity(self, added: int) -> None:
        maximum = BASELINE_ROOT_LIMIT if self.kind == "BASELINE_CANDIDATE" else REPORT_ROOT_LIMIT
        count = 0
        used = 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            current = Path(directory)
            _safe_directory(current, allow_missing=False)
            for name in dirs:
                _safe_directory(current / name, allow_missing=False)
                if current == self.root and name != ".l08-publish.lock":
                    count += 1
            for name in files:
                info = (current / name).lstat()
                if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                    raise ArtifactError("STORAGE_PATH_INVALID")
                used += info.st_size
                if used + added > maximum:
                    raise ArtifactError("STORAGE_CAPACITY_EXCEEDED")
        if used + added > maximum or (self.kind != "BASELINE_CANDIDATE" and count >= 250):
            raise ArtifactError("STORAGE_CAPACITY_EXCEEDED")

    def write(self, payloads: dict[str, bytes]) -> Path:
        blob = _encoded(payloads, self.kind)
        identity = _KINDS[self.kind] + "-" + sha256(blob["MANIFEST.json"])
        destination = self.root / identity
        lock = self.root / ".l08-publish.lock"
        locked = False
        published = False
        reserved = False
        try:
            _safe_directory(self.root, allow_missing=True)
            self._policy(self.root)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _safe_directory(self.root, allow_missing=False)
            self._policy(self.root)
            try:
                lock.mkdir(mode=0o700)
                locked = True
            except FileExistsError:
                raise ArtifactError("STORE_BUSY") from None
            if destination.exists():
                # A deterministic offline replay may reuse an identical already
                # committed artifact. A damaged/partial artifact is never fixed.
                if read_artifact(destination) != blob:
                    raise ArtifactError("ARTIFACT_ALREADY_EXISTS")
                return destination
            self._capacity(sum(map(len, blob.values())))
            destination.mkdir(mode=0o700)
            reserved = True
            _safe_directory(destination, allow_missing=False)
            self._policy(destination)
            for name in sorted(set(blob) - {"MANIFEST.json", "COMMIT.json"}):
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _safe_directory(path.parent, allow_missing=False)
                self._policy(path.parent)
                _write_new(path, blob[name])
            _write_new(destination / "MANIFEST.json", blob["MANIFEST.json"])
            if read_artifact(destination, provisional_commit=blob["COMMIT.json"]) != blob:
                raise ArtifactError("ARTIFACT_INVALID")
            self._point("BEFORE_COMMIT_STAGING")
            staged = destination / ".COMMIT.staged"
            _write_new(staged, blob["COMMIT.json"])
            _safe_directory(destination, allow_missing=False)
            self._policy(destination)
            self._point("BEFORE_COMMIT_PUBLICATION")
            if (destination / "COMMIT.json").exists():
                raise ArtifactError("ARTIFACT_ALREADY_EXISTS")
            os.replace(staged, destination / "COMMIT.json")
            published = True
            self._point("AFTER_COMMIT_PUBLICATION")
            for directory in sorted((p for p in destination.rglob("*") if p.is_dir()),
                                    key=lambda p: len(p.parts), reverse=True):
                _safe_directory(directory, allow_missing=False)
                _sync_directory(directory)
            _sync_directory(destination)
            _sync_directory(self.root)
            self._point("BEFORE_READBACK")
            if read_artifact(destination) != blob:
                raise ArtifactError("ARTIFACT_INVALID")
            return destination
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            maybe_published = published
            if reserved and not published:
                try:
                    maybe_published = (destination / "COMMIT.json").exists()
                except OSError:
                    maybe_published = True
            if maybe_published:
                raise ArtifactError("POST_PUBLICATION_UNCONFIRMED") from None
            # No rollback touches payloads, MANIFEST or an observable COMMIT.
            # Unpublished partial artifacts remain incomplete and un-retryable.
            if reserved:
                try:
                    (destination / ".COMMIT.staged").unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, ArtifactError):
                raise
            raise ArtifactError("STORAGE_FAILED") from None
        finally:
            if locked:
                try:
                    lock.rmdir()
                except OSError:
                    pass  # Stale lock is fail-closed, never auto-recovered.
