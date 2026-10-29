from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from chatgpt_web_api import FailureKind, WebOperationStatus, open_project
from chatgpt_web_api.models import ObservationStabilityProof, ProjectInventoryBaseline
from chatgpt_web_api.project_observation import (
    project_key_identity_set_digest,
    project_observation_identity_set_digest,
    query_project_observations,
)
from chatgpt_web_api.session import ChatGPTSession
from strict_fakes import make_bound_session


@dataclass
class FakePage:
    _url: str
    _title: str = "ChatGPT"
    _target_id: str | None = None
    focused: bool = False
    observations: list[dict[str, Any]] | None = None
    query_projects: list[dict[str, Any]] | None = None
    goto_error: Exception | None = None
    query_evaluate_count: int = 0

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return self._title

    def target_id(self) -> str | None:
        return self._target_id

    def evaluate(self, expression: str, arg: Any | None = None) -> Any:
        if "document.visibilityState" in expression:
            return self.focused
        if "SHIRABE_PROJECT_OBSERVE_V1" in expression:
            assert self.observations is not None
            if len(self.observations) > 1:
                return self.observations.pop(0)
            return self.observations[0]
        if "SHIRABE_PROJECT_QUERY_V1" in expression:
            self.query_evaluate_count += 1
            assert self.query_projects is not None
            return {"url": self._url, "projects": list(self.query_projects)}
        raise AssertionError(f"unexpected evaluate script: {expression[:80]}")

    def goto(self, url: str, *, timeout_ms: int) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self._url = url


class FakeTransport:
    def __init__(self, pages: list[FakePage], connect_error: Exception | None = None) -> None:
        self._pages = pages
        self.connect_error = connect_error
        self.connected_url: str | None = None
        self.closed = False

    def connect(self, cdp_url: str) -> None:
        if self.connect_error:
            raise self.connect_error
        self.connected_url = cdp_url

    def pages(self) -> tuple[FakePage, ...]:
        return tuple(self._pages)

    def close(self) -> None:
        self.closed = True


def obs(url: str, name: str, key: str, *, current: bool = True, count: int = 1) -> dict[str, Any]:
    candidate = {
        "name": name,
        "href": f"https://chatgpt.com/g/{key}-spk-shirabe",
        "projectKey": key,
        "rootUrl": f"https://chatgpt.com/g/{key}-spk-shirabe",
        "tag": "A",
        "aria": f"Open project home {name}",
        "text": name,
    }
    return {
        "url": url,
        "title": "ChatGPT",
        "currentProjectKey": key if current else None,
        "matchingCandidateCount": count,
        "currentMatchingCandidateCount": 1 if current else 0,
        "candidates": [candidate] if count else [],
    }


def test_session_binding_rejects_unregistered_pages_even_when_one_is_focused() -> None:
    """L01 TGT-08: focused state cannot issue a Binding or choose a page."""
    pages = [
        FakePage("https://example.com"),
        FakePage("https://chatgpt.com/", _target_id="a"),
        FakePage("https://chatgpt.com/g/g-p-123/c/x", _target_id="b", focused=True),
    ]
    session = ChatGPTSession(transport=FakeTransport(pages))
    result = session.bind_chatgpt_page()
    assert not result.ok
    assert result.failure == FailureKind.SESSION_BINDING_FAILURE
    assert session._bound_page() is None


def test_session_binding_rejects_multiple_pages_instead_of_pattern_last() -> None:
    """L01 TGT-21: complete fresh page-set violation denies both candidates."""
    pages = [
        FakePage("https://chatgpt.com/", _target_id="a"),
        FakePage("https://chatgpt.com/g/g-p-123/c/x", _target_id="b"),
    ]
    session = make_bound_session(transport=FakeTransport(pages))
    result = session.bind_chatgpt_page()
    assert not result.ok
    assert result.failure == FailureKind.AMBIGUOUS_OBSERVATION
    assert session.fixture_transport.primitive_commands == []


