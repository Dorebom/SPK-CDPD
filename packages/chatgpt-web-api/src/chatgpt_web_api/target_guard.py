"""Strict, dependency-free ChatGPT binding boundary (approved L01 contract).

The authority is injected by the profile owner. Parsing an envelope is not an
admission operation. No Browser connection, selection or Binding issuance occurs
in this module. The Web API never imports CDPD to perform these checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import re
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from .models import FailureKind

ENDPOINT = "http://127.0.0.1:9222"
EXPECTED_ORIGIN = "https://chatgpt.com"
BINDING_SCHEMA = "cdpd.chatgpt-target-binding/v1"
EPOCH_ALGORITHM = "cdpd.browser-ws-path.sha256-128/v1"
TTL_SECONDS = 14400
MAX_JSON_BYTES = 65536
PAGE_FILTER = [{"type": "page", "exclude": False}, {"exclude": True}]
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_WS = re.compile(r"ws://127\.0\.0\.1:9222/devtools/browser/" + _UUID4)
_REASONS = frozenset({
    "BINDING_REQUIRED", "BINDING_RECORD_INVALID", "ENDPOINT_NOT_ALLOWED",
    "PROFILE_UNVERIFIED", "BROWSER_EPOCH_UNVERIFIED", "BROWSER_IDENTITY_MISMATCH",
    "TARGET_MISSING", "TARGET_AMBIGUOUS", "SINGLE_PAGE_VIOLATION",
    "TARGET_BINDING_INVALID", "TARGET_ORIGIN_MISMATCH", "TARGET_UNVERIFIED",
    "PROTOCOL_UNSUPPORTED", "BINDING_EXPIRED", "CLOCK_DISCONTINUITY",
    "BINDING_REVOKED", "CDP_CONNECT_FAILED", "CDP_COMMAND_FAILED",
    "CDP_DISCOVERY_FAILED", "CDP_DISCOVERY_TOO_LARGE", "POLICY_DENIED",
    "NAVIGATION_DURING_CAPTURE", "WORK_NOT_SETTLED", "OWNER_BUSY",
    "RECONCILIATION_REQUIRED", "INTERNAL_ERROR",
})


class GuardError(RuntimeError):
    """A fixed reason code only; never raw URLs, content or exception strings."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _REASONS else "INTERNAL_ERROR"
        if self.code in {"BROWSER_IDENTITY_MISMATCH", "TARGET_BINDING_INVALID", "TARGET_ORIGIN_MISMATCH"}:
            self.failure_kind = FailureKind.TARGET_IDENTITY_MISMATCH
        elif self.code in {"TARGET_AMBIGUOUS", "SINGLE_PAGE_VIOLATION"}:
            self.failure_kind = FailureKind.AMBIGUOUS_OBSERVATION
        elif self.code in {"WORK_NOT_SETTLED", "RECONCILIATION_REQUIRED"}:
            self.failure_kind = FailureKind.RECONCILE_REQUIRED
        else:
            self.failure_kind = FailureKind.SESSION_BINDING_FAILURE
        super().__init__(self.code)


