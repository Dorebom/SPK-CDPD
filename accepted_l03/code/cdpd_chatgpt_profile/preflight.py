"""CDPD-L03: explicit, metadata-only preflight for the approved L01 binding.

Runtime: Python >=3.11 and websockets==16.0. Production OS inspection requires
Windows and the sibling scripts/start_chrome_cdp.ps1. No browser launch, UI
operation, DOM read, arbitrary CDP method, retry, or compatibility shim lives
here. An issued record is an identity assertion; L04 must guard actual Web
operations, and L05/L07 own persistent state and clock continuity.

Only the explicit PreflightOwner.preflight entry issues a record. revalidate
checks the same owner's current record without extending it. Closing this
short-lived inspection connection normally isn't an operational disconnect.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid


ENDPOINT = "http://127.0.0.1:9222"
EXPECTED_ORIGIN = "https://chatgpt.com"
BINDING_SCHEMA = "cdpd.chatgpt-target-binding/v1"
EPOCH_ALGORITHM = "cdpd.browser-ws-path.sha256-128/v1"
TTL_SECONDS = 14400
MAX_JSON_BYTES = 65536
WEBSOCKETS_VERSION = "16.0"
PAGE_FILTER = [{"type": "page", "exclude": False}, {"exclude": True}]
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_WS_PATTERN = re.compile(r"ws://127\.0\.0\.1:9222/devtools/browser/" + _UUID4)
_BROWSER_METHODS = frozenset({
    "Target.setDiscoverTargets", "Target.getTargets", "Target.getTargetInfo",
    "Target.attachToTarget", "Target.detachFromTarget",
})
_SESSION_METHODS = frozenset({"Page.getFrameTree"})
_REASONS = frozenset({
    "BINDING_REQUIRED", "BINDING_RECORD_INVALID", "BINDING_EXPIRED",
    "BINDING_REVOKED", "CLOCK_DISCONTINUITY", "WORK_NOT_SETTLED",
    "OWNER_BUSY", "POLICY_DENIED", "ENDPOINT_NOT_ALLOWED",
    "PROFILE_UNVERIFIED", "PROFILE_COLLISION", "PORT_OCCUPIED",
    "LISTENER_NOT_LOOPBACK", "UNSUPPORTED_PLATFORM", "DEPENDENCY_MISSING",
    "OS_INSPECTION_UNAVAILABLE", "CONCURRENT_LAUNCH", "INVALID_ARGUMENT",
    "STARTUP_FAILED", "STARTUP_TIMEOUT", "STORAGE_FAILED",
    "DEPENDENCY_VERSION_UNSUPPORTED", "BROWSER_EPOCH_UNVERIFIED",
    "BROWSER_IDENTITY_MISMATCH", "TARGET_MISSING", "TARGET_AMBIGUOUS",
    "SINGLE_PAGE_VIOLATION", "TARGET_ORIGIN_MISMATCH",
    "TARGET_BINDING_INVALID", "TARGET_UNVERIFIED", "PROTOCOL_UNSUPPORTED",
    "CDP_DISCOVERY_FAILED", "CDP_DISCOVERY_TOO_LARGE", "CDP_CONNECT_FAILED",
    "CDP_COMMAND_FAILED", "CDP_RESPONSE_TOO_LARGE", "PREFLIGHT_DEADLINE",
    "PREFLIGHT_INPUT_INVALID", "COMMAND_NOT_ALLOWED", "OUTPUT_WRITE_FAILED",
    "INTERNAL_ERROR",
})


class PreflightError(RuntimeError):
    """Only fixed codes cross the logging or result boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _REASONS else "INTERNAL_ERROR"
        super().__init__(self.code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def strict_json_loads(raw: str | bytes, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Reject duplicate keys, nonfinite values, over-limit input and bad UTF-8."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite")
        return number

    def constant(_: str) -> Any:
        raise ValueError("nonfinite")

    try:
        if not isinstance(raw, (str, bytes)):
            raise ValueError("type")
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(data) > max_bytes:
            raise ValueError("size")
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_float=finite_float, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise PreflightError("BINDING_RECORD_INVALID") from None


def validate_endpoint(endpoint: str) -> str:
    if type(endpoint) is not str or endpoint != ENDPOINT:
        raise PreflightError("ENDPOINT_NOT_ALLOWED")
    return endpoint


def derive_browser_epoch(ws_url: str) -> str:
    if type(ws_url) is not str or not _WS_PATTERN.fullmatch(ws_url):
        raise PreflightError("BROWSER_EPOCH_UNVERIFIED")
    path = ws_url[len("ws://127.0.0.1:9222"):]
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]


