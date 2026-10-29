"""Read-only Windows storage admission for the single-profile Supervisor.

No ACL, owner, directory, registry, profile or cloud provider is modified.
Native APIs are required in production.  Synthetic probes are explicit test
inputs and never establish Windows ACL/NTFS/cloud-provider acceptance.

The same-user trust boundary excludes hostile code running as that user or an
administrator.  Name-based ACL snapshots are not an atomic filesystem sandbox;
unknown APIs/results fail closed.  Unregistered third-party file copying cannot
be disproved by these path APIs and remains an operator constraint.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import os
from pathlib import PureWindowsPath
import re
from typing import Any


_UINT = ctypes.c_uint32
_WORD = ctypes.c_uint16
_PTR = ctypes.c_void_p
_BOOL = ctypes.c_int32
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_CLOUD_OR_OFFLINE_ATTRIBUTES = 0x1000 | 0x40000 | 0x80000 | 0x100000 | 0x400000
_FULL_CONTROL = 0x001F01FF
_GENERIC_ALL = 0x10000000
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"


class StoragePolicyError(ValueError):
    def __init__(self, code: str = "STORAGE_POLICY_DENIED") -> None:
        allowed = {"STORAGE_POLICY_DENIED", "WINDOWS_REQUIRED", "INPUT_INVALID",
                   "PATH_OUTSIDE_ROOT", "REPARSE_OR_CLOUD_PATH", "PATH_MISSING",
                   "LOCAL_NTFS_REQUIRED", "PRIVATE_ACL_REQUIRED", "OWNER_MISMATCH",
                   "CLOUD_SCOPE_UNKNOWN", "NATIVE_API_UNAVAILABLE"}
        self.code = code if code in allowed else "STORAGE_POLICY_DENIED"
        super().__init__(self.code)


@dataclass(frozen=True)
class AccessEntry:
    ace_type: int
    flags: int
    mask: int
    sid: str


@dataclass(frozen=True)
class SecurityFacts:
    owner_sid: str
    dacl_present: bool
    dacl_protected: bool
    entries: tuple[AccessEntry, ...]


def _windows_path(value: Any) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        raise StoragePolicyError("INPUT_INVALID") from None
    if (type(raw) is not str or not raw or len(raw) > 240
            or re.search(r"[\x00-\x1f\x7f]", raw)
            or raw.startswith(("\\\\", "//"))):
        raise StoragePolicyError("INPUT_INVALID")
    path = PureWindowsPath(raw)
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise StoragePolicyError("INPUT_INVALID")
    raw_parts = re.split(r"[\\/]", raw[3:])
    if any(part in (".", "..") for part in raw_parts):
        raise StoragePolicyError("INPUT_INVALID")
    for part in path.parts[1:]:
        if (part.endswith((".", " ")) or re.search(r'[:*?"<>|]', part)
                or PureWindowsPath(part).is_reserved()):
            raise StoragePolicyError("INPUT_INVALID")
    return str(path)


def _inside(path: str, root: str) -> bool:
    try:
        return ntpath.normcase(ntpath.commonpath((path, root))) == ntpath.normcase(root)
    except ValueError:
        return False


def _chain(path: str) -> list[str]:
    value = PureWindowsPath(path)
    return [str(parent) for parent in reversed(value.parents)] + [str(value)]


class _ACL(ctypes.Structure):
    _fields_ = [("revision", ctypes.c_ubyte), ("sbz1", ctypes.c_ubyte),
                ("size", _WORD), ("count", _WORD), ("sbz2", _WORD)]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", _WORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("sid", _PTR), ("attributes", _UINT)]


class _NativeWindowsProbe:
    """ctypes declarations use fixed-width Windows scalars and owned buffers."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise StoragePolicyError("WINDOWS_REQUIRED")
        try:
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            self.cloud = ctypes.WinDLL("cldapi", use_last_error=True)
            self._declare()
            self.user_sid = self._current_sid()
        except StoragePolicyError:
            raise
        except (AttributeError, OSError, ValueError):
            raise StoragePolicyError("NATIVE_API_UNAVAILABLE") from None

    def _declare(self) -> None:
        def bind(dll, name, args, result):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

        pointer = ctypes.POINTER
        wide = ctypes.c_wchar_p
        bind(self.kernel, "GetCurrentProcess", [], _PTR)
        bind(self.kernel, "CloseHandle", [_PTR], _BOOL)
        bind(self.kernel, "LocalFree", [_PTR], _PTR)
        bind(self.kernel, "GetFileAttributesW", [wide], _UINT)
        bind(self.kernel, "GetVolumePathNameW", [wide, wide, _UINT], _BOOL)
        bind(self.kernel, "GetDriveTypeW", [wide], _UINT)
        bind(self.kernel, "GetVolumeInformationW",
             [wide, wide, _UINT, pointer(_UINT), pointer(_UINT), pointer(_UINT), wide, _UINT], _BOOL)
        bind(self.advapi, "OpenProcessToken", [_PTR, _UINT, pointer(_PTR)], _BOOL)
        bind(self.advapi, "GetTokenInformation", [_PTR, _UINT, _PTR, _UINT, pointer(_UINT)], _BOOL)
        bind(self.advapi, "ConvertSidToStringSidW", [_PTR, pointer(_PTR)], _BOOL)
        bind(self.advapi, "IsValidSid", [_PTR], _BOOL)
        bind(self.advapi, "GetLengthSid", [_PTR], _UINT)
        bind(self.advapi, "GetNamedSecurityInfoW",
             [wide, _UINT, _UINT, pointer(_PTR), pointer(_PTR), pointer(_PTR), pointer(_PTR), pointer(_PTR)], _UINT)
        bind(self.advapi, "GetSecurityDescriptorControl", [_PTR, pointer(_WORD), pointer(_UINT)], _BOOL)
        bind(self.advapi, "GetAce", [_PTR, _UINT, pointer(_PTR)], _BOOL)
        bind(self.cloud, "CfGetSyncRootInfoByPath", [wide, _UINT, _PTR, _UINT, pointer(_UINT)], ctypes.c_int32)

    def _sid_text(self, sid: int | _PTR) -> str:
        if not sid or not self.advapi.IsValidSid(sid):
            raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
        value = _PTR()
        if not self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
        try:
            return ctypes.wstring_at(value.value)
        finally:
            self.kernel.LocalFree(value)

    def _current_sid(self) -> str:
        token = _PTR()
        if not self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise StoragePolicyError("OWNER_MISMATCH")
        try:
            length = _UINT()
            self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            if not ctypes.sizeof(_TOKEN_USER) <= length.value <= 65536:
                raise StoragePolicyError("OWNER_MISMATCH")
            data = ctypes.create_string_buffer(length.value)
            if not self.advapi.GetTokenInformation(token, 1, data, length, ctypes.byref(length)):
                raise StoragePolicyError("OWNER_MISMATCH")
            info = ctypes.cast(data, ctypes.POINTER(_TOKEN_USER)).contents
            return self._sid_text(info.sid)
        finally:
            self.kernel.CloseHandle(token)

    def attributes(self, path: str) -> int | None:
        value = int(self.kernel.GetFileAttributesW(path))
        if value != 0xFFFFFFFF:
            return value
        if ctypes.get_last_error() in (2, 3):
            return None
        raise StoragePolicyError()

    def volume(self, path: str) -> tuple[int, str]:
        root = ctypes.create_unicode_buffer(32768)
        if not self.kernel.GetVolumePathNameW(path, root, len(root)):
            raise StoragePolicyError("LOCAL_NTFS_REQUIRED")
        drive_type = int(self.kernel.GetDriveTypeW(root.value))
        filesystem = ctypes.create_unicode_buffer(64)
        serial, max_component, flags = _UINT(), _UINT(), _UINT()
        if not self.kernel.GetVolumeInformationW(root.value, None, 0, ctypes.byref(serial),
                                                 ctypes.byref(max_component), ctypes.byref(flags),
                                                 filesystem, len(filesystem)):
            raise StoragePolicyError("LOCAL_NTFS_REQUIRED")
        return drive_type, filesystem.value

    def outside_cloud_root(self, path: str) -> bool:
        data, returned = ctypes.create_string_buffer(4096), _UINT()
        result = int(self.cloud.CfGetSyncRootInfoByPath(path, 0, data, len(data), ctypes.byref(returned))) & 0xFFFFFFFF
        # HRESULT_FROM_WIN32(ERROR_CLOUD_FILE_NOT_UNDER_SYNC_ROOT = 390).
        # Missing provider, denied access and unrelated HRESULTs are NOT proof
        # of an ordinary local path.  Only the exact negative fact is accepted.
        if result == 0x80070186:
            return True
        if result == 0 or result in (0x800700EA, 0x8007007A):
            return False
        raise StoragePolicyError("CLOUD_SCOPE_UNKNOWN")

    def security(self, path: str) -> SecurityFacts:
        owner, dacl, descriptor = _PTR(), _PTR(), _PTR()
        result = self.advapi.GetNamedSecurityInfoW(path, 1, 0x1 | 0x4,
                                                  ctypes.byref(owner), None,
                                                  ctypes.byref(dacl), None, ctypes.byref(descriptor))
        try:
            if result != 0 or not descriptor or not owner or not dacl:
                raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
            control, revision = _WORD(), _UINT()
            if not self.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
            header = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
            if header.size < ctypes.sizeof(_ACL) or header.count > 256:
                raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
            entries = []
            acl_start, acl_end = dacl.value, dacl.value + header.size
            for index in range(header.count):
                ace = _PTR()
                if not self.advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace:
                    raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
                if not acl_start + ctypes.sizeof(_ACL) <= ace.value <= acl_end - ctypes.sizeof(_ACE_HEADER):
                    raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
                ah = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
                # Only the simple ACCESS_ALLOWED_ACE shape is admitted.  All
                # deny/object/callback/exotic ACL policies need explicit review.
                if ah.type != 0 or ah.size < 16 or ace.value + ah.size > acl_end:
                    raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
                sid = ace.value + 8
                if not self.advapi.IsValidSid(sid) or self.advapi.GetLengthSid(sid) > ah.size - 8:
                    raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
                mask = ctypes.cast(ace.value + 4, ctypes.POINTER(_UINT)).contents.value
                entries.append(AccessEntry(ah.type, ah.flags, mask, self._sid_text(sid)))
            return SecurityFacts(self._sid_text(owner), bool(control.value & 0x4),
                                 bool(control.value & 0x1000), tuple(entries))
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)


