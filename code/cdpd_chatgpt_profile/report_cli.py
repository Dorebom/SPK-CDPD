"""Fixed offline CLI; no acquisition, automatic approval or arbitrary imports."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from .baseline import verify_approval_record
from .bundle_validation import verify_report_bundle
from .evidence_store import _read_blob, _read_regular, _safe_directory
from .l08_artifact_store import ArtifactError, absolute_path, sha256
from .report import derive_l08_report, verify_report_package, write_baseline_candidate, write_report_package


class InputError(ValueError):
    def __init__(self, code="CLI_INPUT_INVALID"):
        self.code = code if code in {"CLI_INPUT_INVALID", "EVIDENCE_READ_FAILED", "COMMIT_PIN_MISMATCH",
            "OUTPUT_OVERLAPS_INPUT", "APPROVAL_READ_FAILED", "WINDOWS_PROFILE_REQUIRED"} else "CLI_INPUT_INVALID"
        super().__init__(self.code)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes the rejected raw value/path. Never copy it
        # into a product log, even when a caller supplies conversation content.
        raise InputError()


def _pin(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise InputError()
    return value


def read_evidence_bundle(path, expected_commit_sha256=None):
    try:
        root = absolute_path(path)
        blob = _read_blob(root)
        if expected_commit_sha256 is not None and sha256(blob.get("COMMIT.json", b"")) != _pin(expected_commit_sha256):
            raise InputError("COMMIT_PIN_MISMATCH")
        # Internal invalid evidence is deliberately left for the mandatory
        # report front-end to classify UNKNOWN. No raw bytes are logged.
        try:
            _, payload = verify_report_bundle(blob)
        except ValueError:
            return blob
        if payload["RESULT.json"]["profile"]["capture_id"] != root.name:
            raise InputError("EVIDENCE_READ_FAILED")
        return blob
    except InputError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise InputError("EVIDENCE_READ_FAILED") from None


def _approval(path, pin, baseline):
    if path is None:
        if pin is not None:
            raise InputError()
        return None
    try:
        selected = absolute_path(path)
        _safe_directory(selected.parent, allow_missing=False)
        raw = _read_regular(selected, 16384)
        return verify_approval_record(raw, trusted_approval_sha256=pin, baseline_blob=baseline)
    except (OSError, ValueError, TypeError):
        raise InputError("APPROVAL_READ_FAILED") from None


def _disjoint_output(output, inputs):
    target = absolute_path(output)
    for item in inputs:
        if item is None:
            continue
        source = absolute_path(item)
        if source == target or source in target.parents:
            raise InputError("OUTPUT_OVERLAPS_INPUT")
    _safe_directory(target, allow_missing=True)
    return target


def storage_policy(output_root, profile_directory):
    if os.name != "nt":
        return None
    if profile_directory is None:
        raise InputError("WINDOWS_PROFILE_REQUIRED")
    from .supervisor_policy import WindowsStoragePolicy
    policy = WindowsStoragePolicy(output_root, profile_directory)
    policy.validate_startup()
    return policy


def run_offline(*, source_bundle, source_commit_sha256=None, baseline_bundle=None,
                baseline_commit_sha256=None, approval_record=None, trusted_approval_sha256=None,
                output_directory, profile_directory=None):
    source = read_evidence_bundle(source_bundle, source_commit_sha256)
    baseline = (read_evidence_bundle(baseline_bundle, baseline_commit_sha256)
                if baseline_bundle is not None else None)
    approval = _approval(approval_record, trusted_approval_sha256, baseline)
    output = _disjoint_output(output_directory, [source_bundle, baseline_bundle, approval_record])
    policy = storage_policy(output, profile_directory)
    value = derive_l08_report(source, baseline, approval=approval)
    artifact = write_report_package(output, source, baseline, approval=approval,
                                    windows_storage_authorizer=policy)
    if not verify_report_package(artifact, source, baseline, approval=approval):
        raise ArtifactError("POST_PUBLICATION_UNCONFIRMED")
    return {"schema": "cdpd.l08-cli-result/v1", "operation_status": "COMPLETED",
            "assessment_status": value["report"]["assessment_status"],
            "baseline_status": value["baseline_status"], "capture_id": value["capture_id"],
            "observation_mode": "OFFLINE_REPLAY", "artifact_id": artifact.name}


def main(argv=None):
    parser = SafeArgumentParser(description="L08 offline report / unapproved Baseline candidate; no browser actions.")
    parser.add_argument("command", choices=("report", "baseline-candidate", "verify"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-commit-sha256")
    parser.add_argument("--baseline")
    parser.add_argument("--baseline-commit-sha256")
    parser.add_argument("--approval-record")
    parser.add_argument("--trusted-approval-sha256")
    parser.add_argument("--output-root")
    parser.add_argument("--profile-directory")
    parser.add_argument("--report-package")
    try:
        args = parser.parse_args(argv)
        if args.command == "report":
            if args.output_root is None or args.report_package is not None:
                raise InputError()
            result = run_offline(source_bundle=args.source, source_commit_sha256=args.source_commit_sha256,
                baseline_bundle=args.baseline, baseline_commit_sha256=args.baseline_commit_sha256,
                approval_record=args.approval_record, trusted_approval_sha256=args.trusted_approval_sha256,
                output_directory=args.output_root, profile_directory=args.profile_directory)
            code = {"PASS": 0, "REVIEW": 10, "BREAKING": 11, "UNKNOWN": 12}[result["assessment_status"]]
        elif args.command == "baseline-candidate":
            if (args.output_root is None or args.baseline is not None or args.approval_record is not None
                    or args.trusted_approval_sha256 is not None or args.report_package is not None):
                raise InputError()
            source = read_evidence_bundle(args.source, args.source_commit_sha256)
            output = _disjoint_output(args.output_root, [args.source])
            artifact = write_baseline_candidate(output, source,
                windows_storage_authorizer=storage_policy(output, args.profile_directory))
            result = {"schema": "cdpd.l08-cli-result/v1", "operation_status": "COMPLETED",
                      "assessment_status": "UNKNOWN", "baseline_status": "BASELINE_UNAPPROVED",
                      "artifact_id": artifact.name, "automatic_approval": False}
            code = 0  # Candidate creation succeeded; assessment is still UNKNOWN.
        else:
            if args.report_package is None or args.output_root is not None:
                raise InputError()
            source = read_evidence_bundle(args.source, args.source_commit_sha256)
            baseline = read_evidence_bundle(args.baseline, args.baseline_commit_sha256) if args.baseline else None
            authority = _approval(args.approval_record, args.trusted_approval_sha256, baseline)
            valid = verify_report_package(args.report_package, source, baseline, approval=authority)
            result = {"schema": "cdpd.l08-cli-result/v1", "operation_status": "VERIFIED" if valid else "REJECTED",
                      "integrity_and_semantics_valid": valid}
            code = 0 if valid else 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        reason = exc.code if isinstance(exc, (InputError, ArtifactError)) else "L08_OPERATION_FAILED"
        print(json.dumps({"schema": "cdpd.l08-cli-result/v1", "operation_status": "BLOCKED",
                          "assessment_status": "UNKNOWN", "reason_code": reason}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