def normalize_origin(url: str) -> str:
    try:
        if not isinstance(url, str) or not url or "\\" in url:
            raise ValueError("url")
        if any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError("control")
        value = urlsplit(url)
        if (value.scheme.lower() != "https" or
                value.hostname is None or value.hostname.lower() != "chatgpt.com" or
                value.username is not None or value.password is not None or
                value.port not in (None, 443) or "%" in value.netloc):
            raise ValueError("origin")
        return EXPECTED_ORIGIN
    except (ValueError, TypeError, UnicodeError):
        raise PreflightError("TARGET_ORIGIN_MISMATCH") from None


def _opaque_id(value: Any, code: str = "TARGET_UNVERIFIED") -> str:
    if (type(value) is not str or not 1 <= len(value) <= 256 or
            not all(0x21 <= ord(c) <= 0x7E for c in value)):
        raise PreflightError(code)
    return value


def _timestamp(value: Any) -> datetime:
    try:
        if type(value) is not str or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            raise ValueError("timestamp")
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise PreflightError("BINDING_RECORD_INVALID") from None


@dataclass(frozen=True)
class BindingRecord:
    binding_id: str
    target_id: str
    expected_browser_id: str
    issued_at_utc: str
    expires_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BINDING_SCHEMA, "binding_id": self.binding_id,
            "epoch_algorithm": EPOCH_ALGORITHM,
            "target": {"endpoint": ENDPOINT, "target_id": self.target_id,
                       "expected_origin": EXPECTED_ORIGIN,
                       "expected_browser_id": self.expected_browser_id},
            "issued_at_utc": self.issued_at_utc, "expires_at_utc": self.expires_at_utc,
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


def parse_binding_record(raw: str | bytes) -> BindingRecord:
    data = strict_json_loads(raw)
    required = {"schema", "binding_id", "epoch_algorithm", "target",
                "issued_at_utc", "expires_at_utc"}
    if type(data) is not dict or set(data) != required:
        raise PreflightError("BINDING_RECORD_INVALID")
    target = data["target"]
    if (data["schema"] != BINDING_SCHEMA or data["epoch_algorithm"] != EPOCH_ALGORITHM or
            type(data["binding_id"]) is not str or not re.fullmatch(_UUID4, data["binding_id"]) or
            type(target) is not dict or set(target) != {
                "endpoint", "target_id", "expected_origin", "expected_browser_id"}):
        raise PreflightError("BINDING_RECORD_INVALID")
    if (target["endpoint"] != ENDPOINT or target["expected_origin"] != EXPECTED_ORIGIN or
            type(target["expected_browser_id"]) is not str or
            not re.fullmatch(r"[0-9a-f]{32}", target["expected_browser_id"])):
        raise PreflightError("BINDING_RECORD_INVALID")
    _opaque_id(target["target_id"], "BINDING_RECORD_INVALID")
    issued, expires = _timestamp(data["issued_at_utc"]), _timestamp(data["expires_at_utc"])
    if (expires - issued).total_seconds() != TTL_SECONDS:
        raise PreflightError("BINDING_RECORD_INVALID")
    return BindingRecord(data["binding_id"], target["target_id"],
                         target["expected_browser_id"], data["issued_at_utc"],
                         data["expires_at_utc"])


@dataclass(frozen=True)
class PreflightConfig:
    chrome_path: str
    user_data_dir: str
    state_directory: str
    endpoint: str = ENDPOINT
    timeout_seconds: float = 10.0


class _Budget:
    def __init__(self, timeout: float) -> None:
        self.deadline = _monotonic() + timeout

    def remaining(self) -> float:
        remaining = self.deadline - _monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise PreflightError("PREFLIGHT_DEADLINE")
        return remaining


def _check_runtime_dependency() -> None:
    try:
        if importlib.metadata.version("websockets") != WEBSOCKETS_VERSION:
            raise PreflightError("DEPENDENCY_VERSION_UNSUPPORTED")
        from websockets.sync.client import connect  # noqa: F401
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise PreflightError("DEPENDENCY_MISSING") from None


