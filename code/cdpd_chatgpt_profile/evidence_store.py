"""L06 profile evidence writer and reader; no Web or authority operations.

The trusted coordinator owns this writer. A one-shot Capture Worker returns
temporary results and must not acquire this store to publish its own COMMIT.
Accepted generic cdpd.store is unchanged; its verifier is an additional check.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable

from cdpd.store import verify_bundle

from .bundle_validation import BundleInputError, canonical, verify_report_bundle


BUNDLE_LIMIT = 10 * 1024 * 1024
_PAYLOADS = frozenset({"TRIGGER.json", "EVENTS.json", "RESULT.json", "STRUCTURE.json"})
_ALL_FILES = _PAYLOADS | {"MANIFEST.json", "COMMIT.json"}
_CAPTURE_ID = re.compile(r"C-[0-9a-f]{32}\Z")


class ProfileStorageError(OSError):
    """Fixed code only; never includes rejected content, paths or exceptions."""
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _capture_id(value: Any) -> str:
    if not isinstance(value, str) or _CAPTURE_ID.fullmatch(value) is None:
        raise BundleInputError("PROFILE_INCONSISTENT")
    return value


def encode_profile_bundle(files: dict[str, Any], capture_id: str) -> dict[str, bytes]:
    """Encode and validate all six/five files without changing caller payloads.

    Capture ID must already match RESULT.profile.capture_id. This function does
    not add missing fields, drop unknown fields, repair data, or infer authority.
    The full 10-MiB limit includes the newly encoded Manifest and COMMIT.
    """
    capture_id = _capture_id(capture_id)
    if type(files) is not dict or set(files) - _PAYLOADS:
        raise BundleInputError("BUNDLE_BYTES_INVALID")
    try:
        blob = {name: canonical(files[name]) for name in sorted(files)}
        manifest = {
            "schema": "cdpd.bundle-manifest/v1",
            "capture_id": capture_id,
            "files": [{"path": name, "bytes": len(raw), "sha256": _sha(raw)}
                      for name, raw in blob.items()],
        }
        blob["MANIFEST.json"] = canonical(manifest)
        blob["COMMIT.json"] = canonical({"schema": "cdpd.commit/v1",
                                         "manifest_sha256": _sha(blob["MANIFEST.json"])})
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise BundleInputError("BUNDLE_JSON_INVALID") from None
    verify_report_bundle(blob)
    return blob


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_directory(path: Path, *, allow_missing: bool) -> None:
    """Check every existing ancestor before resolving/creating anything."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise ProfileStorageError("STORAGE_PATH_INVALID") from None
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ProfileStorageError("STORAGE_PATH_INVALID")


