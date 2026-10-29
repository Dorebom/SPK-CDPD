"""L08 offline report envelope, deterministic Markdown and immutable outputs.

The unchanged approved report-v1 core lives under ``report``. L08 traceability
is a separately versioned envelope; no field is added to the old closed schema.
No network, browser, collector, Supervisor or Web operation is invoked here.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator

from .baseline import ApprovalAuthority, baseline_candidate_metadata, normalized_profile_sha256
from .bundle_validation import BundleInputError, canonical, strict_json, verify_report_bundle
from .l08_artifact_store import ArtifactError, ArtifactStore, read_artifact, sha256
from .report_engine import derive_report, validate_report_bytes

ROOT = Path(__file__).resolve().parents[2]
_CAPTURE_ID = re.compile(r"C-[0-9a-f]{32}\Z")
_REASON_CHECKS = {
    "INTEGRITY_INVALID": "BUNDLE_INTEGRITY",
    "CAPTURE_NOT_COMPLETE": "CAPTURE_COMPLETENESS",
    "POLICY_NOT_ALLOWED": "CAPTURE_POLICY",
    "BINDING_NOT_VERIFIED": "BINDING_VERIFICATION",
    "REQUIRED_COVERAGE_MISSING": "COVERAGE_AND_HISTORY",
    "BASELINE_NOT_APPROVED": "BASELINE_APPROVAL",
    "CONTEXT_NOT_COMPARABLE": "CONTEXT_COMPARABILITY",
    "CATALOG_NOT_COMPARABLE": "CATALOG_AND_POLICY",
    "DOCUMENT_RELATION_UNKNOWN": "DOCUMENT_RELATION",
    "REQUIRED_PROBE_INCOMPLETE": "APPLICABLE_PROBE_COMPLETENESS",
    "REQUIRED_EVIDENCE_MISSING": "REQUIRED_EVIDENCE",
    "REQUIRED_FUNCTION_VIOLATED": "REQUIRED_FUNCTION",
    "SIGNIFICANT_DRIFT": "NONBREAKING_DRIFT",
}


def _boundary_reason(reason: str) -> str:
    return "BASELINE_UNAPPROVED" if reason == "BASELINE_NOT_APPROVED" else reason


def derive_l08_report(source_blob: dict[str, bytes], baseline_blob: dict[str, bytes] | None = None,
                      *, approval: ApprovalAuthority | None = None) -> dict[str, Any]:
    core = derive_report(source_blob, baseline_blob, approval=approval)
    capture_id = None
    source_commit = None
    try:
        _, source = verify_report_bundle(source_blob)
        value = source["RESULT.json"]["profile"]["capture_id"]
        if type(value) is str and _CAPTURE_ID.fullmatch(value):
            capture_id = value
        source_commit = sha256(source_blob["COMMIT.json"])
    except (BundleInputError, KeyError, TypeError, ValueError):
        pass  # Invalid raw input/IDs/exception text must never be rendered.
    normalized = []
    for blob in (baseline_blob, source_blob):
        try:
            normalized.append(normalized_profile_sha256(blob) if blob is not None else None)
        except (ValueError, TypeError, KeyError):
            normalized.append(None)
    checks = [{"check_id": _REASON_CHECKS[reason], "reason_code": _boundary_reason(reason),
               "verdict": "REVIEW" if reason == "SIGNIFICANT_DRIFT" else
                   "BREAKING" if reason == "REQUIRED_FUNCTION_VIOLATED" else "UNKNOWN"}
              for reason in core["reason_codes"]]
    return {"schema": "cdpd.l08.report/v1", "ticket_id": "CDPD-L08",
            "observation_mode": "OFFLINE_REPLAY", "capture_id": capture_id,
            "hash_scope": "BUNDLE_MANIFEST_SHA256",
            "before_sha256": core["baseline_manifest_sha256"],
            "after_sha256": core["source_manifest_sha256"],
            "source_commit_sha256": source_commit,
            "before_normalized_sha256": normalized[0], "after_normalized_sha256": normalized[1],
            "baseline_status": "APPROVED" if core["baseline_approved"] else "BASELINE_UNAPPROVED",
            "failed_checks": checks, "report": core}


def validate_l08_report_bytes(raw: bytes, source_blob: dict[str, bytes],
                              baseline_blob: dict[str, bytes] | None = None, *,
                              approval: ApprovalAuthority | None = None) -> bool:
    try:
        value = strict_json(raw, 262144)
        schema = strict_json((ROOT / "contracts/l08_report_v1.schema.json").read_bytes(), 262144)
        if not Draft202012Validator(schema).is_valid(value):
            return False
        if not validate_report_bytes(canonical(value["report"]), source_blob, baseline_blob,
                                     approval=approval):
            return False
        return canonical(value) == canonical(derive_l08_report(source_blob, baseline_blob, approval=approval))
    except (ValueError, TypeError, KeyError, OSError, RecursionError, OverflowError):
        return False


def render_markdown(value: dict[str, Any]) -> bytes:
    """Render only the closed-schema fixed vocabulary and hashed traceability."""
    schema = strict_json((ROOT / "contracts/l08_report_v1.schema.json").read_bytes(), 262144)
    if not Draft202012Validator(schema).is_valid(value):
        raise ArtifactError("ARTIFACT_INVALID")
    core = value["report"]
    show = lambda v: str(v) if v is not None else "UNKNOWN"
    lines = ["# CDPD L08 structural-feature report", "",
        f"**Assessment: {core['assessment_status']}**", "",
        "Scope: STATIC_UI_CONTRACT_COMPATIBILITY_NOT_BUSINESS_SUCCESS.", "",
        "This is an offline replay of committed evidence, not a new browser observation.", "",
        f"- capture_id: `{show(value['capture_id'])}`",
        f"- Capture status: `{core['capture_status']}`",
        f"- Baseline status: `{value['baseline_status']}`",
        f"- Context recipe: `{core['context_recipe']}`",
        f"- Before Manifest SHA-256: `{show(value['before_sha256'])}`",
        f"- After Manifest SHA-256: `{show(value['after_sha256'])}`",
        f"- Before normalized profile SHA-256: `{show(value['before_normalized_sha256'])}`",
        f"- After normalized profile SHA-256: `{show(value['after_normalized_sha256'])}`",
        f"- Source COMMIT SHA-256: `{show(value['source_commit_sha256'])}`", "",
        "## Failed or review-required checks", "",
        "| Check | Verdict | Reason |", "|---|---|---|"]
    for row in value["failed_checks"]:
        lines.append(f"| {row['check_id']} | {row['verdict']} | {row['reason_code']} |")
    if not value["failed_checks"]:
        lines.append("| NONE | PASS | CONTRACT_SATISFIED |")
    lines += ["", "## Feature findings", "",
        "| Feature | Required | Verdict | Observed match/visible/enabled/complete | Expected min/max/visible/enabled | Source / Baseline pointer |",
        "|---|---|---|---|---|---|"]
    for row in core["findings"]:
        obs, expected = row["observed"], row["expected"]
        observed = "/".join(str(obs[k]) for k in ("match_count", "visible_count", "enabled_count", "probe_complete")) if obs else "N/A"
        expectation = "/".join(show(expected[k]) for k in ("match_min", "match_max", "visible_min", "enabled_min")) if expected else "N/A"
        lines.append(f"| {row['feature_id']} | {row['required']} | {row['verdict']} | {observed} | {expectation} | {show(row['evidence_pointer'])} / {show(row['baseline_pointer'])} |")
    lines += ["", "## Limits", "",
        "- Automatic action: NONE. Baseline adoption is a separate Human decision.",
        "- Hypothesis: NOT_EVALUATED; confidence: NOT_ESTIMATED. Causality is not inferred.",
        "- Not proven: SEND_SUCCESS, RESPONSE_CONTENT_CORRECT, RESPONSE_CAUSAL_IDENTITY, DOWNLOAD_CORRECT, WORK_SUCCESS.",
        "- UNKNOWN takes precedence over observed BREAKING findings when prerequisites are incomplete.",
        f"- Recommended action: {core['recommended_action']}.", ""]
    return "\n".join(lines).encode("utf-8")


def report_payloads(source_blob: dict[str, bytes], baseline_blob: dict[str, bytes] | None = None,
                    *, approval: ApprovalAuthority | None = None) -> dict[str, bytes]:
    value = derive_l08_report(source_blob, baseline_blob, approval=approval)
    raw = canonical(value)
    if not validate_l08_report_bytes(raw, source_blob, baseline_blob, approval=approval):
        raise ArtifactError("ARTIFACT_INVALID")
    return {"report.json": raw, "report.md": render_markdown(value)}


def write_report_package(output_root: str | Path, source_blob: dict[str, bytes],
                         baseline_blob: dict[str, bytes] | None = None, *,
                         approval: ApprovalAuthority | None = None,
                         windows_storage_authorizer=None, fault_hook=None) -> Path:
    payloads = report_payloads(source_blob, baseline_blob, approval=approval)
    return ArtifactStore(output_root, kind="REPORT", windows_storage_authorizer=windows_storage_authorizer,
                         fault_hook=fault_hook).write(payloads)


def verify_report_package(path: str | Path, source_blob: dict[str, bytes],
                          baseline_blob: dict[str, bytes] | None = None, *,
                          approval: ApprovalAuthority | None = None) -> bool:
    try:
        observed = read_artifact(path)
        expected = report_payloads(source_blob, baseline_blob, approval=approval)
        return (set(observed) == set(expected) | {"MANIFEST.json", "COMMIT.json"}
                and all(observed[k] == v for k, v in expected.items())
                and strict_json(observed["MANIFEST.json"])["kind"] == "REPORT")
    except (OSError, ValueError, TypeError, KeyError):
        return False


def write_baseline_candidate(output_root: str | Path, source_blob: dict[str, bytes], *,
                              windows_storage_authorizer=None) -> Path:
    # Candidate creation verifies exact source bytes but never writes an
    # approval decision, trust pin, active pointer or adopted-baseline location.
    metadata = baseline_candidate_metadata(source_blob)
    payloads = {"baseline-candidate.json": canonical(metadata)}
    capture_id = metadata["capture_id"]
    payloads.update({"source_bundle/" + capture_id + "/" + name: raw for name, raw in source_blob.items()})
    return ArtifactStore(output_root, kind="BASELINE_CANDIDATE",
                         windows_storage_authorizer=windows_storage_authorizer).write(payloads)