def canonical_bytes(data: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(data, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise GuardError("BINDING_RECORD_INVALID") from None


def strict_json_loads(raw: str | bytes, max_bytes: int = MAX_JSON_BYTES) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in items:
            if key in data:
                raise ValueError("duplicate")
            data[key] = value
        return data

    def finite(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite")
        return number

    def constant(_: str) -> Any:
        raise ValueError("nonfinite")

    try:
        if type(raw) not in (str, bytes):
            raise ValueError("type")
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(data) > max_bytes:
            raise ValueError("size")
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_float=finite, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise GuardError("BINDING_RECORD_INVALID") from None


def validate_endpoint(endpoint: str) -> str:
    if type(endpoint) is not str or endpoint != ENDPOINT:
        raise GuardError("ENDPOINT_NOT_ALLOWED")
    return endpoint


def derive_browser_epoch(ws_url: str) -> str:
    if type(ws_url) is not str or not _WS.fullmatch(ws_url):
        raise GuardError("BROWSER_EPOCH_UNVERIFIED")
    return hashlib.sha256(ws_url[len("ws://127.0.0.1:9222"):].encode("utf-8")).hexdigest()[:32]


def normalize_origin(url: str) -> str:
    try:
        if type(url) is not str or not url or "\\" in url or any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError("url")
        value = urlsplit(url)
        if (value.scheme.lower() != "https" or value.hostname is None or
                value.hostname.lower() != "chatgpt.com" or value.username is not None or
                value.password is not None or value.port not in (None, 443) or "%" in value.netloc):
            raise ValueError("origin")
        return EXPECTED_ORIGIN
    except (ValueError, TypeError, UnicodeError):
        raise GuardError("TARGET_ORIGIN_MISMATCH") from None


def opaque_id(value: Any, code: str = "TARGET_UNVERIFIED") -> str:
    if type(value) is not str or not 1 <= len(value) <= 256 or not all(0x21 <= ord(c) <= 0x7E for c in value):
        raise GuardError(code)
    return value


def timestamp(value: Any) -> datetime:
    try:
        if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            raise ValueError("timestamp")
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise GuardError("BINDING_RECORD_INVALID") from None


@dataclass(frozen=True)
class ParsedBinding:
    binding_id: str
    target_id: str
    expected_browser_id: str
    issued_at_utc: str
    expires_at_utc: str
    endpoint: str = ENDPOINT
    expected_origin: str = EXPECTED_ORIGIN

    def to_dict(self) -> dict[str, Any]:
        return {"schema": BINDING_SCHEMA, "binding_id": self.binding_id,
                "epoch_algorithm": EPOCH_ALGORITHM,
                "target": {"endpoint": self.endpoint, "target_id": self.target_id,
                           "expected_origin": self.expected_origin,
                           "expected_browser_id": self.expected_browser_id},
                "issued_at_utc": self.issued_at_utc, "expires_at_utc": self.expires_at_utc}

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    @property
    def record_hash(self) -> str:
        # Matches the accepted L03 serialization, including its trailing LF.
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def record_sha256(self) -> str:
        return self.record_hash

    @classmethod
    def parse(cls, raw: ParsedBinding | dict[str, Any] | str | bytes) -> ParsedBinding:
        if isinstance(raw, cls):
            raw = raw.to_bytes()
        elif type(raw) is dict:
            raw = canonical_bytes(raw)
        data = strict_json_loads(raw)
        required = {"schema", "binding_id", "epoch_algorithm", "target", "issued_at_utc", "expires_at_utc"}
        if type(data) is not dict or set(data) != required:
            raise GuardError("BINDING_RECORD_INVALID")
        target = data["target"]
        if (data["schema"] != BINDING_SCHEMA or data["epoch_algorithm"] != EPOCH_ALGORITHM or
                type(data["binding_id"]) is not str or not re.fullmatch(_UUID4, data["binding_id"]) or
                type(target) is not dict or set(target) != {"endpoint", "target_id", "expected_origin", "expected_browser_id"}):
            raise GuardError("BINDING_RECORD_INVALID")
        if (target["endpoint"] != ENDPOINT or target["expected_origin"] != EXPECTED_ORIGIN or
                type(target["expected_browser_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", target["expected_browser_id"])):
            raise GuardError("BINDING_RECORD_INVALID")
        opaque_id(target["target_id"], "BINDING_RECORD_INVALID")
        issued, expires = timestamp(data["issued_at_utc"]), timestamp(data["expires_at_utc"])
        if (expires - issued).total_seconds() != TTL_SECONDS:
            raise GuardError("BINDING_RECORD_INVALID")
        return cls(data["binding_id"], target["target_id"], target["expected_browser_id"],
                   data["issued_at_utc"], data["expires_at_utc"])


@dataclass(frozen=True)
class ClockSample:
    utc: datetime
    monotonic: float
    suspend_epoch: int
    continuity_known: bool = True


class Clock(Protocol):
    def sample(self) -> ClockSample: ...


def valid_sample(sample: ClockSample) -> ClockSample:
    try:
        if (not isinstance(sample, ClockSample) or type(sample.utc) is not datetime or
                sample.utc.tzinfo is None or sample.utc.utcoffset() != timedelta(0) or
                type(sample.monotonic) not in (int, float) or not math.isfinite(sample.monotonic) or
                type(sample.suspend_epoch) is not int or sample.suspend_epoch < 0 or
                sample.continuity_known is not True):
            raise ValueError("clock")
        return sample
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise GuardError("CLOCK_DISCONTINUITY") from None


def _sample_clock(clock: Clock) -> ClockSample:
    """Normalize only sampling failures, before the caller latches revocation.

    A transient Clock exception is the same loss of time authority as an
    explicitly unknown sample. Do not let a later healthy sample erase it.
    Provider, authority and diagnostic errors are outside this narrow boundary.
    """
    try:
        return valid_sample(clock.sample())
    except Exception:
        raise GuardError("CLOCK_DISCONTINUITY") from None


def check_continuity(anchor: ClockSample, current: ClockSample) -> None:
    valid_sample(anchor)
    valid_sample(current)
    if (current.suspend_epoch != anchor.suspend_epoch or current.monotonic < anchor.monotonic or
            abs((current.utc - anchor.utc).total_seconds() - (current.monotonic - anchor.monotonic)) > 5.0):
        raise GuardError("CLOCK_DISCONTINUITY")


class SystemClock:
    """Windows continuity keeper, established before explicit Preflight.

    Suspend/resume notifications latch a generation even when wall and monotonic
    clocks both advance during sleep. Unavailable notification registration is
    an unknown continuity state, never an optimistic fallback. A second native
    counter comparison catches a delayed notification for a substantive sleep.
    L04 tests inject Clock samples; native Windows integration remains L11.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._epoch = 0
        self._known = False
        self._handle: Any = None
        self._callback: Any = None
        self._parameters: Any = None
        self._sleep_bias: float | None = None
        self._last_sample: ClockSample | None = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self._ctypes = ctypes
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self._power = ctypes.WinDLL("powrprof", use_last_error=True)
            callback_type = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p)

            class Parameters(ctypes.Structure):
                _fields_ = [("Callback", callback_type), ("Context", ctypes.c_void_p)]

            def notification(_context: Any, event: int, _setting: Any) -> int:
                if event in {4, 7, 18}:  # suspend, resume, automatic resume
                    with self._lock:
                        self._epoch += 1
                return 0

            self._callback = callback_type(notification)
            self._parameters = Parameters(self._callback, None)
            self._power.PowerRegisterSuspendResumeNotification.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            self._power.PowerRegisterSuspendResumeNotification.restype = wintypes.DWORD
            self._power.PowerUnregisterSuspendResumeNotification.argtypes = [ctypes.c_void_p]
            self._power.PowerUnregisterSuspendResumeNotification.restype = wintypes.DWORD
            handle = ctypes.c_void_p()
            if self._power.PowerRegisterSuspendResumeNotification(2, ctypes.byref(self._parameters), ctypes.byref(handle)) != 0:
                return
            self._handle = handle
            self._kernel.GetTickCount64.argtypes = []
            self._kernel.GetTickCount64.restype = ctypes.c_ulonglong
            self._kernel.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
            self._kernel.QueryUnbiasedInterruptTime.restype = wintypes.BOOL
            self._sleep_bias = self._native_bias()
            self._known = True
        except Exception:
            self._known = False

    def _native_bias(self) -> float:
        unbiased = self._ctypes.c_ulonglong()
        if not self._kernel.QueryUnbiasedInterruptTime(self._ctypes.byref(unbiased)):
            raise GuardError("CLOCK_DISCONTINUITY")
        return float(self._kernel.GetTickCount64()) / 1000.0 - float(unbiased.value) / 10_000_000.0

    def sample(self) -> ClockSample:
        try:
            bias = self._native_bias() if self._known else None
            with self._lock:
                if bias is not None and self._sleep_bias is not None and abs(bias - self._sleep_bias) > 0.05:
                    self._epoch += 1
                    self._sleep_bias = bias
                sample = ClockSample(datetime.now(timezone.utc), time.monotonic(), self._epoch, self._known)
                self._last_sample = sample
                return sample
        except Exception:
            with self._lock:
                self._known = False
                return ClockSample(datetime.now(timezone.utc), time.monotonic(), self._epoch, False)

    def close(self) -> None:
        with self._lock:
            self._known = False
            self._epoch += 1
            handle, self._handle = self._handle, None
        if handle is not None:
            try:
                self._power.PowerUnregisterSuspendResumeNotification(handle)
            except Exception:
                pass


@dataclass(frozen=True)
class AuthoritySnapshot:
    binding_id: str
    record_hash: str
    generation: int
    state: str = "BOUND"
    revoked: bool = False
    superseded: bool = False
    continuity_known: bool = True
    mode: str = "NORMAL"


class BindingAuthority(Protocol):
    def snapshot(self) -> AuthoritySnapshot: ...
    def acquire_operation(self, token: object) -> None: ...
    def release_operation(self, token: object) -> None: ...
    def claim_operation_id(self, method: str, operation_id: str) -> None: ...
    def invalidate(self, reason: str, *, binding_id: str | None = None, generation: int | None = None) -> None: ...
    def set_mode(self, mode: str, *, binding_id: str | None = None, generation: int | None = None) -> None: ...


class BindingGuard:
    """One immutable admitted generation, shared with a trusted live authority."""

    def __init__(self, record: ParsedBinding | dict[str, Any] | str | bytes,
                 authority: BindingAuthority, *, clock: Clock | None = None) -> None:
        self.record = ParsedBinding.parse(record)
        if authority is None or not callable(getattr(authority, "snapshot", None)):
            raise GuardError("BINDING_REQUIRED")
        self._authority = authority
        self._clock: Clock = clock if clock is not None else getattr(authority, "clock", None) or SystemClock()
        self._state = "UNBOUND"
        self._reason = "BINDING_REQUIRED"
        self._mode = "NORMAL"
        self._terminal = False
        self._generation: int | None = None
        try:
            # Initial sampling must share the same terminal failure boundary
            # as later checks. Even a failed new guard must revoke this record's
            # existing live guards; it must not revoke another binding_id.
            self._anchor = _sample_clock(self._clock)
            self._issued = timestamp(self.record.issued_at_utc)
            self._expires = timestamp(self.record.expires_at_utc)
            self._deadline = self._anchor.monotonic + (self._expires - self._anchor.utc).total_seconds()
            snapshot = self._read_snapshot()
            self._validate_snapshot(snapshot, initial=True)
            self._generation = snapshot.generation
            self._state, self._reason = "BOUND", "NONE"
            self.check()
        except GuardError as error:
            self.invalidate(error.code)
            raise

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def generation(self) -> int | None:
        return self._generation

    @property
    def mode(self) -> str:
        return self._mode

    def _read_snapshot(self) -> AuthoritySnapshot:
        try:
            snapshot = self._authority.snapshot()
        except GuardError:
            raise
        except Exception:
            raise GuardError("BINDING_REVOKED") from None
        if not isinstance(snapshot, AuthoritySnapshot):
            raise GuardError("BINDING_RECORD_INVALID")
        return snapshot

    def _validate_snapshot(self, current: AuthoritySnapshot, *, initial: bool = False) -> None:
        if current.continuity_known is not True:
            raise GuardError("CLOCK_DISCONTINUITY")
        if (type(current.generation) is not int or current.generation < 1 or
                (not initial and current.generation != self._generation) or
                current.binding_id != self.record.binding_id or current.revoked is not False or
                current.superseded is not False or current.state != "BOUND"):
            raise GuardError("BINDING_REVOKED")
        if current.record_hash != self.record.record_hash:
            raise GuardError("BINDING_RECORD_INVALID")
        if current.mode not in {"NORMAL", "DIAGNOSTIC_ONLY", "RECONCILIATION_REQUIRED"}:
            raise GuardError("BINDING_REVOKED")
        # Overlay restrictions are sticky; no lower-trust current value clears them.
        if current.mode == "RECONCILIATION_REQUIRED" or self._mode == "RECONCILIATION_REQUIRED":
            self._mode = "RECONCILIATION_REQUIRED"
        elif current.mode == "DIAGNOSTIC_ONLY" or self._mode == "DIAGNOSTIC_ONLY":
            self._mode = "DIAGNOSTIC_ONLY"

    def check(self, mutation: bool = False, *, allow_navigating: bool = False) -> None:
        if self._terminal:
            raise GuardError(self._reason)
        try:
            self._validate_snapshot(self._read_snapshot())
            sample = _sample_clock(self._clock)
            check_continuity(self._anchor, sample)
            if not self._issued <= sample.utc < self._expires or sample.monotonic >= self._deadline:
                raise GuardError("BINDING_EXPIRED")
        except GuardError as error:
            self.invalidate(error.code)
            raise GuardError(self._reason) from None
        if self._state == "NAVIGATING" and not allow_navigating:
            raise GuardError("WORK_NOT_SETTLED")
        if self._state not in {"BOUND", "NAVIGATING"}:
            raise GuardError("BINDING_REVOKED")
        if mutation and self._mode != "NORMAL":
            raise GuardError("RECONCILIATION_REQUIRED" if self._mode == "RECONCILIATION_REQUIRED" else "POLICY_DENIED")

    def invalidate(self, reason: str) -> None:
        if self._terminal:
            return
        safe = GuardError(reason).code
        self._terminal = True
        self._state = "EXPIRED" if safe == "BINDING_EXPIRED" else "INVALIDATED"
        self._reason = safe
        try:
            self._authority.invalidate(safe, binding_id=self.record.binding_id, generation=self._generation)
        except Exception:
            # Local denial remains latched even if sharing failure metadata fails.
            pass

    def require_reconciliation(self) -> None:
        self._mode = "RECONCILIATION_REQUIRED"
        try:
            self._authority.set_mode(self._mode, binding_id=self.record.binding_id, generation=self._generation)
        except Exception:
            self.invalidate("BINDING_REVOKED")

    def set_diagnostic_only(self) -> None:
        if self._mode != "RECONCILIATION_REQUIRED":
            self._mode = "DIAGNOSTIC_ONLY"
        try:
            self._authority.set_mode(self._mode, binding_id=self.record.binding_id, generation=self._generation)
        except Exception:
            self.invalidate("BINDING_REVOKED")

    def begin_operation(self, token: object) -> None:
        """Reserve the shared owner; no reservation capability means no dispatch."""
        self.check(allow_navigating=True)
        acquire = getattr(self._authority, "acquire_operation", None)
        release = getattr(self._authority, "release_operation", None)
        if token is None or not callable(acquire) or not callable(release):
            raise GuardError("BINDING_REQUIRED")
        try:
            acquire(token)
        except GuardError:
            raise
        except Exception:
            self.invalidate("BINDING_REVOKED")
            raise GuardError("BINDING_REVOKED") from None
        try:
            self.check(allow_navigating=True)
        except Exception:
            try:
                release(token)
            except Exception:
                pass
            raise

    def end_operation(self, token: object) -> None:
        """Release only this token; it never clears an uncertain-result overlay."""
        release = getattr(self._authority, "release_operation", None)
        if token is None or not callable(release):
            self.invalidate("BINDING_REVOKED")
            raise GuardError("BINDING_REVOKED")
        try:
            release(token)
        except GuardError:
            raise
        except Exception:
            self.invalidate("BINDING_REVOKED")
            raise GuardError("BINDING_REVOKED") from None

    def claim_operation_id(self, method: str, operation_id: str) -> None:
        """Consume one caller-owned operation identity in the shared owner ledger."""
        self.check(mutation=True)
        claim = getattr(self._authority, "claim_operation_id", None)
        if not callable(claim):
            raise GuardError("BINDING_REQUIRED")
        try:
            claim(method, operation_id)
        except GuardError:
            raise
        except Exception:
            self.invalidate("BINDING_REVOKED")
            raise GuardError("BINDING_REVOKED") from None
        self.check(mutation=True)

    def begin_navigation(self) -> None:
        self.check(mutation=True)
        self._state = "NAVIGATING"

    def navigation_settled(self) -> None:
        # Session must first establish same-socket full live identity; this only
        # changes the document overlay and cannot renew or change the Binding.
        self.check(allow_navigating=True)
        if self._state != "NAVIGATING":
            raise GuardError("WORK_NOT_SETTLED")
        self._state = "BOUND"

    def assert_endpoint(self, endpoint: str) -> None:
        try:
            validate_endpoint(endpoint)
            if endpoint != self.record.endpoint:
                raise GuardError("ENDPOINT_NOT_ALLOWED")
            self.check()
        except GuardError as error:
            self.invalidate(error.code)
            raise

    def assert_ws(self, ws_url: str) -> str:
        try:
            epoch = derive_browser_epoch(ws_url)
            if epoch != self.record.expected_browser_id:
                raise GuardError("BROWSER_IDENTITY_MISMATCH")
            self.check(allow_navigating=True)
            return epoch
        except GuardError as error:
            self.invalidate(error.code)
            raise


def single_page(targets: Any) -> dict[str, Any]:
    """Validate a fresh complete page-filter reply; no weak list fallback."""
    if type(targets) is not list:
        raise GuardError("TARGET_UNVERIFIED")
    seen: set[str] = set()
    pages: list[dict[str, Any]] = []
    for info in targets:
        if type(info) is not dict:
            raise GuardError("TARGET_UNVERIFIED")
        target_id = opaque_id(info.get("targetId"))
        if target_id in seen:
            raise GuardError("TARGET_UNVERIFIED")
        seen.add(target_id)
        kind = info.get("type")
        if kind == "page":
            if info.get("subtype", "") != "" or type(info.get("url")) is not str:
                raise GuardError("TARGET_UNVERIFIED")
            pages.append(info)
        elif kind not in {"worker", "service_worker", "shared_worker", "iframe", "browser", "other"}:
            raise GuardError("TARGET_UNVERIFIED")
    if not pages:
        raise GuardError("TARGET_MISSING")
    matching = 0
    for info in pages:
        try:
            normalize_origin(info["url"])
            matching += 1
        except GuardError:
            pass
    if matching > 1:
        raise GuardError("TARGET_AMBIGUOUS")
    if len(pages) != 1:
        raise GuardError("SINGLE_PAGE_VIOLATION")
    normalize_origin(pages[0]["url"])
    return pages[0]


def same_target(actual: Any, selected: dict[str, Any]) -> None:
    if type(actual) is not dict or actual.get("type") != "page" or actual.get("targetId") != selected["targetId"]:
        raise GuardError("TARGET_BINDING_INVALID")
    opaque_id(actual.get("targetId"))
    if actual.get("subtype", "") != "":
        raise GuardError("TARGET_UNVERIFIED")
    normalize_origin(actual.get("url"))
    if actual["url"] != selected["url"]:
        raise GuardError("TARGET_UNVERIFIED")


def frame_snapshot(result: Any, selected: dict[str, Any]) -> tuple[str, str, str, str]:
    if type(result) is not dict or type(result.get("frameTree")) is not dict:
        raise GuardError("TARGET_UNVERIFIED")
    frame = result["frameTree"].get("frame")
    if type(frame) is not dict or "parentId" in frame or frame.get("unreachableUrl"):
        raise GuardError("TARGET_UNVERIFIED")
    frame_id, loader_id = opaque_id(frame.get("id")), opaque_id(frame.get("loaderId"))
    normalize_origin(frame.get("url"))
    if frame.get("securityOrigin") != EXPECTED_ORIGIN:
        raise GuardError("TARGET_ORIGIN_MISMATCH")
    if frame["url"] != selected["url"]:
        raise GuardError("TARGET_UNVERIFIED")
    return frame_id, loader_id, frame["url"], EXPECTED_ORIGIN