class WindowsStoragePolicy:
    """Check a pre-created private root and one disjoint dedicated profile.

    ``validate_startup()`` raises fixed ``StoragePolicyError``.  ``policy(path)``
    returns False on a failed/unknown check and publishes ``last_failure``.
    New paths are admitted only below the root after checking all existing
    ancestors; actual creation/storage still has to succeed.
    """

    def __init__(self, root: Any, profile_directory: Any, *,
                 test_only: bool = False, native_probe: Any = None) -> None:
        if type(test_only) is not bool or (native_probe is not None and not test_only):
            raise StoragePolicyError("INPUT_INVALID")
        self.root, self.profile_directory = _windows_path(root), _windows_path(profile_directory)
        if (len(PureWindowsPath(self.root).parts) < 2
                or len(PureWindowsPath(self.profile_directory).parts) < 2
                or _inside(self.root, self.profile_directory)
                or _inside(self.profile_directory, self.root)):
            raise StoragePolicyError("INPUT_INVALID")
        if test_only and native_probe is None:
            raise StoragePolicyError("INPUT_INVALID")
        self.test_only = test_only
        self.native_qualified = False  # Native execution alone is not independent qualification.
        self._probe = native_probe if test_only else _NativeWindowsProbe()
        self.last_failure = "NONE"
        self._sync_roots = []
        for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(name):
                self._sync_roots.append(_windows_path(os.environ[name]))

    def _attributes(self, path: str, *, allow_missing: bool) -> str:
        nearest = None
        chain = _chain(path)
        for index, part in enumerate(chain):
            attributes = self._probe.attributes(part)
            if attributes is None:
                if not allow_missing or nearest is None:
                    raise StoragePolicyError("PATH_MISSING")
                return nearest
            if (type(attributes) is not int or attributes < 0
                    or attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _CLOUD_OR_OFFLINE_ATTRIBUTES)):
                raise StoragePolicyError("REPARSE_OR_CLOUD_PATH")
            if index < len(chain) - 1 and not attributes & _FILE_ATTRIBUTE_DIRECTORY:
                raise StoragePolicyError()
            nearest = part
        return nearest

    def _local(self, path: str) -> None:
        drive_type, filesystem = self._probe.volume(path)
        if type(drive_type) is not int or drive_type != 3 or filesystem != "NTFS":
            raise StoragePolicyError("LOCAL_NTFS_REQUIRED")
        if self._probe.outside_cloud_root(path) is not True:
            raise StoragePolicyError("REPARSE_OR_CLOUD_PATH")
        for part in PureWindowsPath(path).parts[1:]:
            lower = part.casefold()
            if (lower in {"dropbox", "google drive", "googledrive", "iclouddrive", "box"}
                    or lower == "onedrive" or lower.startswith("onedrive - ")):
                raise StoragePolicyError("REPARSE_OR_CLOUD_PATH")
        if any(_inside(path, sync_root) for sync_root in self._sync_roots):
            raise StoragePolicyError("REPARSE_OR_CLOUD_PATH")

    def _private(self, path: str, *, protected: bool) -> None:
        security = self._probe.security(path)
        if type(security) is not SecurityFacts or security.owner_sid != self._probe.user_sid:
            raise StoragePolicyError("OWNER_MISMATCH")
        if (security.dacl_present is not True or not security.entries
                or (protected and security.dacl_protected is not True)):
            raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
        allowed = {self._probe.user_sid, _SYSTEM, _ADMINISTRATORS}
        owner_can_control = False
        for entry in security.entries:
            if (type(entry) is not AccessEntry or entry.ace_type != 0 or entry.sid not in allowed
                    or type(entry.mask) is not int or not 0 <= entry.mask <= 0xFFFFFFFF):
                raise StoragePolicyError("PRIVATE_ACL_REQUIRED")
            if entry.sid == self._probe.user_sid and not entry.flags & 0x8:
                owner_can_control |= bool(entry.mask & _GENERIC_ALL or entry.mask & _FULL_CONTROL == _FULL_CONTROL)
        if not owner_can_control:
            raise StoragePolicyError("PRIVATE_ACL_REQUIRED")

    def _base(self, path: str) -> None:
        self._attributes(path, allow_missing=False)
        if not self._probe.attributes(path) & _FILE_ATTRIBUTE_DIRECTORY:
            raise StoragePolicyError()
        self._local(path)
        self._private(path, protected=True)

    def validate_startup(self) -> None:
        try:
            self._base(self.root)
            self._base(self.profile_directory)
            self.last_failure = "NONE"
        except StoragePolicyError as error:
            self.last_failure = error.code
            raise
        except Exception:
            self.last_failure = "STORAGE_POLICY_DENIED"
            raise StoragePolicyError() from None

    def __call__(self, path: Any) -> bool:
        try:
            destination = _windows_path(path)
            if not _inside(destination, self.root):
                raise StoragePolicyError("PATH_OUTSIDE_ROOT")
            self.validate_startup()
            nearest = self._attributes(destination, allow_missing=True)
            self._local(nearest)
            for part in _chain(nearest):
                if _inside(part, self.root):
                    self._private(part, protected=ntpath.normcase(part) == ntpath.normcase(self.root))
            self.last_failure = "NONE"
            return True
        except StoragePolicyError as error:
            self.last_failure = error.code
            return False
        except Exception:
            self.last_failure = "STORAGE_POLICY_DENIED"
            return False