def _inspect_launch(config: PreflightConfig, timeout_seconds: float) -> dict[str, Any]:
    """Read-only OS observation. A user-provided JSON attestation isn't accepted."""
    if sys.platform != "win32":
        raise PreflightError("UNSUPPORTED_PLATFORM")
    script = Path(__file__).resolve().parents[1] / "scripts" / "start_chrome_cdp.ps1"
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not script.is_file() or not powershell.is_file():
        raise PreflightError("DEPENDENCY_MISSING")
    command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script),
               "-Mode", "Inspect", "-ChromePath", config.chrome_path,
               "-UserDataDir", config.user_data_dir, "-StateDirectory", config.state_directory,
               "-Endpoint", config.endpoint]
    try:
        run = subprocess.run(command, capture_output=True, timeout=timeout_seconds,
                             check=False, shell=False)
        try:
            observation = strict_json_loads(run.stdout)
        except PreflightError:
            # A blocked PowerShell policy / failed OS query isn't a malformed
            # binding. Never expose its potentially sensitive stderr as a reason.
            raise PreflightError("PROFILE_UNVERIFIED") from None
        if run.returncode != 0:
            reason = observation.get("reason_code") if isinstance(observation, dict) else None
            raise PreflightError(reason if reason in _REASONS else "PROFILE_UNVERIFIED")
        if run.stderr or type(observation) is not dict:
            raise PreflightError("PROFILE_UNVERIFIED")
        return observation
    except PreflightError:
        raise
    except subprocess.TimeoutExpired:
        raise PreflightError("PREFLIGHT_DEADLINE") from None
    except Exception:
        raise PreflightError("PROFILE_UNVERIFIED") from None


