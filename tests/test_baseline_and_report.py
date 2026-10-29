"""Original L08 AC/DIF integration, privacy and publication-boundary evidence.

All approval/live observations here are synthetic fixtures. These tests never
start a browser, register a task, adopt a Baseline or exercise native Windows.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from cdpd_chatgpt_profile import l08_artifact_store as store
from cdpd_chatgpt_profile.baseline import BaselineInputError
from cdpd_chatgpt_profile.bundle_validation import canonical, verify_report_bundle
from cdpd_chatgpt_profile.l08_monitor import ChromeObservation, load_config, scheduled_capture_status
from cdpd_chatgpt_profile import l08_monitor, report_cli
from cdpd_chatgpt_profile.report import (
    derive_l08_report, report_payloads, render_markdown, validate_l08_report_bytes,
    verify_report_package, write_baseline_candidate, write_report_package,
)
from l08_fixture import valid_blob, valid_payload, pack_payload, approval_for, history_partial_payload

CANARY = "PRIVATE_CHAT_INPUT_VALUE_https://chatgpt.com/?token=secret_QUERY_123"


@pytest.fixture(autouse=True)
def l08_windows_synthetic_scope(request, monkeypatch):
    """New L08-only extension of the unchanged inherited synthetic fixture.

    This grants only the executing test's private tmp_path, never native ACL
    acceptance. Production CLI has no flag/environment equivalent.
    """
    if os.name != "nt" or request.node.get_closest_marker("no_windows_synthetic_capability"):
        return
    sandbox = request.getfixturevalue("tmp_path").absolute()
    original = store.ArtifactStore.__init__
    def grant(path):
        try:
            selected = Path(path).absolute()
            selected.relative_to(sandbox)
            for item in (selected, *selected.parents):
                if item == sandbox.parent:
                    break
                if item.exists() or item.is_symlink():
                    info = item.lstat()
                    if item.is_symlink() or store._is_reparse(info):
                        return False
            return True
        except (ValueError, OSError):
            return False
    def synthetic_init(self, root, *, kind, windows_storage_authorizer=None, fault_hook=None):
        if windows_storage_authorizer is None and grant(root):
            windows_storage_authorizer = grant
        original(self, root, kind=kind, windows_storage_authorizer=windows_storage_authorizer,
                 fault_hook=fault_hook)
    real_policy = report_cli.storage_policy
    def synthetic_policy(root, profile):
        return grant if grant(root) else real_policy(root, profile)
    monkeypatch.setattr(store.ArtifactStore, "__init__", synthetic_init)
    monkeypatch.setattr(report_cli, "storage_policy", synthetic_policy)
    monkeypatch.setattr(l08_monitor, "storage_policy", synthetic_policy)


def saved_bundle(parent, blob):
    capture = json.loads(blob["MANIFEST.json"])["capture_id"]
    directory = parent / capture
    directory.mkdir(parents=True)
    for name, raw in blob.items():
        (directory / name).write_bytes(raw)
    return directory


def file_snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def no_external(*args, **kwargs):
    raise AssertionError("EXTERNAL_ACTION_FORBIDDEN")


def test_ac01_dif01_excluded_bbox_change_is_not_breaking():
    payload = valid_payload()
    payload["STRUCTURE.json"]["nodes"] = [{"index": 0, "parent": -1, "tag": "div", "role": None,
        "shadowBoundary": False, "disabled": False, "hidden": False, "visible": True, "box": [0, 0, 20, 20]}]
    baseline = pack_payload(payload)
    payload["STRUCTURE.json"]["nodes"][0]["box"] = [0, 0, 10, 10]
    # An unrelated broad-tree node is not the normalized functional feature.
    payload["STRUCTURE.json"]["nodes"][0]["tag"] = "section"
    source = pack_payload(payload)
    _, _, authority = approval_for(baseline)
    result = derive_l08_report(source, baseline, approval=authority)
    assert result["report"]["assessment_status"] == "PASS"
    assert result["before_sha256"] != result["after_sha256"]
    assert result["before_normalized_sha256"] == result["after_normalized_sha256"]


def test_ac01_dif01_local_feature_shape_change_is_review():
    baseline = valid_blob()
    payload = valid_payload()
    shape = payload["STRUCTURE.json"]["profile"]["feature_shapes"]["composer.input"]
    shape.append({"tag": "span", "role": None, "parent": None, "shadow": False})
    source = pack_payload(payload)
    _, _, authority = approval_for(baseline)
    result = derive_l08_report(source, baseline, approval=authority)
    assert result["report"]["assessment_status"] == "REVIEW"
    assert result["failed_checks"][-1]["check_id"] == "NONBREAKING_DRIFT"


def test_ac01_dif02_missing_composer_breaking_with_actual_failed_condition():
    baseline = valid_blob()
    payload = valid_payload()
    probe = next(p for p in payload["STRUCTURE.json"]["profile"]["probes"] if p["feature_id"] == "composer.input")
    probe.update(match_count=0, visible_count=0, enabled_count=0)
    source = pack_payload(payload)
    _, _, authority = approval_for(baseline)
    result = derive_l08_report(source, baseline, approval=authority)
    assert result["report"]["assessment_status"] == "BREAKING"
    assert any(c["check_id"] == "REQUIRED_FUNCTION" for c in result["failed_checks"])
    raw = report_payloads(source, baseline, approval=authority)
    assert b"composer.input" in raw["report.md"]
    assert b"0/0/0/True" in raw["report.md"]
    assert result["before_sha256"] in raw["report.md"].decode()
    assert result["after_sha256"] in raw["report.md"].decode()


def test_ac02_dif03_missing_baseline_named_boundary_without_core_schema_change():
    source = valid_blob()
    result = derive_l08_report(source)
    assert result["baseline_status"] == "BASELINE_UNAPPROVED"
    assert result["report"]["assessment_status"] == "UNKNOWN"
    assert "BASELINE_NOT_APPROVED" in result["report"]["reason_codes"]
    assert "capture_id" not in result["report"]
    assert result["capture_id"] == json.loads(source["MANIFEST.json"])["capture_id"]
    assert validate_l08_report_bytes(canonical(result), source)


def test_ac03_offline_determinism_and_input_immutability(monkeypatch):
    source = valid_blob()
    original = copy.deepcopy(source)
    _, _, authority = approval_for(source)
    monkeypatch.setattr(socket.socket, "connect", no_external)
    monkeypatch.setattr(socket, "create_connection", no_external)
    monkeypatch.setattr(subprocess, "Popen", no_external)
    first = report_payloads(source, source, approval=authority)
    second = report_payloads(source, source, approval=authority)
    assert first == second
    assert source == original
    assert json.loads(source["RESULT.json"])["assessment_status"] == "NOT_PERFORMED"


@pytest.mark.parametrize("location", ["node_text", "node_value", "node_url", "result_exception", "probe_selector"])
def test_dif04_dynamic_canary_never_appears_in_json_markdown_or_diagnostics(location):
    baseline = valid_blob()
    payload = valid_payload()
    if location.startswith("node_"):
        name = {"node_text": "textContent", "node_value": "value", "node_url": "href"}[location]
        payload["STRUCTURE.json"]["nodes"] = [{"index": 0, "parent": -1, "tag": "div", "role": None,
            "shadowBoundary": False, "disabled": False, "hidden": False, "visible": True, "box": [0, 0, 20, 20]}]
        payload["STRUCTURE.json"]["nodes"][0][name] = CANARY
    elif location == "result_exception":
        payload["RESULT.json"]["exception"] = CANARY
    else:
        payload["STRUCTURE.json"]["profile"]["probes"][0]["selector"] = CANARY
    source = pack_payload(payload)  # Honest hashes do not excuse privacy/schema violation.
    _, _, authority = approval_for(baseline)
    result = report_payloads(source, baseline, approval=authority)
    assert json.loads(result["report.json"])["report"]["assessment_status"] == "UNKNOWN"
    assert all(CANARY.encode() not in raw for raw in result.values())
    assert all(b"token=secret" not in raw for raw in result.values())


def test_dif04_bounded_opaque_private_event_identifier_is_not_rendered():
    source = valid_payload()
    # Accepted producer opaque correlation is a trust assumption; the report
    # still avoids copying it, even when a producer violates that assumption.
    source["TRIGGER.json"]["operation_id"] = CANARY
    source["RESULT.json"]["profile"]["trigger_envelope"]["event"]["operation_id"] = CANARY
    blob = pack_payload(source)
    outputs = report_payloads(blob)
    assert all(CANARY.encode() not in raw for raw in outputs.values())


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b'{"x":NaN}', b'\xff', b'[]', b'null'])
def test_dif04_invalid_json_cannot_create_a_privacy_leak(raw):
    source = valid_blob()
    source["RESULT.json"] = raw
    assert all(CANARY.encode() not in output for output in report_payloads(source).values())


def test_dif04_renderer_rejects_forged_text_and_extra_fields():
    value = derive_l08_report(valid_blob())
    value["report"]["reason_codes"] = [CANARY]
    with pytest.raises(store.ArtifactError, match="ARTIFACT_INVALID"):
        render_markdown(value)


@pytest.mark.parametrize("module", ["baseline.py", "report_engine.py", "report.py", "l08_artifact_store.py"])
def test_dif04_static_pure_pipeline_has_no_network_browser_or_test_import(module):
    tree = ast.parse((ROOT / "code/cdpd_chatgpt_profile" / module).read_text())
    forbidden = {"socket", "requests", "urllib", "websocket", "websockets", "playwright", "selenium", "subprocess", "tests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {n.name.split(".")[0] for n in node.names} & forbidden
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden


def test_dif05_chrome_absent_is_skipped_unknown_no_capture(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", no_external)
    monkeypatch.setattr(subprocess, "Popen", no_external)
    value = scheduled_capture_status(chrome_observer=lambda: ChromeObservation.ABSENT)
    assert value["operation_status"] == "SKIPPED"
    assert value["assessment_status"] == "UNKNOWN"
    assert value["reason_code"] == "CHROME_NOT_RUNNING"
    assert value["capture_attempts"] == value["web_sends"] == value["web_retries"] == 0
    assert value["capture_id"] is None


@pytest.mark.parametrize("result", [True, {"idle": True, "chrome_running": True}, "PRESENT", ChromeObservation.PRESENT])
def test_dif05_presence_or_saved_bool_never_supplies_idle_authority(result):
    value = scheduled_capture_status(chrome_observer=lambda: result)
    assert value["operation_status"] == "BLOCKED"
    assert value["assessment_status"] == "UNKNOWN"
    assert value["capture_attempts"] == 0


@pytest.mark.parametrize("phase", ["AFTER_COMMIT_PUBLICATION", "BEFORE_READBACK"])
def test_report_commit_remains_immutable_after_publication_error(tmp_path, phase):
    source = valid_blob()
    def fail_at(name):
        if name == phase:
            raise OSError(CANARY)
    with pytest.raises(store.ArtifactError, match="POST_PUBLICATION_UNCONFIRMED") as error:
        write_report_package(tmp_path / "reports", source, fault_hook=fail_at)
    assert CANARY not in str(error.value)
    destination = next(p for p in (tmp_path / "reports").iterdir() if p.name.startswith("R-"))
    before = file_snapshot(destination)
    assert "COMMIT.json" in before
    assert verify_report_package(destination, source)
    assert write_report_package(tmp_path / "reports", source) == destination
    assert file_snapshot(destination) == before


@pytest.mark.parametrize("phase", ["BEFORE_COMMIT_STAGING", "BEFORE_COMMIT_PUBLICATION"])
def test_prepublication_failure_does_not_publish_or_repair(tmp_path, phase):
    source = valid_blob()
    def fail_at(name):
        if name == phase:
            raise OSError(CANARY)
    with pytest.raises(store.ArtifactError):
        write_report_package(tmp_path / "reports", source, fault_hook=fail_at)
    destination = next(p for p in (tmp_path / "reports").iterdir() if p.name.startswith("R-"))
    before = file_snapshot(destination)
    assert "COMMIT.json" not in before
    assert not verify_report_package(destination, source)
    with pytest.raises(store.ArtifactError):
        write_report_package(tmp_path / "reports", source)
    assert file_snapshot(destination) == before


def test_forged_report_with_self_consistent_hashes_is_rejected_semantically(tmp_path):
    source = valid_blob()
    value = derive_l08_report(source)
    value["report"]["assessment_status"] = "PASS"
    fake = store.ArtifactStore(tmp_path / "reports", kind="REPORT").write({
        "report.json": canonical(value), "report.md": b"FAKE PASS\n"})
    assert not verify_report_package(fake, source)
    assert not validate_l08_report_bytes(canonical(value), source)


def test_existing_report_tampering_is_not_overwritten(tmp_path):
    source = valid_blob()
    destination = write_report_package(tmp_path / "reports", source)
    (destination / "report.md").write_bytes(b"FOREIGN_DATA")
    before = file_snapshot(destination)
    with pytest.raises(store.ArtifactError):
        write_report_package(tmp_path / "reports", source)
    assert file_snapshot(destination) == before


def test_symlink_source_and_output_refused(tmp_path):
    source = saved_bundle(tmp_path / "source", valid_blob())
    link = tmp_path / "linked-source"
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("SYMLINK_CAPABILITY_UNAVAILABLE")
    with pytest.raises(report_cli.InputError):
        report_cli.read_evidence_bundle(link)
    output = tmp_path / "linked-output"
    output.symlink_to(source, target_is_directory=True)
    with pytest.raises(store.ArtifactError):
        write_report_package(output, valid_blob())


def test_baseline_candidate_has_no_approval_and_copies_exact_source(tmp_path):
    source = valid_blob()
    destination = write_baseline_candidate(tmp_path / "candidates", source)
    content = store.read_artifact(destination)
    metadata = json.loads(content["baseline-candidate.json"])
    assert metadata["adoption_status"] == "CANDIDATE_UNAPPROVED"
    assert metadata["automatic_approval"] is False
    capture = metadata["capture_id"]
    for name, raw in source.items():
        assert content["source_bundle/" + capture + "/" + name] == raw
    assert not any("approval" in name.lower() or "current" in name.lower() for name in content)
    readback = report_cli.read_evidence_bundle(destination / "source_bundle" / capture)
    assert readback == source
    assert derive_l08_report(source, readback)["report"]["assessment_status"] == "UNKNOWN"


def test_ineligible_partial_is_not_baseline_candidate(tmp_path):
    partial = pack_payload(history_partial_payload())
    with pytest.raises(BaselineInputError):
        write_baseline_candidate(tmp_path / "candidates", partial)
    assert not (tmp_path / "candidates").exists()


def test_capacity_rejects_without_deleting_existing_candidates(tmp_path, monkeypatch):
    root = tmp_path / "candidates"
    root.mkdir()
    original = root / "accepted-baseline-evidence"
    original.write_bytes(b"IMMUTABLE")
    monkeypatch.setattr(store, "BASELINE_ROOT_LIMIT", 8)
    with pytest.raises(store.ArtifactError, match="STORAGE_CAPACITY_EXCEEDED"):
        write_baseline_candidate(root, valid_blob())
    assert original.read_bytes() == b"IMMUTABLE"
    assert list(root.iterdir()) == [original]


def test_offline_cli_reproducible_and_source_baseline_unchanged(tmp_path, capsys, monkeypatch):
    source = valid_blob()
    source_path = saved_bundle(tmp_path / "source", source)
    baseline_path = saved_bundle(tmp_path / "baseline", source)
    raw, pin, _ = approval_for(source)
    approval = tmp_path / "human-test-only.json"
    approval.write_bytes(raw)
    originals = {p: file_snapshot(p) for p in [source_path, baseline_path]}
    monkeypatch.setattr(socket, "create_connection", no_external)
    monkeypatch.setattr(subprocess, "Popen", no_external)
    args = ["report", "--source", str(source_path), "--baseline", str(baseline_path),
            "--approval-record", str(approval), "--trusted-approval-sha256", pin,
            "--output-root", str(tmp_path / "reports")]
    assert report_cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert report_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert first["assessment_status"] == "PASS"
    for path, before in originals.items():
        assert file_snapshot(path) == before
    assert approval.read_bytes() == raw


def test_cli_output_cannot_be_inside_accepted_bundle(tmp_path, capsys):
    source = saved_bundle(tmp_path, valid_blob())
    before = file_snapshot(source)
    assert report_cli.main(["report", "--source", str(source), "--output-root", str(source / "reports")]) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "OUTPUT_OVERLAPS_INPUT"
    assert file_snapshot(source) == before


def test_cli_bad_argument_does_not_echo_private_value(capsys):
    assert report_cli.main([CANARY]) == 2
    streams = capsys.readouterr()
    assert CANARY not in streams.out + streams.err
    assert "CLI_INPUT_INVALID" in streams.out


def test_pinned_monitor_offline_replay_never_becomes_a_new_capture(tmp_path, capsys, monkeypatch):
    source = valid_blob()
    directory = saved_bundle(tmp_path / "source", source)
    config = {"schema": "cdpd.l08.monitor-config/v1", "mode": "OFFLINE_REPORT",
              "source_bundle": str(directory), "source_commit_sha256": store.sha256(source["COMMIT.json"]),
              "baseline_bundle": None, "baseline_commit_sha256": None, "approval_record": None,
              "trusted_approval_sha256": None, "output_directory": str(tmp_path / "reports"),
              "profile_directory": None}
    config_path = tmp_path / "monitor.json"
    config_raw = canonical(config)
    config_path.write_bytes(config_raw)
    monkeypatch.setattr(subprocess, "Popen", no_external)
    monkeypatch.setattr(socket, "create_connection", no_external)
    args = ["--config", str(config_path), "--expected-config-sha256", store.sha256(config_raw)]
    assert l08_monitor.main(args) == 12
    first = json.loads(capsys.readouterr().out)
    assert first["scheduled_invocation"] == "OFFLINE_REPLAY_ONLY"
    assert first["assessment_status"] == "UNKNOWN"
    assert l08_monitor.main(args) == 12
    assert json.loads(capsys.readouterr().out) == first


def test_scheduled_capture_config_never_loads_saved_idle_proof(tmp_path, capsys):
    config = {"schema": "cdpd.l08.monitor-config/v1", "mode": "SCHEDULED_CAPTURE",
              "output_directory": str(tmp_path / "reports"), "profile_directory": None,
              "idle": True, "chrome_running": True, "bootstrap": CANARY}
    raw = canonical(config)
    path = tmp_path / "monitor.json"
    path.write_bytes(raw)
    assert l08_monitor.main(["--config", str(path), "--expected-config-sha256", store.sha256(raw)]) == 2
    result = capsys.readouterr()
    assert CANARY not in result.out + result.err
    assert json.loads(result.out)["operation_status"] == "BLOCKED"
    assert not (tmp_path / "reports").exists()


def test_default_scheduled_capture_remains_blocked_unknown(tmp_path, capsys):
    config = {"schema": "cdpd.l08.monitor-config/v1", "mode": "SCHEDULED_CAPTURE",
              "output_directory": str(tmp_path / "reports"), "profile_directory": None}
    raw = canonical(config)
    path = tmp_path / "monitor.json"
    path.write_bytes(raw)
    assert l08_monitor.main(["--config", str(path), "--expected-config-sha256", store.sha256(raw)]) == 12
    result = json.loads(capsys.readouterr().out)
    assert result["assessment_status"] == "UNKNOWN"
    assert result["operation_status"] == "BLOCKED"
    assert result["capture_attempts"] == result["web_sends"] == result["web_retries"] == 0


def test_changed_config_pin_rejects_before_output(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(CANARY.encode())
    with pytest.raises(report_cli.InputError):
        load_config(path, "0" * 64)


@pytest.mark.no_windows_synthetic_capability
@pytest.mark.parametrize("authorizer", [None, lambda path: 1, lambda path: False])
def test_windows_writer_requires_exact_true_from_trusted_code(tmp_path, monkeypatch, authorizer):
    monkeypatch.setattr(store, "_windows_required", lambda: True)
    with pytest.raises(store.ArtifactError, match="WINDOWS_STORAGE_POLICY_UNVERIFIED"):
        write_report_package(tmp_path / "reports", valid_blob(), windows_storage_authorizer=authorizer)
    assert not (tmp_path / "reports").exists()