def test_session_binding_fails_when_no_binding_exists() -> None:
    session = ChatGPTSession(transport=FakeTransport([FakePage("https://example.com/")]))
    result = session.bind_chatgpt_page()
    assert not result.ok
    assert result.failure == FailureKind.SESSION_BINDING_FAILURE


def test_open_project_returns_opened_when_current_identity_is_verified() -> None:
    key = "g-p-6a747415c2488191a48069a54688d62f"
    url = f"https://chatgpt.com/g/{key}-spk-shirabe/c/conv"
    page = FakePage(url, _target_id="t1", focused=True, observations=[obs(url, "SPK-SHIRABE", key)])
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", project_key=key, timeout_seconds=0)
    assert result.ok
    assert result.status == WebOperationStatus.OPENED
    assert result.project is not None
    assert result.project.project_key == key
    assert result.project.matched_name == "SPK-SHIRABE"


def test_open_project_navigates_then_requires_identity_postcondition() -> None:
    key = "g-p-6a747415c2488191a48069a54688d62f"
    start = "https://chatgpt.com/"
    root = f"https://chatgpt.com/g/{key}-spk-shirabe"
    before = obs(start, "SPK-SHIRABE", key, current=False)
    after = obs(root, "SPK-SHIRABE", key, current=True)
    page = FakePage(start, _target_id="t1", focused=True, observations=[before, after])
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", timeout_seconds=0.01, poll_interval_seconds=0.001)
    assert result.ok
    assert page.url == root
    assert result.evidence is not None
    assert "postcondition=target-project-identity-established" in result.evidence.observations


def test_open_project_does_not_claim_success_after_navigation_if_identity_mismatches() -> None:
    requested_key = "g-p-requested"
    wrong_key = "g-p-wrong"
    start = "https://chatgpt.com/"
    before = obs(start, "SPK-SHIRABE", requested_key, current=False)
    after = obs(f"https://chatgpt.com/g/{wrong_key}", "SPK-SHIRABE", wrong_key, current=True)
    page = FakePage(start, focused=True, observations=[before, after])
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", project_key=requested_key, timeout_seconds=0)
    assert not result.ok
    assert result.status == WebOperationStatus.FAILED
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH
    assert result.project is None


