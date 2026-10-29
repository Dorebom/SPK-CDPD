from __future__ import annotations

from pathlib import Path

import pytest

from chatgpt_web_api.completeness import (
    ProjectCompletenessError,
    evaluate_project_inventory,
)
from chatgpt_web_api.models import (
    ObservationStabilityProof,
    ProjectInventoryBaseline,
    ProjectObservation,
    ProjectObservationQueryResult,
    WebDebugEvidence,
)
from chatgpt_web_api.projects import (
    build_project_inventory_result,
    project_key_identity_set_digest,
    project_observation_identity_set_digest,
    query_project_inventory,
)


def obs(key: str | None, name: str | None = None) -> ProjectObservation:
    return ProjectObservation(
        name=name or key or "unresolved",
        project_key=key,
        url=f"https://chatgpt.com/g/{key}-project" if key else None,
    )


def baseline(keys: list[str], *, exhaustive: bool) -> ProjectInventoryBaseline:
    return ProjectInventoryBaseline(
        expected_project_keys=tuple(keys),
        exhaustive=exhaustive,
        baseline_id="acceptance-baseline",
        identity_set_digest=project_key_identity_set_digest(keys),
        authority="acceptance-fixture",
        source="test_project_completeness.py",
    )


def proof(
    rows: list[ProjectObservation] | tuple[ProjectObservation, ...],
    *,
    stable: bool = True,
    proof_id: str = "stability-proof-001",
) -> ObservationStabilityProof:
    return ObservationStabilityProof(
        proof_id=proof_id,
        sample_count=2,
        first_observed_at="2026-08-13T10:00:00+09:00",
        last_observed_at="2026-08-13T10:00:01+09:00",
        identity_set_digest=project_observation_identity_set_digest(rows),
        strategy_id="repeat-until-identity-set-converges/v1",
        stable=stable,
        evidence_ref="evidence://project-inventory/stability-proof-001.json",
    )


def evaluate_stable(
    rows: list[ProjectObservation] | tuple[ProjectObservation, ...],
    *,
    expected: ProjectInventoryBaseline | None = None,
):
    return evaluate_project_inventory(
        rows,
        stability_proof=proof(rows),
        expected=expected,
    )


def test_stable_observation_without_baseline_is_not_full_complete() -> None:
    rows = [obs("g-p-a"), obs("g-p-b")]
    result = evaluate_stable(rows)
    assert result.observed_stable
    assert result.all_observed_resolved
    assert result.known_identity_coverage_complete is None
    assert not result.complete


def test_naked_true_stability_is_not_accepted_as_completeness_proof() -> None:
    with pytest.raises(ProjectCompletenessError, match="ObservationStabilityProof"):
        evaluate_project_inventory(
            [obs("g-p-a")],
            observed_stable=True,
        )


def test_known_non_exhaustive_baseline_can_prove_coverage_but_not_completeness() -> None:
    rows = [obs("g-p-a"), obs("g-p-b")]
    result = evaluate_stable(
        rows,
        expected=baseline(["g-p-a", "g-p-b"], exhaustive=False),
    )
    assert result.known_identity_coverage_complete is True
    assert result.proof_exhaustive is False
    assert result.complete is False


def test_explicit_exhaustive_baseline_and_stability_proof_can_prove_full_completeness() -> None:
    rows = [obs("g-p-a"), obs("g-p-b")]
    result = evaluate_stable(
        rows,
        expected=baseline(["g-p-a", "g-p-b"], exhaustive=True),
    )
    assert result.known_identity_coverage_complete is True
    assert result.full_inventory_verified is True
    assert result.complete is True


def test_exhaustive_baseline_missing_identity_is_incomplete() -> None:
    rows = [obs("g-p-a")]
    result = evaluate_stable(
        rows,
        expected=baseline(["g-p-a", "g-p-b"], exhaustive=True),
    )
    assert result.missing_expected_project_keys == ("g-p-b",)
    assert not result.complete


def test_exhaustive_baseline_rejects_unexpected_extra_as_complete_proof() -> None:
    rows = [obs("g-p-a"), obs("g-p-b")]
    result = evaluate_stable(
        rows,
        expected=baseline(["g-p-a"], exhaustive=True),
    )
    assert result.unexpected_project_keys == ("g-p-b",)
    assert not result.complete


def test_exact_identity_set_but_unstable_proof_is_not_complete() -> None:
    rows = [obs("g-p-a")]
    result = evaluate_project_inventory(
        rows,
        stability_proof=proof(rows, stable=False),
        expected=baseline(["g-p-a"], exhaustive=True),
    )
    assert result.known_identity_coverage_complete is True
    assert result.observed_stable is False
    assert not result.complete


