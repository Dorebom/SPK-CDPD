"""L08 production-engine controls; all observations/adoptions are synthetic."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest

from cdpd_chatgpt_profile import baseline as bl
from cdpd_chatgpt_profile import report_engine as engine
from cdpd_chatgpt_profile.bundle_validation import canonical, verify_report_bundle
from l08_fixture import (
    approval_for, approval_record_for, history_partial_payload,
    pack_payload, rejected_payload, valid_blob, valid_payload,
)


def probe(payload, feature):
    return next(row for row in payload["STRUCTURE.json"]["profile"]["probes"]
                if row["feature_id"] == feature)


def derive(source, baseline=None):
    baseline = baseline if baseline is not None else valid_blob()
    return engine.derive_report(source, baseline, approval=approval_for(baseline)[2])


@pytest.mark.parametrize("recipe", bl.RECIPES)
def test_all_recipe_contracts_pass_with_exact_synthetic_adoption(recipe):
    blob = valid_blob(recipe)
    _, _, authority = approval_for(blob)
    report = engine.derive_report(blob, blob, approval=authority)
    assert report["assessment_status"] == "PASS"
    assert report["reason_codes"] == []
    assert report["context_recipe"] == recipe
    assert report["automatic_action"] == "NONE"
    assert engine.validate_report_bytes(canonical(report), blob, blob, approval=authority)
    assert len(report["findings"]) == 5
    assert len({f["feature_id"] for f in report["findings"]}) == 5
    assert report["hypothesis_code"] == "NOT_EVALUATED"
    assert report["confidence"] == "NOT_ESTIMATED"


def test_missing_baseline_and_candidate_never_adopt():
    source = valid_blob()
    before = copy.deepcopy(source)
    candidate = bl.baseline_candidate_metadata(source)
    assert candidate["adoption_status"] == "CANDIDATE_UNAPPROVED"
    assert candidate["baseline_status"] == "BASELINE_UNAPPROVED"
    assert candidate["automatic_approval"] is False
    for base in (None, source):
        report = engine.derive_report(source, base)
        assert report["assessment_status"] == "UNKNOWN"
        assert "BASELINE_NOT_APPROVED" in report["reason_codes"]
        assert report["baseline_approved"] is False
    assert source == before


@pytest.mark.parametrize("assertion", [True, False, {"approved": True}, {"baseline_approved": True},
                                      {"prevalidated": True, "all_required_ok": True}])
def test_external_self_approval_flags_have_no_authority(assertion):
    blob = valid_blob()
    report = engine.derive_report(blob, blob, approval=assertion)
    assert report["assessment_status"] == "UNKNOWN"
    assert report["baseline_approved"] is False


def test_approval_is_frozen_and_cannot_be_directly_constructed():
    blob = valid_blob()
    raw, trusted, authority = approval_for(blob)
    with pytest.raises(FrozenInstanceError):
        authority.raw_record = b"{}"
    with pytest.raises(TypeError, match="TRUSTED_APPROVAL_BOUNDARY_REQUIRED"):
        bl.ApprovalAuthority(raw, trusted, "0" * 64, "1" * 64)


@pytest.mark.parametrize("pin", [None, "", "0" * 64, "A" * 64, True])
def test_approval_requires_independently_supplied_exact_raw_record_pin(pin):
    blob = valid_blob()
    raw, _, _ = approval_for(blob)
    assert bl.verify_approval_record(raw, trusted_approval_sha256=pin, baseline_blob=blob) is None


@pytest.mark.parametrize("field,value", [
    ("source_manifest_sha256", "1" * 64), ("normalized_profile_sha256", "2" * 64),
    ("recipe", "GENERATING"), ("catalog_sha256", "3" * 64),
    ("normalization_sha256", "4" * 64), ("policy_sha256", "5" * 64),
    ("locale_class", "en-US"), ("viewport_class", "mobile"),
    ("browser_profile", "OTHER_PROFILE"), ("decision", "REJECT"),
    ("approved_utc", "2026-99-99"), ("approver_id", "query?secret=VALUE"),
    ("previous_manifest_sha256", "not-a-hash"), ("approved", True),
])
def test_approval_record_closure_and_every_binding(field, value):
    blob = valid_blob()
    record = approval_record_for(blob)
    record[field] = value
    raw = canonical(record)
    assert bl.verify_approval_record(raw, trusted_approval_sha256=hashlib.sha256(raw).hexdigest(),
                                     baseline_blob=blob) is None


def test_authority_is_rechecked_against_changed_baseline_bytes():
    baseline = valid_blob()
    _, _, authority = approval_for(baseline)
    altered_payload = valid_payload()
    altered_payload["RESULT.json"]["profile"]["capture_id"] = "C-" + "7" * 32
    altered = pack_payload(altered_payload)
    assert bl.normalized_profile_sha256(altered) == bl.normalized_profile_sha256(baseline)
    report = engine.derive_report(altered, altered, approval=authority)
    assert report["assessment_status"] == "UNKNOWN"
    assert report["baseline_approved"] is False


def test_normalization_ignores_identity_capture_and_global_nodes_but_not_feature_shape():
    base_payload = valid_payload()
    base_payload["STRUCTURE.json"]["nodes"] = [{
        "index": 0, "parent": -1, "tag": "div", "role": None,
        "shadowBoundary": False, "disabled": False, "hidden": False,
        "visible": True, "box": [0, 0, 10, 10]}]
    baseline = pack_payload(base_payload)
    payload = copy.deepcopy(base_payload)
    payload["RESULT.json"]["profile"]["capture_id"] = "C-" + "9" * 32
    # A safe global-node bbox change is excluded from feature compatibility.
    payload["STRUCTURE.json"]["nodes"][0]["box"] = [20, 30, 15, 12]
    after = pack_payload(payload)
    assert bl.normalized_profile_sha256(after) == bl.normalized_profile_sha256(baseline)
    assert derive(after, baseline)["assessment_status"] == "PASS"
    payload["STRUCTURE.json"]["profile"]["feature_shapes"]["composer.input"][0]["tag"] = "textarea"
    changed = pack_payload(payload)
    assert bl.normalized_profile_sha256(changed) != bl.normalized_profile_sha256(baseline)
    report = derive(changed, baseline)
    assert report["assessment_status"] == "REVIEW"
    assert "SIGNIFICANT_DRIFT" in report["reason_codes"]
    assert not any(f["verdict"] == "BREAKING" for f in report["findings"])


@pytest.mark.parametrize("recipe,feature", [
    ("IDLE_EMPTY", "composer.input"), ("IDLE_EMPTY", "composer.send"),
    ("IDLE_INPUT_PRESENT", "composer.input"), ("IDLE_INPUT_PRESENT", "composer.send"),
    ("GENERATING", "generation.stop"), ("RESPONSE_READY", "response.container"),
])
def test_fully_observed_required_function_absence_is_breaking(recipe, feature):
    baseline = valid_blob(recipe)
    payload = valid_payload(recipe)
    probe(payload, feature).update(match_count=0, visible_count=0, enabled_count=0)
    report = derive(pack_payload(payload), baseline)
    assert report["assessment_status"] == "BREAKING"
    assert "REQUIRED_FUNCTION_VIOLATED" in report["reason_codes"]
    finding = next(row for row in report["findings"] if row["feature_id"] == feature)
    assert finding["verdict"] == "BREAKING"
    assert finding["observed"]["match_count"] == 0


def test_empty_input_disabled_send_allowed_input_present_disabled_send_breaks():
    assert derive(valid_blob())["assessment_status"] == "PASS"
    payload = valid_payload("IDLE_INPUT_PRESENT")
    probe(payload, "composer.send")["enabled_count"] = 0
    assert derive(pack_payload(payload), valid_blob("IDLE_INPUT_PRESENT"))["assessment_status"] == "BREAKING"


def test_hidden_attachment_is_allowed_optional_count_change_is_review():
    assert probe(valid_payload(), "attachment.input")["visible_count"] == 0
    payload = valid_payload()
    probe(payload, "attachment.input").update(match_count=2, enabled_count=2)
    report = derive(pack_payload(payload))
    assert report["assessment_status"] == "REVIEW"
    assert next(f for f in report["findings"] if f["feature_id"] == "attachment.input")["reason_code"] == "OPTIONAL_FUNCTION_DRIFT"


@pytest.mark.parametrize("feature", ["composer.input", "attachment.input"])
@pytest.mark.parametrize("change", ["missing", "incomplete"])
def test_applicable_missing_or_incomplete_even_optional_is_unknown(feature, change):
    payload = valid_payload()
    if change == "missing":
        payload["STRUCTURE.json"]["profile"]["probes"] = [
            p for p in payload["STRUCTURE.json"]["profile"]["probes"] if p["feature_id"] != feature]
    else:
        probe(payload, feature)["probe_complete"] = False
    report = derive(pack_payload(payload))
    assert report["assessment_status"] == "UNKNOWN"
    assert "REQUIRED_PROBE_INCOMPLETE" in report["reason_codes"]


def test_unknown_dominates_breaking_but_preserves_observed_violation():
    payload = history_partial_payload()
    probe(payload, "composer.input").update(match_count=0, visible_count=0, enabled_count=0)
    report = derive(pack_payload(payload))
    assert report["assessment_status"] == "UNKNOWN"
    assert "CAPTURE_NOT_COMPLETE" in report["reason_codes"]
    assert "REQUIRED_COVERAGE_MISSING" in report["reason_codes"]
    assert "REQUIRED_FUNCTION_VIOLATED" in report["reason_codes"]
    assert report["findings"][0]["verdict"] == "BREAKING"


@pytest.mark.parametrize("reason", [None, "NONE", "POLICY_DENIED", "NOT_RUN"])
def test_n01_metadata_reason_compatibility_retains_unknown(reason):
    blob = pack_payload(rejected_payload(reason))
    verify_report_bundle(blob)
    report = derive(blob)
    assert report["assessment_status"] == "UNKNOWN"
    assert report["capture_status"] == "REJECTED"
    assert "INTEGRITY_INVALID" not in report["reason_codes"]
    assert engine.validate_report_bytes(canonical(report), blob, valid_blob(), approval=approval_for(valid_blob())[2])


def test_n02_history_only_partial_can_keep_structure_but_never_approve_baseline():
    blob = pack_payload(history_partial_payload())
    _, payload = verify_report_bundle(blob)
    assert "STRUCTURE.json" in payload and not payload["STRUCTURE.json"]["truncated"]
    report = derive(blob)
    assert report["assessment_status"] == "UNKNOWN"
    assert "INTEGRITY_INVALID" not in report["reason_codes"]
    with pytest.raises(bl.BaselineInputError, match="BASELINE_INELIGIBLE"):
        bl.baseline_candidate_metadata(blob)
    raw, trusted, _ = approval_for(valid_blob())
    assert bl.verify_approval_record(raw, trusted_approval_sha256=trusted, baseline_blob=blob) is None


@pytest.mark.parametrize("side", ["source", "baseline"])
@pytest.mark.parametrize("mutation", ["raw_hash", "schema", "history_contradiction", "failed_structure", "extra_file"])
def test_both_inputs_always_use_full_frontend_and_reject_forged_pass(side, mutation):
    good = valid_blob()
    payload = valid_payload()
    if mutation == "schema":
        payload["RESULT.json"]["profile"]["prevalidated"] = True
    elif mutation == "history_contradiction":
        payload["RESULT.json"]["profile"]["trigger_envelope"]["profile"]["loss_count_exact"] = False
    elif mutation == "failed_structure":
        payload["RESULT.json"]["profile"]["stages"]["S2"].update(status="FAILED", reason_code="CDP_COMMAND_FAILED")
    invalid = pack_payload(payload)
    if mutation == "raw_hash":
        invalid["RESULT.json"] += b" "
    elif mutation == "extra_file":
        invalid["PRIVATE.txt"] = b"SYNTHETIC_SECRET"
    source, baseline = (invalid, good) if side == "source" else (good, invalid)
    authority = approval_for(good)[2]
    report = engine.derive_report(source, baseline, approval=authority)
    assert report["assessment_status"] == "UNKNOWN"
    assert "INTEGRITY_INVALID" in report["reason_codes"]
    assert engine.validate_report_bytes(canonical(report), source, baseline, approval=authority)
    forged = engine.derive_report(good, good, approval=authority)
    assert forged["assessment_status"] == "PASS"
    assert not engine.validate_report_bytes(canonical(forged), source, baseline, approval=authority)
    assert b"SYNTHETIC_SECRET" not in canonical(report)


@pytest.mark.parametrize("field,value", [("locale_class", "en-US"), ("viewport_class", "mobile"),
                                         ("browser_profile", "OTHER_PROFILE"),
                                         ("normalization_sha256", "1" * 64),
                                         ("policy_sha256", "2" * 64), ("context_verified", False)])
def test_context_or_policy_mismatch_cannot_pass(field, value):
    payload = valid_payload()
    payload["RESULT.json"]["profile"]["report_context"][field] = value
    report = derive(pack_payload(payload))
    assert report["assessment_status"] == "UNKNOWN"
    assert any(c in report["reason_codes"] for c in ("CONTEXT_NOT_COMPARABLE", "CATALOG_NOT_COMPARABLE"))


@pytest.mark.parametrize("mutation", ["pointer", "count", "omit_finding", "duplicate_finding", "extra_capture_id", "status"])
def test_receiver_rederives_every_field_and_never_repairs(mutation):
    blob = valid_blob()
    authority = approval_for(blob)[2]
    report = engine.derive_report(blob, blob, approval=authority)
    if mutation == "pointer":
        report["findings"][0]["evidence_pointer"] = "STRUCTURE.json#/profile/probes/1"
    elif mutation == "count":
        report["findings"][0]["observed"]["match_count"] = 2
    elif mutation == "omit_finding":
        report["findings"].pop()
    elif mutation == "duplicate_finding":
        report["findings"].append(report["findings"][0])
    elif mutation == "extra_capture_id":
        report["capture_id"] = "C-" + "1" * 32
    else:
        report["assessment_status"] = "REVIEW"
        report["recommended_action"] = "REVIEW_NONBREAKING_DRIFT"
    before = copy.deepcopy(report)
    assert not engine.validate_report_bytes(canonical(report), blob, blob, approval=authority)
    assert report == before


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b'{"value":NaN}', b'{"value":Infinity}',
                                  b'\xff', b'[' * 1000, b'X' * 262145])
def test_report_receiver_strict_bytes(raw):
    blob = valid_blob()
    assert not engine.validate_report_bytes(raw, blob, blob, approval=approval_for(blob)[2])


def test_offline_determinism_and_inputs_unchanged():
    blob = valid_blob()
    authority = approval_for(blob)[2]
    before = copy.deepcopy(blob)
    outputs = [engine.canonical_report_bytes(blob, blob, approval=authority) for _ in range(3)]
    assert outputs[0] == outputs[1] == outputs[2]
    assert blob == before
    assert engine.validate_report_bytes(outputs[0], blob, blob, approval=authority)
    # Network/Playwright are prohibited by the preserved inherited autouse fixture.


def test_core_runtime_has_no_test_import_or_mutation_or_network_dependency():
    import ast
    root = Path(__file__).resolve().parents[1]
    forbidden = {"socket", "requests", "urllib", "subprocess", "playwright", "fix001_model", "bundle_input_model"}
    for name in ("baseline.py", "report_engine.py"):
        tree = ast.parse((root / "code/cdpd_chatgpt_profile" / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split('.')[0] not in forbidden for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert (node.module or '').split('.')[0] not in forbidden
                assert not (node.module or '').startswith('test')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {'write_bytes', 'write_text', 'unlink', 'rename', 'replace', 'mkdir'}


@pytest.mark.parametrize("locale", ["aa-aaaaaa-aaaaaaa-aaaaaaa-aaaaaaa",
                                    "aa-aaaaaaa-aaaaaaa-aaaaaaa-aaaaaaa",
                                    "aaa-aaaaaaa-aaaaaaa-aaaaaaa-aaaaaaa"])
def test_approval_preserves_the_accepted_maximum_locale_shape(locale):
    payload = valid_payload()
    assert 33 <= len(locale) <= 35
    payload["RESULT.json"]["profile"]["report_context"]["locale_class"] = locale
    blob = pack_payload(payload)
    authority = approval_for(blob)[2]
    assert engine.derive_report(blob, blob, approval=authority)["assessment_status"] == "PASS"


@pytest.mark.parametrize("case", [
    "supplement_metadata_NOT_RUN_reason_None_source",
    "supplement_metadata_NOT_RUN_reason_None_baseline",
    "grid_history_S1_COMPLETE_S2_COMPLETE_source",
    "grid_history_S1_COMPLETE_S2_COMPLETE_baseline",
])
def test_exact_original_n01_n02_fixtures_preserve_shared_frontend_and_unknown(case):
    directory = Path(__file__).resolve().parent / "fixtures/l06_original_issue_cases" / case
    source = {p.name: p.read_bytes() for p in (directory / "source_bundle").iterdir()}
    baseline = {p.name: p.read_bytes() for p in (directory / "baseline_bundle").iterdir()}
    verify_report_bundle(source)
    verify_report_bundle(baseline)
    try:
        authority = approval_for(baseline)[2]
    except bl.BaselineInputError:
        authority = None  # Invalid/incomplete Baseline cannot be adopted even in a test.
    report = engine.derive_report(source, baseline, approval=authority)
    assert report["assessment_status"] == "UNKNOWN"
    assert "INTEGRITY_INVALID" not in report["reason_codes"]
    assert engine.validate_report_bytes(canonical(report), source, baseline, approval=authority)
    report.update(assessment_status="PASS", reason_codes=[], recommended_action="NONE")
    assert not engine.validate_report_bytes(canonical(report), source, baseline, approval=authority)


def approved_oracle_derivation():
    """Test-only differential reference: immutable approved rule functions.

    Use the accepted production front-end for both sides, as required by L08;
    do not import or change historical model files or turn them into production.
    """
    import ast
    reference = Path(__file__).resolve().parents[1] / "references/approved_l02/tests/fix001_model.py"
    raw = reference.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "7eb5c9faf21d7025d0878a93fd6667deda6a5ae7a04abe14f2fe821e8f67a086"
    wanted = {"digest", "object_digest", "checked_probe", "probe_map", "expected_probe",
              "satisfies", "checked_shape", "stages_verified", "derive_report"}
    tree = ast.parse(raw)
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in selected} == wanted
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {"Any": object, "copy": copy, "hashlib": hashlib,
                 "canonical": canonical, "verified_bundle": verify_report_bundle}
    exec(compile(module, str(reference), "exec"), namespace)
    return namespace["derive_report"]


@pytest.mark.parametrize("variant", [*bl.RECIPES, "shape", "required_missing", "optional_change",
                                    "probe_incomplete", "history_partial", "rejected", "no_baseline",
                                    "source_corrupt", "baseline_corrupt"])
def test_core_exact_parity_with_approved_rule_functions(variant):
    recipe = variant if variant in bl.RECIPES else "IDLE_EMPTY"
    baseline = valid_blob(recipe)
    raw, _, authority = approval_for(baseline)
    legacy_approval = json.loads(raw)
    legacy_approval["approved"] = True  # Explicit oracle-only mock, never product authority.
    payload = valid_payload(recipe)
    if variant == "shape":
        payload["STRUCTURE.json"]["profile"]["feature_shapes"]["composer.input"][0]["tag"] = "textarea"
    elif variant == "required_missing":
        probe(payload, "composer.input").update(match_count=0, visible_count=0, enabled_count=0)
    elif variant == "optional_change":
        probe(payload, "attachment.input").update(match_count=2, enabled_count=2)
    elif variant == "probe_incomplete":
        probe(payload, "attachment.input")["probe_complete"] = False
    elif variant == "history_partial":
        payload = history_partial_payload()
    elif variant == "rejected":
        payload = rejected_payload()
    source = pack_payload(payload)
    if variant == "source_corrupt":
        source["RESULT.json"] += b" "
    elif variant == "baseline_corrupt":
        baseline["RESULT.json"] += b" "
    elif variant == "no_baseline":
        baseline = None
        authority = None
        legacy_approval = None
    policy = json.loads((Path(__file__).resolve().parents[1] / "contracts/report_policy.json").read_bytes())
    expected = approved_oracle_derivation()(source, baseline, legacy_approval, policy)
    actual = engine.derive_report(source, baseline, approval=authority)
    assert actual == expected


@pytest.mark.parametrize("location", ["correlation", "forbidden_value", "forbidden_url"])
def test_raw_secrets_never_leave_core_error_or_success_paths(location):
    secret = "SYNTHETIC_CANARY_PROMPT_AND_VALUE_https://example.invalid/?token=PRIVATE"
    payload = valid_payload()
    if location == "correlation":
        payload["TRIGGER.json"]["correlation"]["opaque_test"] = secret
        payload["RESULT.json"]["profile"]["trigger_envelope"]["event"]["correlation"]["opaque_test"] = secret
    elif location == "forbidden_value":
        probe(payload, "composer.input")["value"] = secret
    else:
        payload["STRUCTURE.json"]["url"] = secret
    blob = pack_payload(payload)
    report = derive(blob)
    assert secret.encode() not in canonical(report)
    assert b"PRIVATE" not in canonical(report)
    assert report["assessment_status"] == ("PASS" if location == "correlation" else "UNKNOWN")
