"""L08 immutable Baseline metadata and an explicit local approval-read boundary.

No function adopts a Baseline or writes a pointer. A trusted approval digest is
an independently supplied operator trust input, not a digest this module treats
as authority merely because it can compute it. Same-user file fabrication and
Human identity authentication are outside SHA-256's assurance.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .bundle_validation import canonical, strict_json, verify_report_bundle

ROOT = Path(__file__).resolve().parents[2]
_POLICY_FILE_SHA256 = "618983645a8cba5f120c3a2f6df8d147344a956fa88736620f9eec484a05504b"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_APPROVAL_TOKEN = object()
CONTEXT_FIELDS = (
    "recipe", "catalog_sha256", "normalization_sha256", "policy_sha256",
    "locale_class", "viewport_class", "browser_profile",
)
RECIPES = ("IDLE_EMPTY", "IDLE_INPUT_PRESENT", "GENERATING", "RESPONSE_READY")


class BaselineInputError(ValueError):
    """A fixed code only; never interpolate an untrusted value or exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _policy() -> dict[str, Any]:
    """Load the exact accepted policy, with no caller policy override."""
    try:
        raw = (ROOT / "contracts/report_policy.json").read_bytes()
        if _digest(raw) != _POLICY_FILE_SHA256:
            raise BaselineInputError("REPORT_POLICY_INVALID")
        return strict_json(raw, 32768)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise BaselineInputError("REPORT_POLICY_INVALID") from None


def _policy_hashes(policy: dict[str, Any]) -> tuple[str, str, str]:
    return (_digest(canonical(policy["probe_contracts"])),
            _digest(canonical(policy["normalization_contract"])),
            _digest(canonical(policy)))


def _expected(policy: dict[str, Any], feature: str, recipe: str):
    spec = policy["probe_contracts"][feature]
    if recipe not in spec["recipes"]:
        return None, False
    expectation = dict(spec["expected"])
    expectation.update(spec.get("recipe_overrides", {}).get(recipe, {}))
    return expectation, spec["required"]