def test_unresolved_visible_project_prevents_full_completeness() -> None:
    rows = [obs("g-p-a"), obs(None, "visible-but-unresolved")]
    result = evaluate_stable(
        rows,
        expected=baseline(["g-p-a"], exhaustive=True),
    )
    assert result.resolved_count == 1
    assert not result.all_observed_resolved
    assert not result.complete


def test_duplicate_resolved_identity_prevents_full_completeness() -> None:
    rows = [obs("g-p-a"), obs("g-p-a")]
    result = evaluate_stable(
        rows,
        expected=baseline(["g-p-a"], exhaustive=True),
    )
    assert result.duplicate_project_keys == ("g-p-a",)
    assert not result.complete


def test_twenty_observed_of_twenty_five_expected_never_reports_complete() -> None:
    expected = [f"g-p-{i:02d}" for i in range(25)]
    observed = [obs(key) for key in expected[:20]]
    result = evaluate_stable(
        observed,
        expected=baseline(expected, exhaustive=True),
    )
    assert result.observed_count == 20
    assert result.resolved_count == 20
    assert len(result.missing_expected_project_keys) == 5
    assert not result.complete


def test_acceptance_fixture_twenty_five_total_seven_pinned_eighteen_projects_is_test_only() -> None:
    pinned = [f"g-p-pin-{i:02d}" for i in range(7)]
    projects = [f"g-p-project-{i:02d}" for i in range(18)]
    expected = pinned + projects
    rows = [obs(key) for key in expected]
    result = evaluate_stable(
        rows,
        expected=baseline(expected, exhaustive=True),
    )
    assert len(expected) == 25
    assert len(pinned) == 7
    assert len(projects) == 18
    assert result.complete


def test_reference_project_query_result_exposes_observation_and_completeness_separately() -> None:
    rows = (obs("g-p-a"), obs("g-p-b"))
    stability = proof(rows)
    result = build_project_inventory_result(
        rows,
        stability_proof=stability,
        expected=baseline(["g-p-a", "g-p-b"], exhaustive=False),
    )
    assert result.projects == rows
    assert result.completeness.observed_stable
    assert result.completeness.known_identity_coverage_complete is True
    assert result.complete is False
    assert result.stability_proof_id == stability.proof_id


def test_duplicate_expected_identity_is_invalid_baseline() -> None:
    rows = [obs("g-p-a")]
    with pytest.raises(ProjectCompletenessError):
        evaluate_project_inventory(
            rows,
            stability_proof=proof(rows),
            expected=baseline(["g-p-a", "g-p-a"], exhaustive=True),
        )


def test_empty_exhaustive_baseline_is_invalid() -> None:
    with pytest.raises(ProjectCompletenessError):
        evaluate_project_inventory(
            (),
            stability_proof=proof(()),
            expected=baseline([], exhaustive=True),
        )


def test_empty_non_exhaustive_baseline_is_not_completeness_proof() -> None:
    result = evaluate_project_inventory(
        (),
        stability_proof=proof(()),
        expected=baseline([], exhaustive=False),
    )
    assert result.known_identity_coverage_complete is True
    assert not result.complete


def test_stability_proof_for_different_observed_identity_set_is_rejected() -> None:
    rows = [obs("g-p-a"), obs("g-p-b")]
    wrong = proof([obs("g-p-a")])
    with pytest.raises(ProjectCompletenessError, match="does not match the observed Project identity set"):
        evaluate_project_inventory(
            rows,
            stability_proof=wrong,
            expected=baseline(["g-p-a", "g-p-b"], exhaustive=True),
        )