def _validate_launch(observation: Any) -> dict[str, Any]:
    if type(observation) is not dict:
        raise PreflightError("PROFILE_UNVERIFIED")
    if observation.get("status") != "PASS":
        code = observation.get("reason_code")
        raise PreflightError(code if code in _REASONS else "PROFILE_UNVERIFIED")
    keys = {"schema", "status", "endpoint", "launch_id", "record_sha256", "process_id",
            "process_started_utc", "profile_path_sha256", "executable_path_sha256",
            "owner_sid_sha256", "listener_address", "listener_port", "evidence_kind", "action"}
    if (set(observation) != keys or
            observation["schema"] != "cdpd.chrome-launch-observation/v1" or
            observation["endpoint"] != ENDPOINT or observation["action"] != "INSPECTED" or
            observation["evidence_kind"] != "WINDOWS_OS_INSPECTION" or
            type(observation["process_id"]) is not int or observation["process_id"] <= 0 or
            type(observation["launch_id"]) is not str or
            not re.fullmatch(_UUID4, observation["launch_id"])):
        raise PreflightError("PROFILE_UNVERIFIED")
    if observation["listener_address"] != "127.0.0.1" or (
            type(observation["listener_port"]) is not int or observation["listener_port"] != 9222):
        raise PreflightError("LISTENER_NOT_LOOPBACK")
    for key in ("record_sha256", "profile_path_sha256", "executable_path_sha256", "owner_sid_sha256"):
        if type(observation[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", observation[key]):
            raise PreflightError("PROFILE_UNVERIFIED")
    try:
        started = datetime.fromisoformat(observation["process_started_utc"].replace("Z", "+00:00"))
        if started.tzinfo is None or started.utcoffset() != timedelta(0):
            raise ValueError("timezone")
    except (ValueError, TypeError, AttributeError):
        raise PreflightError("PROFILE_UNVERIFIED") from None
    return {key: observation[key] for key in sorted(keys)}


def _discover(endpoint: str, timeout_seconds: float) -> tuple[str, str]:
    validate_endpoint(endpoint)  # Before constructing any network object.
    budget = _Budget(timeout_seconds)
    connection = http.client.HTTPConnection("127.0.0.1", 9222, timeout=timeout_seconds)
    connection.set_debuglevel(0)
    expired = threading.Event()
    raw_socket: socket.socket | None = None
    timer: threading.Timer | None = None

    def expire() -> None:
        expired.set()
        if raw_socket is not None:
            try:
                raw_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    try:
        connection.connect()  # One direct connect; no proxy or redirect handling.
        raw_socket = connection.sock
        # Socket timeouts alone are inactivity limits. A separate absolute
        # deadline also terminates slow header/body trickles on this socket.
        timer = threading.Timer(budget.remaining(), expire)
        timer.daemon = True
        timer.start()
        connection.request("GET", "/json/version", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise PreflightError("CDP_DISCOVERY_FAILED")  # Never follow a Location header.
        raw = response.read(MAX_JSON_BYTES + 1)
        if expired.is_set():
            raise PreflightError("PREFLIGHT_DEADLINE")
        budget.remaining()
        if len(raw) > MAX_JSON_BYTES:
            raise PreflightError("CDP_DISCOVERY_TOO_LARGE")
        try:
            metadata = strict_json_loads(raw)
        except PreflightError:
            raise PreflightError("CDP_DISCOVERY_FAILED") from None
        if type(metadata) is not dict or "webSocketDebuggerUrl" not in metadata:
            raise PreflightError("BROWSER_EPOCH_UNVERIFIED")
        ws_url = metadata["webSocketDebuggerUrl"]
        return ws_url, derive_browser_epoch(ws_url)
    except PreflightError:
        raise
    except TimeoutError:
        raise PreflightError("PREFLIGHT_DEADLINE") from None
    except Exception:
        if expired.is_set():
            raise PreflightError("PREFLIGHT_DEADLINE") from None
        raise PreflightError("CDP_CONNECT_FAILED") from None
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


class _CDPConnection:
    """A narrow one-shot connection: exact WS URL, no proxy and no retries."""

    def __init__(self, ws_url: str, timeout_seconds: float) -> None:
        derive_browser_epoch(ws_url)
        _check_runtime_dependency()
        self._budget = _Budget(timeout_seconds)
        self._sequence = 0
        self._events: list[dict[str, Any]] = []
        self._expired = threading.Event()
        self._timer: threading.Timer | None = None
        self._socket: Any = None
        # An unregistered Logger with no propagation prevents library DEBUG/raw
        # frame output even when the caller enables its root logger.
        quiet = logging.Logger("cdpd.preflight.transport")
        quiet.addHandler(logging.NullHandler())
        quiet.propagate = False
        quiet.disabled = True
        try:
            from websockets.sync.client import connect
            self._socket = connect(ws_url, proxy=None, origin=None, compression=None,
                                   user_agent_header=None, ping_interval=None,
                                   open_timeout=self._budget.remaining(), close_timeout=0.2,
                                   max_size=MAX_JSON_BYTES, max_queue=32, logger=quiet)
            self._timer = threading.Timer(self._budget.remaining(), self._expire)
            self._timer.daemon = True
            self._timer.start()
        except Exception as error:
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:
                    pass
            if isinstance(error, PreflightError):
                raise
            raise PreflightError("CDP_CONNECT_FAILED") from None

    def _expire(self) -> None:
        self._expired.set()
        try:
            self._socket.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def call(self, method: str, params: dict[str, Any],
             session_id: str | None = None) -> dict[str, Any]:
        if method not in (_SESSION_METHODS if session_id is not None else _BROWSER_METHODS):
            raise PreflightError("COMMAND_NOT_ALLOWED")
        if method in {"Target.getTargets", "Target.setDiscoverTargets"}:
            expected = {"filter": PAGE_FILTER}
            if method == "Target.setDiscoverTargets":
                expected["discover"] = True
            if params != expected:
                raise PreflightError("COMMAND_NOT_ALLOWED")
        self._sequence += 1
        packet: dict[str, Any] = {"id": self._sequence, "method": method, "params": params}
        if session_id is not None:
            packet["sessionId"] = session_id
        try:
            self._budget.remaining()
            self._socket.send(json.dumps(packet, separators=(",", ":"), allow_nan=False))
            while True:
                raw = self._socket.recv(timeout=self._budget.remaining())
                if not isinstance(raw, (bytes, str)):
                    raise PreflightError("CDP_COMMAND_FAILED")
                if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_JSON_BYTES:
                    raise PreflightError("CDP_RESPONSE_TOO_LARGE")
                try:
                    message = strict_json_loads(raw)
                except PreflightError:
                    raise PreflightError("CDP_COMMAND_FAILED") from None
                if type(message) is not dict:
                    raise PreflightError("CDP_COMMAND_FAILED")
                if "id" not in message:
                    if type(message.get("method")) is not str or type(message.get("params", {})) is not dict:
                        raise PreflightError("CDP_COMMAND_FAILED")
                    self._events.append(message)
                    if len(self._events) > 256:
                        raise PreflightError("TARGET_UNVERIFIED")
                    continue
                if type(message["id"]) is not int or message["id"] != self._sequence:
                    raise PreflightError("CDP_COMMAND_FAILED")
                if message.get("sessionId") != session_id:
                    raise PreflightError("TARGET_BINDING_INVALID")
                if "error" in message:
                    error = message["error"]
                    if (method in {"Target.getTargets", "Target.setDiscoverTargets"} and
                            type(error) is dict and error.get("code") in {-32601, -32602}):
                        raise PreflightError("PROTOCOL_UNSUPPORTED")
                    raise PreflightError("CDP_COMMAND_FAILED")
                if type(message.get("result")) is not dict:
                    raise PreflightError("CDP_COMMAND_FAILED")
                return message["result"]
        except PreflightError:
            raise
        except TimeoutError:
            raise PreflightError("PREFLIGHT_DEADLINE") from None
        except Exception:
            if self._expired.is_set():
                raise PreflightError("PREFLIGHT_DEADLINE") from None
            raise PreflightError("CDP_COMMAND_FAILED") from None

    def take_events(self) -> list[dict[str, Any]]:
        events, self._events = self._events, []
        return events

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        try:
            self._socket.close()
        except Exception:
            pass  # No reconnect and no shutdown of the browser or its targets.


def _open_connection(ws_url: str, timeout_seconds: float) -> _CDPConnection:
    return _CDPConnection(ws_url, timeout_seconds)


def _single_page(targets: Any) -> dict[str, Any]:
    if type(targets) is not list:
        raise PreflightError("TARGET_UNVERIFIED")
    seen: set[str] = set()
    pages = []
    for target in targets:
        if type(target) is not dict:
            raise PreflightError("TARGET_UNVERIFIED")
        target_id = _opaque_id(target.get("targetId"))
        if target_id in seen:
            raise PreflightError("TARGET_UNVERIFIED")
        seen.add(target_id)
        target_type = target.get("type")
        if target_type == "page":
            if target.get("subtype", "") != "" or type(target.get("url")) is not str:
                raise PreflightError("TARGET_UNVERIFIED")
            pages.append(target)
        elif target_type not in {"worker", "service_worker", "shared_worker", "iframe", "browser", "other"}:
            raise PreflightError("TARGET_UNVERIFIED")
    if not pages:
        raise PreflightError("TARGET_MISSING")
    matching = 0
    for page in pages:
        try:
            normalize_origin(page["url"])
            matching += 1
        except PreflightError:
            pass
    if matching > 1:
        raise PreflightError("TARGET_AMBIGUOUS")
    if len(pages) != 1:
        raise PreflightError("SINGLE_PAGE_VIOLATION")
    normalize_origin(pages[0]["url"])
    return pages[0]


def _same_target(actual: Any, selected: dict[str, Any]) -> None:
    if type(actual) is not dict or actual.get("type") != "page":
        raise PreflightError("TARGET_BINDING_INVALID")
    if actual.get("targetId") != selected["targetId"]:
        raise PreflightError("TARGET_BINDING_INVALID")
    if actual.get("subtype", "") != "":
        raise PreflightError("TARGET_UNVERIFIED")
    normalize_origin(actual.get("url"))
    if actual["url"] != selected["url"]:
        raise PreflightError("TARGET_UNVERIFIED")


def _check_events(events: list[dict[str, Any]], selected: dict[str, Any],
                  *, initialization: bool = False, session_id: str | None = None,
                  detaching: bool = False) -> None:
    for event in events:
        if type(event) is not dict or type(event.get("params", {})) is not dict:
            raise PreflightError("TARGET_UNVERIFIED")
        method, params = event.get("method"), event.get("params", {})
        if method == "Target.targetCreated":
            info = params.get("targetInfo")
            if type(info) is not dict:
                raise PreflightError("TARGET_UNVERIFIED")
            if info.get("type") != "page":
                continue
            if initialization:
                _same_target(info, selected)
            else:
                raise PreflightError("TARGET_UNVERIFIED")
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo")
            if type(info) is not dict:
                raise PreflightError("TARGET_UNVERIFIED")
            if info.get("type") == "page" or info.get("targetId") == selected["targetId"]:
                _same_target(info, selected)
        elif method in {"Target.targetDestroyed", "Target.targetCrashed", "Inspector.detached"}:
            raise PreflightError("TARGET_UNVERIFIED")
        elif method == "Target.attachedToTarget":
            _same_target(params.get("targetInfo"), selected)
            if session_id is not None and params.get("sessionId") != session_id:
                raise PreflightError("TARGET_BINDING_INVALID")
        elif method == "Target.detachedFromTarget":
            if not detaching or params.get("sessionId") != session_id:
                raise PreflightError("TARGET_UNVERIFIED")
        elif type(method) is not str or method.startswith(("Target.", "Page.", "Inspector.")):
            raise PreflightError("TARGET_UNVERIFIED")
        # Non-target protocol notifications aren't persisted or used to select a tab.


def _frame_snapshot(result: Any, selected: dict[str, Any]) -> tuple[str, str, str, str]:
    if type(result) is not dict or type(result.get("frameTree")) is not dict:
        raise PreflightError("TARGET_UNVERIFIED")
    frame = result["frameTree"].get("frame")
    if type(frame) is not dict or "parentId" in frame or frame.get("unreachableUrl"):
        raise PreflightError("TARGET_UNVERIFIED")
    frame_id = _opaque_id(frame.get("id"))
    loader_id = _opaque_id(frame.get("loaderId"))
    normalize_origin(frame.get("url"))
    if frame.get("securityOrigin") != EXPECTED_ORIGIN:
        raise PreflightError("TARGET_ORIGIN_MISMATCH")
    if frame["url"] != selected["url"]:
        raise PreflightError("TARGET_UNVERIFIED")
    return frame_id, loader_id, frame["url"], EXPECTED_ORIGIN


def _probe(config: PreflightConfig, expected: BindingRecord | None,
           audit: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    budget = _Budget(config.timeout_seconds)
    audit["launch_inspections"] += 1
    launch = _validate_launch(_inspect_launch(config, budget.remaining()))

    def discover() -> tuple[str, str]:
        audit["discoveries"] += 1
        url, epoch = _discover(config.endpoint, budget.remaining())
        if derive_browser_epoch(url) != epoch:
            raise PreflightError("BROWSER_EPOCH_UNVERIFIED")
        return url, epoch

    ws_url, epoch = discover()
    if expected is not None and expected.expected_browser_id != epoch:
        raise PreflightError("BROWSER_IDENTITY_MISMATCH")
    audit["websocket_connections"] += 1
    connection = _open_connection(ws_url, budget.remaining())
    session: str | None = None
    detached = False
    selected: dict[str, Any] | None = None

    def call(method: str, params: dict[str, Any], *, attached: bool = False,
             initial: bool = False, detaching: bool = False) -> dict[str, Any]:
        budget.remaining()
        audit["cdp_commands"].append(method)
        result = connection.call(method, params, session_id=session if attached else None)
        if selected is not None and not initial:
            _check_events(connection.take_events(), selected, session_id=session,
                          detaching=detaching)
        return result

    try:
        if discover() != (ws_url, epoch):
            raise PreflightError("BROWSER_IDENTITY_MISMATCH")
        call("Target.setDiscoverTargets", {"discover": True, "filter": PAGE_FILTER}, initial=True)
        targets = call("Target.getTargets", {"filter": PAGE_FILTER}, initial=True)
        selected = _single_page(targets.get("targetInfos"))
        if expected is not None and selected["targetId"] != expected.target_id:
            raise PreflightError("TARGET_BINDING_INVALID")
        _check_events(connection.take_events(), selected, initialization=True)
        info = call("Target.getTargetInfo", {"targetId": selected["targetId"]})
        _same_target(info.get("targetInfo"), selected)
        attachment = call("Target.attachToTarget", {"targetId": selected["targetId"], "flatten": True})
        session = _opaque_id(attachment.get("sessionId"))
        frame = _frame_snapshot(call("Page.getFrameTree", {}, attached=True), selected)

        # Recheck the OS listener/profile owner, then fresh identity on the exact
        # same browser socket and target session. No cached context.pages list.
        audit["launch_inspections"] += 1
        if _validate_launch(_inspect_launch(config, budget.remaining())) != launch:
            raise PreflightError("PROFILE_UNVERIFIED")
        final_targets = call("Target.getTargets", {"filter": PAGE_FILTER})
        _same_target(_single_page(final_targets.get("targetInfos")), selected)
        final_info = call("Target.getTargetInfo", {"targetId": selected["targetId"]})
        _same_target(final_info.get("targetInfo"), selected)
        if _frame_snapshot(call("Page.getFrameTree", {}, attached=True), selected) != frame:
            raise PreflightError("TARGET_UNVERIFIED")
        if discover() != (ws_url, epoch):
            raise PreflightError("BROWSER_IDENTITY_MISMATCH")
        # The detach acknowledgement also drains preceding events. Detach only
        # our own session; a failed detach cannot turn a failed probe into PASS.
        call("Target.detachFromTarget", {"sessionId": session}, detaching=True)
        detached = True
        budget.remaining()
        audit["target_id_sha256"] = hashlib.sha256(selected["targetId"].encode("ascii")).hexdigest()
        audit["browser_epoch"] = epoch
        audit["launch_record_sha256"] = launch["record_sha256"]
        return selected["targetId"], epoch, launch
    finally:
        if session is not None and not detached:
            # One cleanup attempt is allowed; it is not an identity/operation retry.
            # Never repeat a detach already dispatched by the successful path.
            if not audit["cdp_commands"] or audit["cdp_commands"][-1] != "Target.detachFromTarget":
                try:
                    call("Target.detachFromTarget", {"sessionId": session}, detaching=True)
                except Exception:
                    audit["cleanup_status"] = "SESSION_DETACH_FAILED_SOCKET_CLOSED"
        connection.close()


class PreflightOwner:
    """Trusted, single-consumer memory for L03; no file-based resurrection API."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_record: BindingRecord | None = None
        self._state = "UNBOUND"
        self._terminal_reason = "BINDING_REQUIRED"
        self._anchor_utc: datetime | None = None
        self._anchor_mono: float | None = None
        self._deadline_mono: float | None = None
        self._launch: dict[str, Any] | None = None

    @property
    def current_record(self) -> BindingRecord | None:
        return self._current_record

    @property
    def state(self) -> str:
        return self._state

    def preflight(self, config: PreflightConfig, *, policy_allowed: bool = False,
                  work_settled: bool = False) -> dict[str, Any]:
        return self._execute(config, new_record=True, policy_allowed=policy_allowed,
                             work_settled=work_settled)

    def revalidate(self, config: PreflightConfig, *, policy_allowed: bool = False) -> dict[str, Any]:
        return self._execute(config, new_record=False, policy_allowed=policy_allowed,
                             work_settled=True)

    def _check_lease(self) -> None:
        record = self._current_record
        if record is None or self._anchor_utc is None or self._anchor_mono is None or self._deadline_mono is None:
            raise PreflightError("BINDING_REQUIRED")
        now, mono = _utc_now(), _monotonic()
        if now.tzinfo is None or not math.isfinite(mono):
            raise PreflightError("CLOCK_DISCONTINUITY")
        drift = (now - self._anchor_utc).total_seconds() - (mono - self._anchor_mono)
        if mono < self._anchor_mono or abs(drift) > 5.0:
            raise PreflightError("CLOCK_DISCONTINUITY")
        if not _timestamp(record.issued_at_utc) <= now < _timestamp(record.expires_at_utc) or mono >= self._deadline_mono:
            raise PreflightError("BINDING_EXPIRED")

    def _execute(self, config: PreflightConfig, *, new_record: bool,
                 policy_allowed: bool, work_settled: bool) -> dict[str, Any]:
        audit: dict[str, Any] = {"mode": "EXPLICIT_PREFLIGHT" if new_record else "REVALIDATE",
                                "launch_inspections": 0, "discoveries": 0,
                                "websocket_connections": 0, "cdp_commands": [],
                                "binding_issued": 0, "content_commands": 0, "mutation_commands": 0}
        if not self._lock.acquire(blocking=False):
            return self._result("BLOCKED", "OWNER_BUSY", audit)
        entered = False
        try:
            if policy_allowed is not True:
                raise PreflightError("POLICY_DENIED")
            if work_settled is not True:
                raise PreflightError("WORK_NOT_SETTLED")
            if new_record:
                # An authorized, settled explicit replacement terminates the old
                # generation before validating any replacement input.
                self._current_record = None
                self._launch = None
                self._state = "PREFLIGHT"
                entered = True
            else:
                if self._state != "BOUND" or self._current_record is None:
                    raise PreflightError(self._terminal_reason)
                entered = True
            if not isinstance(config, PreflightConfig):
                raise PreflightError("PREFLIGHT_INPUT_INVALID")
            validate_endpoint(config.endpoint)
            if (type(config.timeout_seconds) not in (int, float) or
                    not math.isfinite(config.timeout_seconds) or not 0.1 <= config.timeout_seconds <= 30):
                raise PreflightError("PREFLIGHT_INPUT_INVALID")
            if any(type(value) is not str or not value or any(ord(c) < 32 for c in value)
                   for value in (config.chrome_path, config.user_data_dir, config.state_directory)):
                raise PreflightError("PREFLIGHT_INPUT_INVALID")
            if not new_record:
                self._check_lease()
            _check_runtime_dependency()
            target_id, epoch, launch = _probe(config, None if new_record else self._current_record, audit)
            if new_record:
                now, mono = _utc_now(), _monotonic()
                if now.tzinfo is None or now.utcoffset() != timedelta(0) or not math.isfinite(mono):
                    raise PreflightError("CLOCK_DISCONTINUITY")
                issued = now.replace(microsecond=0)
                expires = issued + timedelta(seconds=TTL_SECONDS)
                self._current_record = BindingRecord(
                    str(uuid.uuid4()), target_id, epoch, issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    expires.strftime("%Y-%m-%dT%H:%M:%SZ"))
                # Validate our serialization against the same strict external contract.
                parse_binding_record(self._current_record.to_bytes())
                self._anchor_utc, self._anchor_mono = now, mono
                self._deadline_mono = mono + (expires - now).total_seconds()
                self._launch = launch
                audit["binding_issued"] = 1
            else:
                if launch != self._launch:
                    raise PreflightError("PROFILE_UNVERIFIED")
                self._check_lease()
            self._state = "BOUND"
            self._terminal_reason = "BINDING_REVOKED"
            return self._result("PASS", "NONE", audit)
        except PreflightError as error:
            if entered:
                self._state = "UNBOUND" if new_record else (
                    "EXPIRED" if error.code == "BINDING_EXPIRED" else "INVALIDATED")
                self._terminal_reason = error.code
                if new_record:
                    self._current_record = None
                    audit["binding_issued"] = 0
            return self._result("BLOCKED", error.code, audit)
        except Exception:
            if entered:
                self._state = "UNBOUND" if new_record else "INVALIDATED"
                self._terminal_reason = "INTERNAL_ERROR"
                if new_record:
                    self._current_record = None
            return self._result("BLOCKED", "INTERNAL_ERROR", audit)
        finally:
            self._lock.release()

    def _result(self, status: str, reason: str, audit: dict[str, Any]) -> dict[str, Any]:
        record = self._current_record if status == "PASS" and self._state == "BOUND" else None
        return {"schema": "cdpd.preflight-result/v1", "status": status,
                "reason_code": reason, "state": self._state,
                "binding": record.to_dict() if record else None,
                "binding_sha256": hashlib.sha256(record.to_bytes()).hexdigest() if record else None,
                "audit": audit}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PreflightError("PREFLIGHT_INPUT_INVALID")


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description="Explicit CDPD identity-metadata preflight; no UI/DOM operations.")
    parser.add_argument("--chrome-path", required=True)
    parser.add_argument("--user-data-dir", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--allow-metadata-probe", action="store_true",
                        help="Explicitly authorize this one identity-metadata probe.")
    parser.add_argument("--work-settled", action="store_true",
                        help="Confirm no operation or unresolved dispatch remains at this boundary.")
    parser.add_argument("--binding-output", help="Write a new binding file exclusively; never overwrite an old record.")
    try:
        args = parser.parse_args(argv)
    except PreflightError:
        print(json.dumps({"schema": "cdpd.preflight-result/v1", "status": "BLOCKED",
                          "reason_code": "PREFLIGHT_INPUT_INVALID", "state": "UNBOUND",
                          "binding": None, "binding_sha256": None, "audit": {}}))
        return 2
    config = PreflightConfig(args.chrome_path, args.user_data_dir, args.state_directory,
                             args.endpoint, args.timeout_seconds)
    result = PreflightOwner().preflight(config, policy_allowed=args.allow_metadata_probe,
                                         work_settled=args.work_settled)
    if result["status"] == "PASS" and args.binding_output:
        try:
            record = parse_binding_record(_canonical_bytes(result["binding"]))
            with open(args.binding_output, "xb") as stream:
                stream.write(record.to_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            result = {"schema": "cdpd.preflight-result/v1", "status": "BLOCKED",
                      "reason_code": "OUTPUT_WRITE_FAILED", "binding": None,
                      "binding_sha256": None, "state": "UNBOUND", "audit": result["audit"]}
    print(json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