def _satisfies(observed: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (observed["match_count"] >= expected["match_min"]
            and (expected["match_max"] is None
                 or observed["match_count"] <= expected["match_max"])
            and observed["visible_count"] >= expected["visible_min"]
            and observed["enabled_count"] >= expected["enabled_min"])


def _checked_probe(probe: dict[str, Any]) -> dict[str, Any]:
    if type(probe) is not dict or set(probe) != {
        "feature_id", "match_count", "visible_count", "enabled_count", "probe_complete"
    }:
        raise BaselineInputError("BASELINE_PROFILE_INVALID")
    nums = [probe[k] for k in ("match_count", "visible_count", "enabled_count")]
    if (not all(type(n) is int and 0 <= n <= 2000 for n in nums)
            or nums[1] > nums[0] or nums[2] > nums[0]
            or type(probe["probe_complete"]) is not bool):
        raise BaselineInputError("BASELINE_PROFILE_INVALID")
    return {k: probe[k] for k in (
        "match_count", "visible_count", "enabled_count", "probe_complete")}


def _probe_map(payload: dict[str, Any], policy: dict[str, Any]):
    result: dict[str, Any] = {}
    try:
        probes = payload["STRUCTURE.json"]["profile"]["probes"]
        if type(probes) is not list or len(probes) > 50:
            raise BaselineInputError("BASELINE_PROFILE_INVALID")
        for index, probe in enumerate(probes):
            feature = probe["feature_id"]
            if feature not in policy["baseline_catalog"] or feature in result:
                raise BaselineInputError("BASELINE_PROFILE_INVALID")
            result[feature] = (index, _checked_probe(probe))
    except (KeyError, TypeError, ValueError):
        return result, True
    return result, False


def _checked_shape(shape: Any) -> list[dict[str, Any]]:
    if type(shape) is not list or len(shape) > 2000:
        raise BaselineInputError("BASELINE_PROFILE_INVALID")
    tags = {"div", "span", "input", "textarea", "button", "form", "section", "article"}
    roles = {None, "textbox", "button", "form", "region", "group"}
    result = []
    for index, node in enumerate(shape):
        if type(node) is not dict or set(node) != {"tag", "role", "parent", "shadow"}:
            raise BaselineInputError("BASELINE_PROFILE_INVALID")
        if (node["tag"] not in tags or node["role"] not in roles
                or type(node["shadow"]) is not bool):
            raise BaselineInputError("BASELINE_PROFILE_INVALID")
        parent = node["parent"]
        if parent is not None and (type(parent) is not int or not 0 <= parent < index):
            raise BaselineInputError("BASELINE_PROFILE_INVALID")
        result.append(dict(node))
    return result


def _stages_complete(profile: dict[str, Any]) -> bool:
    stages = profile.get("stages")
    if type(stages) is not dict:
        return False
    for name in ("S0", "S1", "S2", "S3"):
        stage = stages.get(name)
        if (type(stage) is not dict
                or set(stage) != {"status", "reason_code", "duration_ms"}
                or stage["status"] != "COMPLETE" or stage["reason_code"] is not None
                or type(stage["duration_ms"]) is not int or stage["duration_ms"] < 0):
            return False
    return True


def _normalized(payload: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Canonical applicable feature counts/shapes/context; no capture identity."""
    try:
        context = payload["RESULT.json"]["profile"]["report_context"]
        recipe = context["recipe"]
        if recipe not in RECIPES:
            raise BaselineInputError("BASELINE_PROFILE_INCOMPLETE")
        probes, bad = _probe_map(payload, policy)
        if bad:
            raise BaselineInputError("BASELINE_PROFILE_INCOMPLETE")
        features = []
        for feature in policy["baseline_catalog"]:
            expected, _ = _expected(policy, feature, recipe)
            if expected is None:
                continue
            if feature not in probes:
                raise BaselineInputError("BASELINE_PROFILE_INCOMPLETE")
            features.append({
                "feature_id": feature,
                "observed": probes[feature][1],
                "shape": _checked_shape(payload["STRUCTURE.json"]["profile"]["feature_shapes"][feature]),
            })
        return {"schema": "cdpd.chatgpt-baseline-normalized/v1",
                "context": {key: context[key] for key in CONTEXT_FIELDS},
                "features": features}
    except (KeyError, TypeError, ValueError):
        raise BaselineInputError("BASELINE_PROFILE_INCOMPLETE") from None


def normalized_profile_sha256(baseline_blob: Mapping[str, bytes]) -> str:
    """Always run the accepted full front-end; no prevalidated bypass exists."""
    _, payload = verify_report_bundle(baseline_blob)
    return _digest(canonical(_normalized(payload, _policy())))


def _eligible(payload: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Human intent never makes an intrinsically invalid Baseline eligible."""
    try:
        result = payload["RESULT.json"]
        profile = result["profile"]
        context = profile["report_context"]
        catalog, normalization, policy_sha = _policy_hashes(policy)
        if (result["capture_status"] != "COMPLETE"
                or context["recipe"] not in RECIPES or context["context_verified"] is not True
                or context["catalog_sha256"] != catalog
                or context["normalization_sha256"] != normalization
                or context["policy_sha256"] != policy_sha
                or profile["coverage_complete"] is not True
                or profile["history_coverage"]["complete"] is not True
                or profile["policy_allowed"] is not True
                or profile["sanitization"]["passed"] is not True
                or not all(profile["identity_checks"][key] is True
                           for key in ("before", "after", "current", "keeper", "lease"))
                or not _stages_complete(profile)):
            return False
        relation = profile["observation_relation"]
        if relation == "CURRENT_OBSERVATION":
            if payload["TRIGGER.json"]["kind"] != "MANUAL":
                return False
        elif relation != "SAME_DOCUMENT_VERIFIED":
            return False
        probes, bad = _probe_map(payload, policy)
        if bad:
            return False
        for feature in policy["baseline_catalog"]:
            expected, required = _expected(policy, feature, context["recipe"])
            if expected is None:
                continue
            observed = probes.get(feature, (None, None))[1]
            if (observed is None or observed["probe_complete"] is not True
                    or (required and not _satisfies(observed, expected))):
                return False
        _normalized(payload, policy)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def baseline_candidate_metadata(blob: Mapping[str, bytes]) -> dict[str, Any]:
    """Describe a valid candidate without writing an approval or active pointer."""
    manifest_sha, payload = verify_report_bundle(blob)
    policy = _policy()
    if not _eligible(payload, policy):
        raise BaselineInputError("BASELINE_INELIGIBLE")
    profile = payload["RESULT.json"]["profile"]
    return {"schema": "cdpd.chatgpt-baseline-candidate/v1",
            "adoption_status": "CANDIDATE_UNAPPROVED",
            "baseline_status": "BASELINE_UNAPPROVED",
            "capture_id": profile["capture_id"],
            "source_manifest_sha256": manifest_sha,
            "normalized_profile_sha256": _digest(canonical(_normalized(payload, policy))),
            "report_context": {key: profile["report_context"][key] for key in CONTEXT_FIELDS},
            "automatic_approval": False}


@dataclass(frozen=True, slots=True, init=False)
class ApprovalAuthority:
    """Validated local approval bytes, never an external ``approved=True`` dict.

    This immutable object is a carrier for a trust decision, not an unforgeable
    Python capability. Every consumption rechecks the raw record and all binds.
    """

    raw_record: bytes
    trusted_approval_sha256: str
    source_manifest_sha256: str
    normalized_profile_sha256: str

    def __init__(self, raw_record: bytes, trusted_approval_sha256: str,
                 source_manifest_sha256: str, normalized_profile_sha256: str,
                 *, _token: object = None) -> None:
        if _token is not _APPROVAL_TOKEN:
            raise TypeError("TRUSTED_APPROVAL_BOUNDARY_REQUIRED")
        object.__setattr__(self, "raw_record", raw_record)
        object.__setattr__(self, "trusted_approval_sha256", trusted_approval_sha256)
        object.__setattr__(self, "source_manifest_sha256", source_manifest_sha256)
        object.__setattr__(self, "normalized_profile_sha256", normalized_profile_sha256)


def verify_approval_record(
    raw: bytes | None, *, trusted_approval_sha256: str | None,
    baseline_blob: Mapping[str, bytes] | None,
) -> ApprovalAuthority | None:
    """Read/verify explicit Human-adoption evidence; missing trust means no approval.

    The independently selected trusted digest must not be computed from the
    record by a scheduler or report caller as an automatic approval shortcut.
    This function does not authenticate Human identity or change any artifact.
    """
    try:
        if (type(raw) is not bytes or baseline_blob is None
                or type(trusted_approval_sha256) is not str
                or _SHA256_RE.fullmatch(trusted_approval_sha256) is None
                or not hmac.compare_digest(_digest(raw), trusted_approval_sha256)):
            return None
        record = strict_json(raw, 32768)
        schema = strict_json((ROOT / "contracts/baseline_approval_v1.schema.json").read_bytes(), 32768)
        if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(record):
            return None
        manifest_sha, payload = verify_report_bundle(baseline_blob)
        policy = _policy()
        if not _eligible(payload, policy):
            return None
        context = payload["RESULT.json"]["profile"]["report_context"]
        normalized_sha = _digest(canonical(_normalized(payload, policy)))
        if (record["source_manifest_sha256"] != manifest_sha
                or record["normalized_profile_sha256"] != normalized_sha
                or any(record[key] != context[key] for key in CONTEXT_FIELDS)):
            return None
        return ApprovalAuthority(raw, trusted_approval_sha256, manifest_sha, normalized_sha,
                                 _token=_APPROVAL_TOKEN)
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return None


def _approval_matches(approval: ApprovalAuthority | None, baseline_blob) -> bool:
    if type(approval) is not ApprovalAuthority:
        return False
    try:
        checked = verify_approval_record(
            approval.raw_record, trusted_approval_sha256=approval.trusted_approval_sha256,
            baseline_blob=baseline_blob)
        return (checked is not None
                and checked.source_manifest_sha256 == approval.source_manifest_sha256
                and checked.normalized_profile_sha256 == approval.normalized_profile_sha256)
    except (AttributeError, TypeError, ValueError):
        return False
