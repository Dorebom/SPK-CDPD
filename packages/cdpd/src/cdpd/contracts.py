"""Versioned, application-neutral contracts. Correlation values are caller-owned."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from types import MappingProxyType
from urllib.parse import urlsplit
import math

SCHEMA_EVENT = "cdpd.event/v1"
SCHEMA_CAPTURE = "cdpd.capture-request/v1"
KINDS = frozenset({"ACTION_STARTED", "ACTION_FINISHED", "ACTION_FAILED", "OBSERVATION_FAILED", "HEARTBEAT", "MANUAL"})
FAILURES = frozenset({"NONE", "TARGET_MISSING", "TARGET_AMBIGUOUS", "NOT_INTERACTABLE", "TIMEOUT", "UNKNOWN", "DISCONNECTED", "RECONCILE_REQUIRED", "POSTCONDITION_FAILED"})


def _text(value: Any, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name}: nonempty string up to {limit} characters required")
    return value  # Never trim, normalize, hash or reinterpret a caller correlation ID.


def exact_keys(data: Mapping[str, Any], required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(data, dict) or set(data) - required - optional or required - set(data):
        raise ValueError("invalid object fields")


def origin(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("http(s) origin required")
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    suffix = f":{port}" if port and port != (443 if p.scheme == "https" else 80) else ""
    return f"{p.scheme}://{host}{suffix}"


@dataclass(frozen=True)
class DiagnosticEvent:
    producer_id: str
    producer_instance_id: str
    operation_id: str
    kind: str
    failure_code: str = "NONE"
    correlation: Mapping[str, str] = field(default_factory=dict)
    schema: str = SCHEMA_EVENT

    def __post_init__(self) -> None:
        for name in ("producer_id", "producer_instance_id", "operation_id"):
            _text(getattr(self, name), name)
        if self.schema != SCHEMA_EVENT or self.kind not in KINDS or self.failure_code not in FAILURES:
            raise ValueError("unsupported schema/kind/failure_code")
        if not isinstance(self.correlation, Mapping) or len(self.correlation) > 16:
            raise ValueError("correlation: at most 16 opaque ID entries")
        copied = dict(self.correlation)
        for key, value in copied.items():
            _text(key, "correlation key", 64)
            _text(value, "correlation value", 1024)
        # defensive copy; serialization also returns a copy
        object.__setattr__(self, "correlation", MappingProxyType(copied))

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "producer_id": self.producer_id,
                "producer_instance_id": self.producer_instance_id, "operation_id": self.operation_id,
                "kind": self.kind, "failure_code": self.failure_code, "correlation": dict(self.correlation)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiagnosticEvent:
        exact_keys(data, {"schema", "producer_id", "producer_instance_id", "operation_id", "kind"}, {"failure_code", "correlation"})
        return cls(**data)


@dataclass(frozen=True)
class TargetBinding:
    endpoint: str
    target_id: str
    expected_origin: str
    expected_browser_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.endpoint, "endpoint", 1024)
        _text(self.target_id, "target_id")
        if origin(self.expected_origin) != self.expected_origin:
            raise ValueError("expected_origin must be normalized origin, without path")
        if self.expected_browser_id is not None:
            _text(self.expected_browser_id, "expected_browser_id")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetBinding:
        exact_keys(data, {"endpoint", "target_id", "expected_origin"}, {"expected_browser_id"})
        return cls(**data)


@dataclass(frozen=True)
class CaptureRequest:
    event: DiagnosticEvent
    target: TargetBinding
    timeout_seconds: float = 10.0
    max_nodes: int = 1000
    schema: str = SCHEMA_CAPTURE

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_CAPTURE or not isinstance(self.event, DiagnosticEvent) or not isinstance(self.target, TargetBinding):
            raise ValueError("invalid capture request")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 20:
            raise ValueError("timeout_seconds must be finite, 0.1..20")
        if isinstance(self.max_nodes, bool) or not isinstance(self.max_nodes, int) or not 1 <= self.max_nodes <= 2000:
            raise ValueError("max_nodes must be 1..2000")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "event": self.event.to_dict(), "target": asdict(self.target),
                "timeout_seconds": self.timeout_seconds, "max_nodes": self.max_nodes}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureRequest:
        exact_keys(data, {"schema", "event", "target"}, {"timeout_seconds", "max_nodes"})
        return cls(event=DiagnosticEvent.from_dict(data["event"]), target=TargetBinding.from_dict(data["target"]),
                   schema=data["schema"], timeout_seconds=data.get("timeout_seconds", 10.0), max_nodes=data.get("max_nodes", 1000))