def test_completeness_module_is_web_transport_independent() -> None:
    source = ((Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api") / "completeness.py").read_text(encoding="utf-8")
    assert "ChatGPTSession" not in source
    assert "querySelector" not in source
    assert ".evaluate(" not in source
    assert "shirabe_runtime" not in source


def test_acceptance_fixture_counts_are_not_hardcoded_in_production() -> None:
    root = (Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api")
    production = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("completeness.py", "projects.py", "models.py")
    )
    assert "range(25)" not in production
    assert "== 25" not in production
    assert "25 Project" not in production
    assert "pinned-7" not in production
    assert "projects-18" not in production


def test_projects_and_completeness_do_not_import_shirabe_runtime() -> None:
    root = (Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api")
    for name in ("projects.py", "completeness.py", "models.py"):
        assert "shirabe_runtime" not in (root / name).read_text(encoding="utf-8")


def test_projects_module_preserves_bind_project_public_contract() -> None:
    from chatgpt_web_api.projects import bind_project, open_project

    assert callable(bind_project)
    assert callable(open_project)
    assert bind_project is not open_project


def test_open_project_preserves_m1_web_001_timeout_keyword_contract() -> None:
    import inspect
    from chatgpt_web_api.projects import open_project

    params = inspect.signature(open_project).parameters
    assert "timeout_seconds" in params
    assert "poll_interval_seconds" in params


def test_command_observer_keeps_href_less_controls_identity_unresolved() -> None:
    from chatgpt_web_api import projects as projects_module

    source = projects_module._PROJECT_OBSERVE_SCRIPT
    assert "SHIRABE_PROJECT_OBSERVE_V1" in source
    assert "projectKey: keyFrom(href)," in source
    assert "rootUrl: rootFrom(href)," in source
    assert "if (!projectKey) continue;" in source
    assert "hrefMatch ? hrefMatch[1] : currentProjectKey" not in source
    assert "projectKey || currentProjectKey" not in source


def test_completeness_integration_rejects_unaudited_exhaustive_baseline() -> None:
    rows = (obs("g-p-a"),)
    unaudited = ProjectInventoryBaseline(
        expected_project_keys=("g-p-a",),
        exhaustive=True,
        baseline_id="baseline-without-provenance",
    )
    with pytest.raises(
        ValueError,
        match="requires baseline_id, identity_set_digest, authority, and source",
    ):
        build_project_inventory_result(
            rows,
            stability_proof=proof(rows),
            expected=unaudited,
        )


def test_completeness_integration_rejects_exhaustive_baseline_digest_mismatch() -> None:
    rows = (obs("g-p-a"),)
    mismatched = ProjectInventoryBaseline(
        expected_project_keys=("g-p-a",),
        exhaustive=True,
        baseline_id="baseline-001",
        identity_set_digest="wrong-digest",
        authority="human-approved",
        source="published-inventory",
    )
    with pytest.raises(ValueError, match="identity_set_digest does not match"):
        build_project_inventory_result(
            rows,
            stability_proof=proof(rows),
            expected=mismatched,
        )


def test_reference_api_rejects_naked_true_stability_even_with_auditable_exhaustive_baseline() -> None:
    expected = baseline(["g-p-a"], exhaustive=True)
    with pytest.raises(ProjectCompletenessError, match="ObservationStabilityProof"):
        build_project_inventory_result(
            (obs("g-p-a"),),
            observed_stable=True,
            expected=expected,
        )


def test_completeness_integration_accepts_auditable_baseline_and_stability_proof() -> None:
    rows = (obs("g-p-a"),)
    stability = proof(rows, proof_id="proof-reference-api")
    expected = baseline(["g-p-a"], exhaustive=True)
    result = build_project_inventory_result(
        rows,
        stability_proof=stability,
        expected=expected,
    )
    assert result.complete
    assert result.baseline_id == "acceptance-baseline"
    assert result.stability_proof_id == "proof-reference-api"
    assert result.observed_identity_set_digest == stability.identity_set_digest
    assert result.expected_baseline is expected


def test_query_reference_api_carries_full_proof_basis_into_result_and_evidence(monkeypatch) -> None:
    rows = (obs("g-p-a"), obs("g-p-b"))
    stability = proof(rows, proof_id="proof-query-api")
    expected = baseline(["g-p-a", "g-p-b"], exhaustive=True)

    observed = ProjectObservationQueryResult(
        projects=rows,
        observed_stable=True,
        evidence=WebDebugEvidence(
            "projects.observe",
            (
                f"stability_proof_id={stability.proof_id}",
                f"stability_evidence_ref={stability.evidence_ref}",
            ),
        ),
        stability_proof=stability,
        observed_identity_set_digest=stability.identity_set_digest,
    )

    monkeypatch.setattr(
        "chatgpt_web_api.projects.query_project_observations",
        lambda session, **kwargs: observed,
    )

    result = query_project_inventory(
        object(),
        stability_proof=stability,
        expected=expected,
    )

    assert result.complete
    assert result.stability_proof_id == stability.proof_id
    assert result.baseline_id == expected.baseline_id
    assert result.observed_identity_set_digest == stability.identity_set_digest
    assert result.evidence is not None
    assert f"stability_proof_id={stability.proof_id}" in result.evidence.observations
    assert f"expected_baseline_id={expected.baseline_id}" in result.evidence.observations
    assert f"expected_baseline_digest={expected.identity_set_digest}" in result.evidence.observations
    assert f"expected_baseline_authority={expected.authority}" in result.evidence.observations
    assert f"expected_baseline_source={expected.source}" in result.evidence.observations
    assert "expected_exhaustive=true" in result.evidence.observations