def test_open_project_fails_closed_on_ambiguous_candidates() -> None:
    page = FakePage(
        "https://chatgpt.com/",
        focused=True,
        observations=[{
            "url": "https://chatgpt.com/",
            "title": "ChatGPT",
            "currentProjectKey": None,
            "matchingCandidateCount": 2,
            "currentMatchingCandidateCount": 0,
            "candidates": [
                {"name": "SPK-SHIRABE", "projectKey": "g-p-a", "rootUrl": "https://chatgpt.com/g/g-p-a"},
                {"name": "SPK-SHIRABE", "projectKey": "g-p-b", "rootUrl": "https://chatgpt.com/g/g-p-b"},
            ],
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.AMBIGUOUS_OBSERVATION


def test_open_project_with_wrong_requested_key_never_uses_visible_name_only() -> None:
    current_key = "g-p-current"
    url = f"https://chatgpt.com/g/{current_key}"
    page = FakePage(url, focused=True, observations=[obs(url, "SPK-SHIRABE", current_key)])
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", project_key="g-p-expected", timeout_seconds=0)
    assert not result.ok
    assert result.project is None



def test_open_project_deduplicates_multiple_dom_nodes_for_same_current_project_key() -> None:
    key = "g-p-regression"
    url = f"https://chatgpt.com/g/{key}-spk-shirabe/c/conv"
    duplicate = {
        "name": "SPK-SHIRABE",
        "href": f"https://chatgpt.com/g/{key}-spk-shirabe",
        "projectKey": key,
        "rootUrl": f"https://chatgpt.com/g/{key}-spk-shirabe",
        "tag": "A",
        "aria": "Open project home SPK-SHIRABE",
        "text": "SPK-SHIRABE",
    }
    page = FakePage(
        url,
        focused=True,
        observations=[{
            "url": url,
            "title": "ChatGPT",
            "currentProjectKey": key,
            "matchingCandidateCount": 7,
            "currentMatchingCandidateCount": 7,
            "candidates": [dict(duplicate) for _ in range(7)],
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", project_key=key, timeout_seconds=0)
    assert result.ok
    assert result.project is not None and result.project.project_key == key
    assert result.evidence is not None
    assert "before.candidate_count=1" in result.evidence.observations
    assert "before.current_candidate_count=1" in result.evidence.observations
    assert "before.raw_candidate_count=7" in result.evidence.observations
    assert "before.raw_current_candidate_count=7" in result.evidence.observations


def test_open_project_deduplicates_same_project_key_even_when_dom_urls_differ() -> None:
    key = "g-p-regression"
    current = f"https://chatgpt.com/g/{key}-spk-shirabe/c/current"
    root = f"https://chatgpt.com/g/{key}-spk-shirabe"
    page = FakePage(
        current,
        focused=True,
        observations=[{
            "url": current,
            "title": "ChatGPT",
            "currentProjectKey": key,
            "matchingCandidateCount": 2,
            "currentMatchingCandidateCount": 2,
            "candidates": [
                {"name": "SPK-SHIRABE", "projectKey": key, "rootUrl": root, "href": root},
                {"name": "SPK-SHIRABE", "projectKey": key, "href": f"{root}/c/other"},
            ],
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", project_key=key, timeout_seconds=0)
    assert result.ok
    assert result.project is not None and result.project.project_key == key


def test_open_project_exact_key_does_not_collapse_different_logical_projects() -> None:
    current_key = "g-p-current"
    requested_key = "g-p-requested"
    url = f"https://chatgpt.com/g/{current_key}-spk-shirabe/c/conv"
    page = FakePage(
        url,
        focused=True,
        observations=[{
            "url": url,
            "title": "ChatGPT",
            "currentProjectKey": current_key,
            "matchingCandidateCount": 2,
            "currentMatchingCandidateCount": 1,
            "candidates": [
                {"name": "SPK-SHIRABE", "projectKey": current_key, "rootUrl": f"https://chatgpt.com/g/{current_key}-spk-shirabe"},
                {"name": "SPK-SHIRABE", "projectKey": requested_key, "rootUrl": f"https://chatgpt.com/g/{requested_key}-spk-shirabe"},
            ],
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = open_project(session, "SPK-SHIRABE", project_key="g-p-other", timeout_seconds=0)
    assert not result.ok
    assert result.project is None


def test_project_observer_does_not_assign_current_route_key_to_href_less_buttons() -> None:
    from chatgpt_web_api import projects as projects_module

    source = projects_module._PROJECT_OBSERVE_SCRIPT
    assert "projectKey: keyFrom(href)," in source
    assert "rootUrl: rootFrom(href)," in source
    assert "keyFrom(href || location.href)" not in source
    assert "rootFrom(href || location.href)" not in source

def test_public_modules_do_not_import_shirabe_runtime() -> None:
    from pathlib import Path

    root = (Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api")
    for name in ("models.py", "session.py", "project_observation.py", "projects.py", "__init__.py"):
        assert "shirabe_runtime" not in (root / name).read_text(encoding="utf-8")


def _inventory_rows(*keys: str) -> list[dict[str, Any]]:
    return [
        {
            "name": f"Project {index}",
            "projectKey": key,
            "url": f"https://chatgpt.com/g/{key}-project-{index}",
            "source": "projects-section-anchor",
        }
        for index, key in enumerate(keys, start=1)
    ]


def _proof_for_rows(rows: list[dict[str, Any]], *, stable: bool = True) -> ObservationStabilityProof:
    from chatgpt_web_api.models import ProjectObservation

    observations = tuple(
        ProjectObservation(
            name=str(row["name"]),
            project_key=str(row["projectKey"]),
            url=str(row["url"]),
            source=str(row["source"]),
        )
        for row in rows
    )
    return ObservationStabilityProof(
        proof_id="stability-proof-001",
        sample_count=3,
        first_observed_at="2026-08-13T10:00:00+09:00",
        last_observed_at="2026-08-13T10:00:02+09:00",
        identity_set_digest=project_observation_identity_set_digest(observations),
        strategy_id="repeat-until-identity-set-converges/v1",
        stable=stable,
        evidence_ref="evidence://project-inventory/stability-proof-001.json",
    )


def _auditable_baseline(*keys: str, exhaustive: bool = True) -> ProjectInventoryBaseline:
    return ProjectInventoryBaseline(
        expected_project_keys=tuple(keys),
        exhaustive=exhaustive,
        baseline_id="published-project-inventory-20260813",
        identity_set_digest=project_key_identity_set_digest(keys),
        authority="human-approved",
        source="docs/canonical/project_inventory.json",
    )


def test_observation_stability_proof_rejects_missing_provenance() -> None:
    with pytest.raises(ValueError, match="evidence_ref"):
        ObservationStabilityProof(
            proof_id="proof",
            sample_count=2,
            first_observed_at="2026-08-13T10:00:00+09:00",
            last_observed_at="2026-08-13T10:00:01+09:00",
            identity_set_digest="abc",
            strategy_id="stable-set/v1",
            stable=True,
            evidence_ref="",
        )


def test_exhaustive_baseline_model_carries_audit_identity_digest_authority_and_source() -> None:
    baseline = _auditable_baseline("g-p-a", "g-p-b")
    assert baseline.exhaustive
    assert baseline.audit_metadata_complete
    assert baseline.baseline_id == "published-project-inventory-20260813"
    assert baseline.identity_set_digest == project_key_identity_set_digest(("g-p-a", "g-p-b"))
    assert baseline.authority == "human-approved"
    assert baseline.source == "docs/canonical/project_inventory.json"


def test_project_observation_query_rejects_naked_true_stability_before_web_observation() -> None:
    page = FakePage("https://chatgpt.com/", focused=True, query_projects=_inventory_rows("g-p-a"))
    session = make_bound_session(transport=FakeTransport([page]))

    with pytest.raises(ValueError, match="ObservationStabilityProof"):
        query_project_observations(session, observed_stable=True)

    assert page.query_evaluate_count == 0


def test_project_observation_query_carries_stability_provenance() -> None:
    rows = _inventory_rows("g-p-a", "g-p-b")
    proof = _proof_for_rows(rows)
    page = FakePage("https://chatgpt.com/", focused=True, query_projects=rows)
    session = make_bound_session(transport=FakeTransport([page]))

    result = query_project_observations(
        session,
        stability_proof=proof,
    )

    assert result.ok
    assert result.stability_proof is proof
    assert result.stability_proof_id == proof.proof_id
    assert result.observed_identity_set_digest == proof.identity_set_digest
    assert result.observed_stable is True
    assert result.evidence is not None
    assert f"stability_proof_id={proof.proof_id}" in result.evidence.observations
    assert f"stability_evidence_ref={proof.evidence_ref}" in result.evidence.observations

def test_project_observation_query_rejects_stability_proof_for_a_different_identity_set() -> None:
    rows = _inventory_rows("g-p-a", "g-p-b")
    wrong_proof = _proof_for_rows(_inventory_rows("g-p-a"))
    page = FakePage("https://chatgpt.com/", focused=True, query_projects=rows)
    session = make_bound_session(transport=FakeTransport([page]))

    with pytest.raises(ValueError, match="does not match the observed Project identity set"):
        query_project_observations(session, stability_proof=wrong_proof)

    assert page.query_evaluate_count == 1