def _read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or _is_reparse(before) or before.st_size > limit):
        raise ProfileStorageError("STORAGE_PATH_INVALID")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode) or _is_reparse(opened)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ProfileStorageError("STORAGE_PATH_INVALID")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if (len(raw) > limit or len(raw) != after.st_size
            or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ProfileStorageError("STORAGE_READ_CHANGED")
    return raw


def _read_blob(root: Path, provisional_commit: bytes | None = None) -> dict[str, bytes]:
    _safe_directory(root, allow_missing=False)
    _capture_id(root.name)
    entries = list(root.iterdir())
    names = {entry.name for entry in entries}
    if len(entries) != len(names) or names - _ALL_FILES:
        raise ProfileStorageError("STORAGE_PATH_INVALID")
    if provisional_commit is not None and "COMMIT.json" in names:
        raise ProfileStorageError("COMMIT_ALREADY_EXISTS")
    blob: dict[str, bytes] = {}
    remaining = BUNDLE_LIMIT
    for name in sorted(names):
        maximum = min(remaining, 32768 if name == "TRIGGER.json" else
                      1048576 if name == "EVENTS.json" else BUNDLE_LIMIT)
        raw = _read_regular(root / name, maximum)
        remaining -= len(raw)
        blob[name] = raw
    if provisional_commit is not None:
        blob["COMMIT.json"] = provisional_commit
    return blob


def verify_profile_bundle(path: str | Path) -> dict[str, Any]:
    """Readback always runs the same full frontend plus generic verify_bundle.

    Missing COMMIT is an incomplete, unpublished bundle on every platform. A
    syntactically valid COMMIT by itself does not establish integrity or profile
    consistency. No reader mutates or repairs stored evidence.
    """
    root = Path(path).absolute()
    result: dict[str, Any] = {
        "schema": "cdpd.profile-integrity-result/v1", "integrity_valid": False,
        "profile_valid": False, "publication_state": "INVALID",
        "assessment_status": "NOT_PERFORMED",
    }
    try:
        _safe_directory(root, allow_missing=False)
        if not (root / "COMMIT.json").exists():
            result.update(publication_state="INCOMPLETE", failure_code="COMMIT_MISSING")
            return result
        blob = _read_blob(root)
        manifest_hash, payload = verify_report_bundle(blob)
        manifest_id = payload["RESULT.json"]["profile"]["capture_id"]
        if manifest_id != root.name:
            raise BundleInputError("PROFILE_INCONSISTENT")
        generic = verify_bundle(root)
        if not generic.get("integrity_valid"):
            raise BundleInputError("BUNDLE_HASH_INVALID")
        # This repeats byte equality only across the generic check's separate
        # reads, so a changed on-disk file cannot receive a stale success.
        if _read_blob(root) != blob:
            raise ProfileStorageError("STORAGE_READ_CHANGED")
        result.update(integrity_valid=True, profile_valid=True,
                      publication_state="COMMITTED_VALID", manifest_sha256=manifest_hash,
                      file_count=len(payload), capture_status=payload["RESULT.json"]["capture_status"])
    except BundleInputError as exc:
        result["failure_code"] = exc.code
    except ProfileStorageError as exc:
        result["failure_code"] = exc.code
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        result["failure_code"] = "STORAGE_READ_FAILED"
    return result


def _write_new(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _flush_windows_directory(path: Path) -> None:
    """Try a real Win32 FlushFileBuffers on a directory; fail closed if unsupported.

    Windows does not promise that flushing a directory HANDLE is supported by
    every filesystem/driver. In particular, a successful file os.fsync plus
    MoveFileEx-style rename is NOT evidence for durable directory metadata or
    power-loss ordering. This operation never substitutes a no-op on Windows.
    Failure after a public COMMIT is reporting-only and MUST NOT roll it back.
    """
    import ctypes
    from ctypes import wintypes

    if os.name != "nt":
        raise ProfileStorageError("WINDOWS_DIRECTORY_FLUSH_UNVERIFIED")
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, wintypes.DWORD,
                                        wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.FlushFileBuffers.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # Backup semantics opens directories; OPEN_REPARSE_POINT excludes
        # silently following a redirected directory when acquiring the handle.
        handle = kernel.CreateFileW(str(path), 0x40000000, 0x7, None, 3,
                                    0x02000000 | 0x00200000, None)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid_handle):
            raise ProfileStorageError("WINDOWS_DIRECTORY_FLUSH_UNVERIFIED")
        try:
            if not kernel.FlushFileBuffers(handle):
                raise ProfileStorageError("WINDOWS_DIRECTORY_FLUSH_UNVERIFIED")
        finally:
            kernel.CloseHandle(handle)
    except ProfileStorageError:
        raise
    except (OSError, AttributeError, TypeError, ValueError):
        raise ProfileStorageError("WINDOWS_DIRECTORY_FLUSH_UNVERIFIED") from None


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        _flush_windows_directory(path)
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ProfileEvidenceStore:
    """Exclusive coordinator-owned publication, with COMMIT strictly last.

    Windows requires the embedding trusted coordinator to supply a storage
    authorizer that verifies local NTFS/dedicated root/ACL policy. Mode 0700 is
    never represented as Windows ACL verification. The authorizer is application
    code, not a bool taken from CaptureRequest or persisted JSON. Windows native
    qualification remains NOT_RUN in this Linux synthetic Candidate.
    """
    def __init__(self, root: str | Path, *, max_bytes: int = BUNDLE_LIMIT,
                 windows_storage_authorizer: Callable[[Path], bool] | None = None) -> None:
        if type(max_bytes) is not int or not 4096 <= max_bytes <= BUNDLE_LIMIT:
            raise ValueError("EVIDENCE_BUDGET_INVALID")
        self.root = Path(root).absolute()
        self.max_bytes = max_bytes
        if windows_storage_authorizer is not None and not callable(windows_storage_authorizer):
            raise TypeError("STORAGE_AUTHORIZER_INVALID")
        self._windows_storage_authorizer = windows_storage_authorizer

    def _require_windows_policy(self, path: Path) -> None:
        """Native store admission is explicit, scoped and rechecked after mkdir.

        This callback is only supplied by the trusted coordinator. Test-only
        synthetic grants live exclusively in pytest fixtures, not in this API.
        Even an exception or non-bool truthy result is a denial.
        """
        if os.name != "nt":
            return
        try:
            allowed = (self._windows_storage_authorizer is not None
                       and self._windows_storage_authorizer(path) is True)
        except Exception:
            allowed = False
        if not allowed:
            raise ProfileStorageError("WINDOWS_STORAGE_POLICY_UNVERIFIED")

    def commit(self, files: dict[str, Any], capture_id: str | None = None, *,
               before_publish: Callable[[], None] | None = None, publish_gate: Any = None,
               metadata_request: Any = None) -> Path:
        """Publish only after an optional trusted final guard returns normally.

        The coordinator supplies a live diagnostic permit callback. No bool or
        stored file flag is interpreted as admission: the callback must return
        None or raise. It runs exactly once after staged byte validation and
        immediately before COMMIT. A rejection discards this transaction's
        uncommitted structure; it does not repair the metadata or retry capture.
        """
        if before_publish is not None and not callable(before_publish):
            raise TypeError("PUBLISH_GUARD_INVALID")
        if publish_gate is not None and not callable(getattr(publish_gate, "publish", None)):
            raise TypeError("PUBLISH_GATE_INVALID")
        from .supervisor_authority import _MetadataOnlyPublicationView
        metadata_view = type(publish_gate) is _MetadataOnlyPublicationView
        if metadata_request is not None and not metadata_view:
            raise TypeError("METADATA_PUBLICATION_GATE_REQUIRED")
        try:
            selected_id = files["RESULT.json"]["profile"]["capture_id"] if capture_id is None else capture_id
        except (KeyError, TypeError):
            raise BundleInputError("PROFILE_INCONSISTENT") from None
        blob = encode_profile_bundle(files, selected_id)
        if sum(map(len, blob.values())) > self.max_bytes:
            raise ProfileStorageError("EVIDENCE_BUDGET_EXCEEDED")
        _safe_directory(self.root, allow_missing=True)
        self._require_windows_policy(self.root)
        destination = self.root / selected_id
        reserved = False
        # Published is a one-way phase transition at the successful atomic
        # rename, NOT at the later directory fsync or integrity readback.
        # A post-publication error may be reported but must never unpublish or
        # rewrite evidence that another observer could already have seen.
        commit_published = False
        staged_commit = destination / ".COMMIT.staged"
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _safe_directory(self.root, allow_missing=False)
            self._require_windows_policy(self.root)  # validate created root ACL, not mode 0700
            destination.mkdir(mode=0o700)  # Same capture ID is never overwritten/retried.
            reserved = True
            _safe_directory(destination, allow_missing=False)
            self._require_windows_policy(destination)  # validate inherited ACL after creation
            for name in sorted(set(blob) - {"MANIFEST.json", "COMMIT.json"}):
                _write_new(destination / name, blob[name])
            _write_new(destination / "MANIFEST.json", blob["MANIFEST.json"])
            # Read actual persisted bytes before publication. Add only the exact
            # intended COMMIT in memory so the same validator can check them.
            actual = _read_blob(destination, provisional_commit=blob["COMMIT.json"])
            if actual != blob:
                raise ProfileStorageError("STORAGE_READ_CHANGED")
            verify_report_bundle(actual)
            if before_publish is not None:
                try:
                    if before_publish() is not None:
                        raise ProfileStorageError("PUBLISH_GUARD_DENIED")
                except Exception:
                    # These paths belong to the directory exclusively reserved
                    # by this transaction. Keep S0 outside this store untouched.
                    # The remaining Manifest may reference the deleted file;
                    # that is an incomplete staging record, never a repaired
                    # or successful metadata-only bundle.
                    try:
                        (destination / "STRUCTURE.json").unlink(missing_ok=True)
                        (destination / "COMMIT.json").unlink(missing_ok=True)
                        _sync_directory(destination)
                    except OSError:
                        raise ProfileStorageError("PUBLISH_GUARD_CLEANUP_FAILED") from None
                    raise ProfileStorageError("PUBLISH_GUARD_DENIED") from None
            # Every path, including the no-gate L06 path, stages and fsyncs
            # COMMIT *before* publication.  A failed staging write is still
            # uncommitted and can be rolled back without touching COMMIT.json.
            _write_new(staged_commit, blob["COMMIT.json"])
            # The gate's atomic critical section must contain no blocking I/O
            # beyond rename. Revalidate outside it and refuse publication if
            # an existing directory has become a reparse point/untrusted ACL.
            _safe_directory(destination, allow_missing=False)
            self._require_windows_policy(destination)

            def publish_atomically() -> None:
                nonlocal commit_published
                # PublicationGate serializes only this atomic filesystem rename
                # (plus an in-memory phase bit), never post-rename I/O.
                os.replace(staged_commit, destination / "COMMIT.json")
                commit_published = True

            if metadata_view:
                # The exact original request and actual persisted, fully
                # validated bytes must match the separately issued metadata
                # capability. No caller bool or similarly named wrapper API
                # can opt a normal content transaction into this branch.
                accepted = publish_gate.publish_verified(publish_atomically, actual,
                                                         metadata_request, destination)
            else:
                accepted = (publish_gate.publish(publish_atomically)
                            if publish_gate is not None else publish_atomically())
            if publish_gate is not None and not accepted:
                # The normal gate rejects *before* invoking the action. If a
                # defective wrapper nevertheless published, fail safely: do not
                # delete its already visible marker or its staged evidence.
                if commit_published or (destination / "COMMIT.json").exists():
                    raise ProfileStorageError("STORAGE_FAILED")
                try:
                    staged_commit.unlink(missing_ok=True)
                    (destination / "STRUCTURE.json").unlink(missing_ok=True)
                    _sync_directory(destination)
                except OSError:
                    raise ProfileStorageError("PUBLISH_GUARD_CLEANUP_FAILED") from None
                raise ProfileStorageError("PUBLISH_GUARD_DENIED")

            # Post-publication phase. Any failure here is *reporting only*:
            # caller gets STORAGE_FAILED, but no COMMIT, MANIFEST or payload
            # bytes are deleted, rewritten or repaired. Even accepted local
            # INVALIDATE or keeper observation loss cannot roll it back.
            _sync_directory(destination)
            _sync_directory(self.root)
            observed = verify_profile_bundle(destination)
            if not observed["integrity_valid"] or not observed["profile_valid"]:
                raise ProfileStorageError("BUNDLE_READBACK_FAILED")
            return destination
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            # Pre-publication staging may be discarded; once os.replace has
            # succeeded, committed evidence is immutable even when a later
            # fsync, readback, or gate wrapper raises. If an exceptional replace
            # reported an ambiguous outcome, presence of COMMIT is treated as
            # possibly published: never unlink an already visible COMMIT.
            if reserved and not commit_published:
                try:
                    possibly_published = (destination / "COMMIT.json").exists()
                except OSError:
                    possibly_published = True  # conservative, never destroy
                if not possibly_published:
                    try:
                        staged_commit.unlink(missing_ok=True)
                        _sync_directory(destination)
                    except OSError:
                        raise ProfileStorageError("FAILED_COMMIT_CLEANUP") from None
            if (isinstance(exc, ProfileStorageError)
                    and exc.code in {"PUBLISH_GUARD_DENIED", "PUBLISH_GUARD_CLEANUP_FAILED",
                                     "WINDOWS_STORAGE_POLICY_UNVERIFIED"}):
                raise ProfileStorageError(exc.code) from None
            raise ProfileStorageError("STORAGE_FAILED") from None
