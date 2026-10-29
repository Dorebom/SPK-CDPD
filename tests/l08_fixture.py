"""L08 SYNTHETIC fixtures only; no Human approval or browser observation.

The accepted l06_profile_complete.json remains unchanged. This helper copies it
and authors independent L08 variants. Computing approval digests here is an
explicit synthetic trust fixture; production code never imports this module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cdpd_chatgpt_profile.baseline import baseline_candidate_metadata, verify_approval_record
from cdpd_chatgpt_profile.bundle_validation import canonical

ROOT = Path(__file__).resolve().parents[1]


def valid_payload(recipe: str = "IDLE_EMPTY") -> dict:
    payload = json.loads((ROOT / "tests/fixtures/l06_profile_complete.json").read_bytes())
    profile = payload["RESULT.json"]["profile"]
    profile["report_context"]["recipe"] = recipe
    profile["trigger_envelope"]["profile"]["ui_context"] = recipe
    if recipe == "IDLE_INPUT_PRESENT":
        next(p for p in payload["STRUCTURE.json"]["profile"]["probes"]
             if p["feature_id"] == "composer.send")["enabled_count"] = 1
    return payload


def pack_payload(payload: dict) -> dict[str, bytes]:
    """Keep hashes honest even when intentionally authoring invalid schema data."""
    raw = {name: canonical(value) for name, value in sorted(payload.items())}
    raw["MANIFEST.json"] = canonical({
        "schema": "cdpd.bundle-manifest/v1",
        "capture_id": payload["RESULT.json"]["profile"]["capture_id"],
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in raw.items()],
    })
    raw["COMMIT.json"] = canonical({"schema": "cdpd.commit/v1",
        "manifest_sha256": hashlib.sha256(raw["MANIFEST.json"]).hexdigest()})
    return raw


def valid_blob(recipe: str = "IDLE_EMPTY") -> dict[str, bytes]:
    return pack_payload(valid_payload(recipe))


def approval_record_for(blob: dict[str, bytes]) -> dict:
    """Explicit SYNTHETIC approval record, never an actual Human adoption."""
    metadata = baseline_candidate_metadata(blob)
    return {"schema": "cdpd.chatgpt-baseline-approval/v1", "decision": "APPROVE",
            "approver_id": "SYNTHETIC_TEST_ONLY_NOT_HUMAN",
            "approved_utc": "2026-10-29T00:00:00Z", "previous_manifest_sha256": None,
            "source_manifest_sha256": metadata["source_manifest_sha256"],
            "normalized_profile_sha256": metadata["normalized_profile_sha256"],
            **metadata["report_context"]}


def approval_for(blob: dict[str, bytes]):
    """Return raw bytes, fixture-selected trust hash and typed test authority."""
    raw = canonical(approval_record_for(blob))
    trusted_sha256 = hashlib.sha256(raw).hexdigest()
    authority = verify_approval_record(raw, trusted_approval_sha256=trusted_sha256, baseline_blob=blob)
    assert authority is not None, "SYNTHETIC_APPROVAL_FIXTURE_INVALID"
    return raw, trusted_sha256, authority


def history_partial_payload() -> dict:
    payload = valid_payload()
    result = payload["RESULT.json"]
    result.update(capture_status="PARTIAL", failure_code="HISTORY_INCOMPLETE")
    result["profile"]["history_coverage"].update(complete=False, loss_count_exact=False)
    result["profile"]["trigger_envelope"]["profile"]["loss_count_exact"] = False
    return payload


def rejected_payload(reason=None) -> dict:
    payload = valid_payload()
    payload.pop("STRUCTURE.json")
    result = payload["RESULT.json"]
    result.update(capture_status="REJECTED", failure_code="POLICY_DENIED")
    profile = result["profile"]
    profile.update(policy_allowed=False, coverage_complete=False, worker_termination="NOT_STARTED")
    profile["timing"]["capture_started_at_utc"] = None
    for name in ("S1", "S2"):
        profile["stages"][name] = {"status": "NOT_RUN", "reason_code": reason, "duration_ms": 0}
    return payload
