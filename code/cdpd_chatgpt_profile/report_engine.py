"""L08 pure production derivation of the unchanged approved report-v1 core.

Rules follow approved L02 FIX003 report_policy/report_v1 and its design oracle;
no test/model module is imported. Both inputs always pass the accepted L06/L07
production Bundle front-end. Immutable filesystem output is a separate layer.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker, validators

from . import baseline as bl
from .baseline import ApprovalAuthority
from .bundle_validation import BundleInputError, canonical, strict_json, verify_report_bundle

ROOT = Path(__file__).resolve().parents[2]
_REPORT_SCHEMA_SHA256 = "b28b4b5bd5764131bfafaa3523da02f26233c41f0e5ac3eb638e767250c0e3e0"
REPORT_MAX_BYTES = 262144


def _obj(value: Any) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _verified(blob, unknown: set[str], policy: dict[str, Any]):
    try:
        return verify_report_bundle(blob)
    except BundleInputError as error:
        unknown.update(code for code in error.report_reasons if code in policy["unknown_conditions"])
        unknown.add("INTEGRITY_INVALID")
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        unknown.add("INTEGRITY_INVALID")
    return None, {}


def derive_report(
    source_blob: Mapping[str, bytes], baseline_blob: Mapping[str, bytes] | None = None,
    *, approval: ApprovalAuthority | None = None,
) -> dict[str, Any]:
    """Derive exact report-v1 fields; never accept asserted verdict/flags/policy.

    An invalid source can produce a legitimate UNKNOWN report. The invalid
    payload itself is never returned, sanitized into success, or modified.
    """
    policy = bl._policy()
    unknown: set[str] = set()
    findings = []
    required_failure = False
    drift = False
    source_sha, source = _verified(source_blob, unknown, policy)
    baseline_sha, base = (None, {})
    if baseline_blob is not None:
        baseline_sha, base = _verified(baseline_blob, unknown, policy)

    source_result = source.get("RESULT.json", {})
    source_profile = _obj(source_result.get("profile"))
    base_result = base.get("RESULT.json", {})
    base_profile = _obj(base_result.get("profile"))
    source_context = _obj(source_profile.get("report_context"))
    base_context = _obj(base_profile.get("report_context"))
    recipe = source_context.get("recipe", "UNDETERMINED")
    if type(recipe) is not str or recipe not in bl.RECIPES:
        recipe = "UNDETERMINED"
    capture_status = source_result.get("capture_status", "NOT_CAPTURED")
    if capture_status not in {"COMPLETE", "PARTIAL", "REJECTED", "NOT_CAPTURED"}:
        capture_status = "NOT_CAPTURED"
    if capture_status != "COMPLETE":
        unknown.add("CAPTURE_NOT_COMPLETE")
    identity = _obj(source_profile.get("identity_checks"))
    if not all(identity.get(key) is True for key in ("before", "after", "current", "keeper", "lease")):
        unknown.add("BINDING_NOT_VERIFIED")
    if (source_profile.get("policy_allowed") is not True
            or _obj(source_profile.get("sanitization")).get("passed") is not True):
        unknown.add("POLICY_NOT_ALLOWED")
    coverage = (source_profile.get("coverage_complete") is True
                and _obj(source_profile.get("history_coverage")).get("complete") is True)
    if not coverage:
        unknown.add("REQUIRED_COVERAGE_MISSING")
    if not bl._stages_complete(source_profile):
        unknown.add("REQUIRED_EVIDENCE_MISSING")
    relation = source_profile.get("observation_relation")
    if relation == "CURRENT_OBSERVATION":
        if source.get("TRIGGER.json", {}).get("kind") != "MANUAL":
            unknown.add("DOCUMENT_RELATION_UNKNOWN")
    elif relation != "SAME_DOCUMENT_VERIFIED":
        unknown.add("DOCUMENT_RELATION_UNKNOWN")

    context_fields = ("recipe", "locale_class", "viewport_class", "browser_profile")
    comparable = (recipe in bl.RECIPES and source_context.get("context_verified") is True
                  and base_context.get("context_verified") is True
                  and all(type(source_context.get(key)) is str
                          and source_context.get(key) == base_context.get(key)
                          for key in context_fields))
    if not comparable:
        unknown.add("CONTEXT_NOT_COMPARABLE")
    catalog_sha, normalization_sha, policy_sha = bl._policy_hashes(policy)
    if any(context.get("catalog_sha256") != catalog_sha
           or context.get("normalization_sha256") != normalization_sha
           or context.get("policy_sha256") != policy_sha
           for context in (source_context, base_context)):
        unknown.add("CATALOG_NOT_COMPARABLE")
    approved = (baseline_sha is not None and bl._approval_matches(approval, baseline_blob))
    source_probes, source_bad = bl._probe_map(source, policy)
    base_probes, base_bad = bl._probe_map(base, policy)
    if source_bad:
        unknown.add("REQUIRED_EVIDENCE_MISSING")
    if base_bad:
        approved = False

    for feature in policy["baseline_catalog"]:
        expected, required = bl._expected(policy, feature, recipe)
        source_index, observed = source_probes.get(feature, (None, None))
        base_index, base_observed = base_probes.get(feature, (None, None))
        finding = {
            "feature_id": feature, "required": required,
            "verdict": "NOT_APPLICABLE", "reason_code": "NOT_APPLICABLE",
            "evidence_pointer": None, "baseline_pointer": None, "coverage_pointer": None,
            "observed": None, "expected": None,
        }
        if expected is not None:
            finding.update(
                expected=expected, observed=observed,
                evidence_pointer=(f"STRUCTURE.json#/profile/probes/{source_index}"
                                  if source_index is not None else None),
                baseline_pointer=(f"STRUCTURE.json#/profile/probes/{base_index}"
                                  if base_index is not None else None),
                coverage_pointer=("RESULT.json#/profile/coverage_complete"
                                  if source_sha is not None else None),
            )
            if (base_observed is None or base_observed["probe_complete"] is not True
                    or (required and not bl._satisfies(base_observed, expected))):
                approved = False
            if observed is None or observed["probe_complete"] is not True:
                finding.update(verdict="UNKNOWN", reason_code="PROBE_INCOMPLETE")
                unknown.add("REQUIRED_PROBE_INCOMPLETE")
            elif required and not bl._satisfies(observed, expected):
                finding.update(verdict="BREAKING", reason_code="REQUIRED_FUNCTION_VIOLATED")
                required_failure = True
            elif not required and base_observed is not None and observed != base_observed:
                finding.update(verdict="REVIEW", reason_code="OPTIONAL_FUNCTION_DRIFT")
                drift = True
            else:
                finding.update(verdict="PASS", reason_code="CONTRACT_SATISFIED")
        findings.append(finding)
    if not approved:
        unknown.add("BASELINE_NOT_APPROVED")

    for feature in policy["baseline_catalog"]:
        expected, _ = bl._expected(policy, feature, recipe)
        if expected is None:
            continue
        try:
            after = bl._checked_shape(source["STRUCTURE.json"]["profile"]["feature_shapes"][feature])
            before = bl._checked_shape(base["STRUCTURE.json"]["profile"]["feature_shapes"][feature])
            if after != before:
                drift = True
        except (KeyError, TypeError, ValueError):
            unknown.add("REQUIRED_EVIDENCE_MISSING")

    status = "UNKNOWN" if unknown else "BREAKING" if required_failure else "REVIEW" if drift else "PASS"
    reasons = [code for code in policy["unknown_conditions"] if code in unknown]
    if required_failure:
        reasons.append("REQUIRED_FUNCTION_VIOLATED")
    if drift:
        reasons.append("SIGNIFICANT_DRIFT")
    action = {"PASS": "NONE", "BREAKING": "REVIEW_FEATURE_CONTRACT",
              "REVIEW": "REVIEW_NONBREAKING_DRIFT", "UNKNOWN": "COLLECT_OR_VERIFY_EVIDENCE"}
    return {
        "schema": "cdpd.chatgpt-report/v1", "scope": policy["scope"],
        "assessment_status": status, "capture_status": capture_status,
        "source_manifest_sha256": source_sha, "baseline_manifest_sha256": baseline_sha,
        "baseline_approved": approved, "context_comparable": comparable,
        "coverage_complete": coverage, "reason_codes": reasons, "findings": findings,
        "automatic_action": "NONE", "context_recipe": recipe,
        "catalog_sha256": catalog_sha if source_context.get("catalog_sha256") == catalog_sha else None,
        "normalization_sha256": (normalization_sha
                                 if source_context.get("normalization_sha256") == normalization_sha else None),
        "not_proven": list(policy["not_proven"]), "hypothesis_code": "NOT_EVALUATED",
        "confidence": "NOT_ESTIMATED", "recommended_action": action[status],
    }


def canonical_report_bytes(
    source_blob: Mapping[str, bytes], baseline_blob: Mapping[str, bytes] | None = None,
    *, approval: ApprovalAuthority | None = None,
) -> bytes:
    """Canonical UTF-8 output from immutable inputs, with no wall-clock field."""
    raw = canonical(derive_report(source_blob, baseline_blob, approval=approval))
    if len(raw) > REPORT_MAX_BYTES:
        raise ValueError("REPORT_TOO_LARGE")
    return raw


def validate_report_bytes(
    raw: bytes, source_blob: Mapping[str, bytes], baseline_blob: Mapping[str, bytes] | None = None,
    *, approval: ApprovalAuthority | None = None,
) -> bool:
    """Strict receiver: reject false declarations, never repair them to UNKNOWN."""
    try:
        value = strict_json(raw, REPORT_MAX_BYTES)
        schema_raw = (ROOT / "contracts/report_v1.schema.json").read_bytes()
        if hashlib.sha256(schema_raw).hexdigest() != _REPORT_SCHEMA_SHA256:
            return False
        schema = strict_json(schema_raw, REPORT_MAX_BYTES)
        checker = Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, v: type(v) is int)
        cls = validators.extend(Draft202012Validator, type_checker=checker)
        if not cls(schema, format_checker=FormatChecker()).is_valid(value):
            return False
        derived = derive_report(source_blob, baseline_blob, approval=approval)
        return canonical(value) == canonical(derived)
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return False
