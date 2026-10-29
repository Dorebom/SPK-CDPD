"""A scheduled invocation is never authority to start or retry a capture.

OFFLINE_REPORT replays explicitly pinned committed bytes. SCHEDULED_CAPTURE
retains the inherited unavailable trusted-idle integration boundary. It cannot
start Chrome, renew Binding, read DOM, send, retry, or call a capture worker.
"""
from __future__ import annotations

from enum import Enum
import json
from pathlib import Path

from .bundle_validation import canonical, strict_json
from .evidence_store import _read_regular, _safe_directory
from .l08_artifact_store import ArtifactError, ArtifactStore, absolute_path, sha256
from .report_cli import InputError, SafeArgumentParser, _disjoint_output, _pin, run_offline, storage_policy


class ChromeObservation(Enum):
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


def scheduled_capture_status(*, chrome_observer=None):
    """A trusted callable may establish a negative availability observation.

    It is not a live Binding/idle capability; even PRESENT cannot admit a
    capture. This parameter is available to trusted composition/tests only,
    never from configuration JSON or a dynamic module name.
    """
    observed = ChromeObservation.UNKNOWN
    if callable(chrome_observer):
        try:
            result = chrome_observer()
            if type(result) is ChromeObservation:
                observed = result
        except Exception:
            pass
    absent = observed is ChromeObservation.ABSENT
    return {"schema": "cdpd.l08-monitor-status/v1", "mode": "SCHEDULED_CAPTURE",
            "operation_status": "SKIPPED" if absent else "BLOCKED", "assessment_status": "UNKNOWN",
            "reason_code": "CHROME_NOT_RUNNING" if absent else "TRUSTED_LIVE_IDLE_BOUNDARY_UNAVAILABLE",
            "capture_status": "NOT_CAPTURED", "capture_id": None,
            "chrome_observation": observed.value, "capture_attempts": 0, "web_sends": 0,
            "web_retries": 0, "new_binding": False, "automatic_action": "NONE"}


def load_config(path, expected_sha256):
    selected = absolute_path(path)
    _safe_directory(selected.parent, allow_missing=False)
    raw = _read_regular(selected, 32768)
    if sha256(raw) != _pin(expected_sha256):
        raise InputError()
    value = strict_json(raw, 32768)
    if type(value) is not dict or value.get("schema") != "cdpd.l08.monitor-config/v1":
        raise InputError()
    common = {"schema", "mode", "output_directory", "profile_directory"}
    if value.get("mode") == "OFFLINE_REPORT":
        allowed = common | {"source_bundle", "source_commit_sha256", "baseline_bundle",
            "baseline_commit_sha256", "approval_record", "trusted_approval_sha256"}
        if set(value) != allowed:
            raise InputError()
        _pin(value["source_commit_sha256"])
        for key in ("source_bundle", "output_directory"):
            absolute_path(value[key])
        for key in ("baseline_bundle", "approval_record", "profile_directory"):
            if value[key] is not None:
                absolute_path(value[key])
        for key in ("baseline_commit_sha256", "trusted_approval_sha256"):
            if value[key] is not None:
                _pin(value[key])
        if (value["baseline_bundle"] is None) != (value["baseline_commit_sha256"] is None):
            raise InputError()
        if (value["approval_record"] is None) != (value["trusted_approval_sha256"] is None):
            raise InputError()
    elif value.get("mode") == "SCHEDULED_CAPTURE":
        if set(value) != common:
            raise InputError()
        absolute_path(value["output_directory"])
        if value["profile_directory"] is not None:
            absolute_path(value["profile_directory"])
    else:
        raise InputError()
    return value


def main(argv=None):
    parser = SafeArgumentParser(description="Pinned L08 offline replay or fail-closed scheduled-capture admission.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    try:
        args = parser.parse_args(argv)
        config = load_config(args.config, args.expected_config_sha256)
        _disjoint_output(config["output_directory"], [args.config])
        if config["mode"] == "OFFLINE_REPORT":
            result = run_offline(**{k: v for k, v in config.items() if k not in {"schema", "mode"}})
            result["scheduled_invocation"] = "OFFLINE_REPLAY_ONLY"
            code = {"PASS": 0, "REVIEW": 10, "BREAKING": 11, "UNKNOWN": 12}[result["assessment_status"]]
        else:
            result = scheduled_capture_status()
            raw = canonical(result)
            markdown = ("# CDPD L08 scheduled capture\n\n"
                "**BLOCKED / UNKNOWN**\n\n"
                "Reason: TRUSTED_LIVE_IDLE_BOUNDARY_UNAVAILABLE.\n\n"
                "No capture, browser start, Binding renewal or Web operation was attempted.\n").encode()
            artifact = ArtifactStore(config["output_directory"], kind="MONITOR_STATUS",
                windows_storage_authorizer=storage_policy(config["output_directory"], config["profile_directory"])
                ).write({"monitor-status.json": raw, "monitor-status.md": markdown})
            result = {**result, "artifact_id": artifact.name}
            code = 12
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        reason = exc.code if isinstance(exc, (InputError, ArtifactError)) else "L08_MONITOR_INPUT_INVALID"
        print(json.dumps({"schema": "cdpd.l08-monitor-result/v1", "operation_status": "BLOCKED",
                          "assessment_status": "UNKNOWN", "reason_code": reason}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
