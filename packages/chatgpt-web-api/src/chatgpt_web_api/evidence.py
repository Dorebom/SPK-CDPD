from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import ArtifactDownloadResult, WebDebugEvidence, WebOperationEvidenceRecord


def evidence_to_dict(record: WebOperationEvidenceRecord) -> dict[str, Any]:
    """Serialize Web-operation evidence without browser credentials or signed URLs."""

    payload = _sanitize_value(asdict(record))
    failure = payload.get("failure")
    if failure is not None:
        payload["failure"] = record.failure.value if record.failure else None
    response = payload.get("response_identity")
    if response is not None and record.response_identity is not None:
        response["role"] = record.response_identity.role.value
    artifact = payload.get("artifact")
    if artifact is not None and record.artifact is not None:
        artifact["response_identity"]["role"] = record.artifact.response_identity.role.value
    return payload



def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        # Web evidence must not persist reusable signed/authenticated URLs.
        value = re.sub(r"(https?://[^\s?#]+)\?[^\s#]+", r"\1?<redacted>", value)
        value = re.sub(
            r"(?i)\b(sig|signature|token|auth|credential)=([^&\s]+)",
            r"\1=<redacted>",
            value,
        )
        return value
    return value

def debug_evidence_to_dict(record: WebDebugEvidence | None) -> dict[str, Any]:
    """Return a sanitized structured projection of causal Web diagnostics."""
    if record is None:
        return {}
    return _sanitize_value({
        "operation": record.operation,
        "observations": list(record.observations),
        "details": dict(record.details),
    })


def evidence_to_json(record: WebOperationEvidenceRecord) -> str:
    return json.dumps(evidence_to_dict(record), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_web_operation_evidence(record: WebOperationEvidenceRecord, path: str | Path) -> Path:
    """Atomically persist immutable Web-operation evidence.

    Existing evidence is never silently overwritten.  This is Web-operation
    evidence, not the SHIRABE Domain Evidence Package introduced later in M1.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Evidence already exists: {target}")

    data = evidence_to_json(record).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise FileExistsError(f"Evidence already exists: {target}")
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def evidence_from_download_result(result: ArtifactDownloadResult) -> WebOperationEvidenceRecord | None:
    return result.operation_evidence
