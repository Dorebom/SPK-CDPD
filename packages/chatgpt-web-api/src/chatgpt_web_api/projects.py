from __future__ import annotations

import re
import time
from typing import Iterable

from .completeness import evaluate_project_inventory
from .models import (
    FailureKind,
    ProjectIdentity,
    ObservationStabilityProof,
    ProjectInventoryBaseline,
    ProjectInventoryQueryResult,
    ProjectObservation,
    ProjectOpenResult,
    WebDebugEvidence,
    WebOperationStatus,
)
from .session import ChatGPTSession, _identity_of


_PROJECT_ROUTE_RE = re.compile(r"/g/(g-p-[A-Za-z0-9]+)(?:-|/|$)")

# M1-WEB-001 compatibility contract.
#
# Important: href-less/button-like controls MUST NOT inherit the current route
# project key. A durable Project identity comes from the candidate's own href
# (or another candidate-owned durable attribute), never from page context.
_PROJECT_OBSERVE_SCRIPT = r"""
() => {
  /* SHIRABE_PROJECT_OBSERVE_V1 */
  const keyFrom = (href) => {
    const match = String(href || '').match(/\/g\/(g-p-[A-Za-z0-9]+)(?:-|\/|$)/);
    return match ? match[1] : null;
  };
  const rootFrom = (href) => {
    const match = String(href || '').match(/^(https:\/\/chatgpt\.com\/g\/g-p-[^/?#]+)/);
    return match ? match[1] : (href || null);
  };
  const currentProjectKey = keyFrom(location.href);
  const nodes = [...document.querySelectorAll(
    'a[href*="/g/g-p-"], button, [role="button"]'
  )];
  const candidates = [];
  for (const node of nodes) {
    const href = String(node.href || node.getAttribute('href') || '');
    const projectKey = keyFrom(href);
    const aria = String(node.getAttribute('aria-label') || '').trim();
    const text = String(node.innerText || node.textContent || '').trim();
    const rawName = aria || text;
    let name = rawName;
    const chatProject = rawName.match(/,\s*chat in project\s+(.+)$/i);
    if (chatProject) name = chatProject[1].trim();

    // Do not fabricate a candidate Project key for href-less controls.
    if (!projectKey) continue;

    candidates.push({
      name,
      href: href || null,
      projectKey: keyFrom(href),
      rootUrl: rootFrom(href),
      tag: String(node.tagName || ''),
      aria,
      text
    });
  }
  return {
    url: location.href,
    title: document.title || null,
    currentProjectKey,
    matchingCandidateCount: candidates.length,
    currentMatchingCandidateCount: candidates.filter(c => c.projectKey === currentProjectKey).length,
    candidates
  };
}
"""

from .project_observation import (
    project_key_identity_set_digest,
    project_observation_identity_set_digest,
    query_project_observations,
)

# M1-WEB-006 completeness integration.
def _expected_baseline_digest(expected: ProjectInventoryBaseline) -> str:
    return project_key_identity_set_digest(expected.expected_project_keys)


def _validate_exhaustive_baseline_for_query(
    expected: ProjectInventoryBaseline | None,
) -> None:
    if expected is None or not expected.exhaustive:
        return
    if not expected.audit_metadata_complete:
        raise ValueError(
            "an exhaustive Project inventory baseline requires baseline_id, "
            "identity_set_digest, authority, and source"
        )
    actual = _expected_baseline_digest(expected)
    if expected.identity_set_digest != actual:
        raise ValueError(
            "exhaustive Project inventory baseline identity_set_digest does not "
            "match expected_project_keys"
        )


def build_project_inventory_result(
    observations: Iterable[ProjectObservation],
    *,
    observed_stable: bool | None = None,
    stability_proof: ObservationStabilityProof | None = None,
    expected: ProjectInventoryBaseline | None = None,
    page=None,
    evidence: WebDebugEvidence | None = None,
) -> ProjectInventoryQueryResult:
    """Join Project observations to M1-WEB-006 completeness semantics."""

    rows = tuple(observations)
    _validate_exhaustive_baseline_for_query(expected)
    observed_digest = project_observation_identity_set_digest(rows)
    completeness = evaluate_project_inventory(
        rows,
        observed_stable=observed_stable,
        stability_proof=stability_proof,
        expected=expected,
    )
    return ProjectInventoryQueryResult(
        projects=rows,
        completeness=completeness,
        page=page,
        evidence=evidence,
        stability_proof=stability_proof,
        expected_baseline=expected,
        observed_identity_set_digest=observed_digest,
    )


def query_project_inventory(
    session: ChatGPTSession,
    *,
    stability_proof: ObservationStabilityProof | None = None,
    observed_stable: bool | None = None,
    expected: ProjectInventoryBaseline | None = None,
) -> ProjectInventoryQueryResult:
    """Observe Project identities, then evaluate M1-WEB-006 completeness."""

    _validate_exhaustive_baseline_for_query(expected)
    observed = query_project_observations(
        session,
        stability_proof=stability_proof,
        observed_stable=observed_stable,
    )
    if not observed.ok:
        completeness = evaluate_project_inventory(
            (), observed_stable=False, stability_proof=None, expected=expected
        )
        return ProjectInventoryQueryResult(
            projects=(),
            completeness=completeness,
            page=observed.page,
            evidence=observed.evidence,
            failure=observed.failure,
            reason=observed.reason,
            stability_proof=observed.stability_proof,
            expected_baseline=expected,
            observed_identity_set_digest=observed.observed_identity_set_digest,
        )

    baseline_observations = (
        f"expected_baseline={'present' if expected is not None else 'absent'}",
        f"expected_baseline_id={expected.baseline_id if expected and expected.baseline_id else 'absent'}",
        f"expected_baseline_digest={expected.identity_set_digest if expected and expected.identity_set_digest else 'absent'}",
        f"expected_baseline_authority={expected.authority if expected and expected.authority else 'absent'}",
        f"expected_baseline_source={expected.source if expected and expected.source else 'absent'}",
        f"expected_exhaustive={str(bool(expected and expected.exhaustive)).lower()}",
    )
    evidence = WebDebugEvidence(
        "projects.query",
        tuple(observed.evidence.observations if observed.evidence else ())
        + baseline_observations,
    )
    return build_project_inventory_result(
        observed.projects,
        stability_proof=observed.stability_proof,
        expected=expected,
        page=observed.page,
        evidence=evidence,
    )


def bind_project(
    session: ChatGPTSession,
    requested_name: str | None = None,
    *,
    project_name: str | None = None,
    project_key: str | None = None,
    project_url: str | None = None,
    requested_url: str | None = None,
) -> ProjectOpenResult:
    """Verify that the current page is already bound to the requested Project.

    This operation never navigates.
    """

    requested_name = str(requested_name or project_name or "").strip()
    project_url = project_url or requested_url
    expected_key = str(project_key or "").strip() or _project_key_from_url(project_url)

    bound, failure = _ensure_bound_page(session, "projects.bind")
    if bound is None:
        return failure

    raw, observe_failure = _observe_project_command(bound, "projects.bind")
    if raw is None:
        return observe_failure

    current_key = _raw_current_key(raw)
    candidates = _logical_candidates(raw)

    if not current_key:
        return _failed(
            bound,
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Current ChatGPT page does not establish a Project identity.",
            "projects.bind",
            ("current_project_key=missing",),
        )

    if expected_key is not None and current_key != expected_key:
        return _failed(
            bound,
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Current Project identity does not match the requested Project.",
            "projects.bind",
            (f"expected_project_key={expected_key}", f"observed_project_key={current_key}"),
        )

    current_candidates = [c for c in candidates if c["project_key"] == current_key]
    if requested_name:
        current_name_matches = [c for c in current_candidates if c["name"] == requested_name]
        if not current_name_matches and expected_key is None:
            return _failed(
                bound,
                FailureKind.TARGET_IDENTITY_MISMATCH,
                "Requested Project name could not be verified against the current Project.",
                "projects.bind",
                (f"requested_name={requested_name}", f"observed_project_key={current_key}"),
            )
        matched_name = current_name_matches[0]["name"] if current_name_matches else requested_name
    else:
        matched_name = current_candidates[0]["name"] if current_candidates else ""

    identity = ProjectIdentity(
        requested_name=requested_name,
        matched_name=matched_name or requested_name,
        project_key=current_key,
        url=str(getattr(bound.ops, "url", "") or raw.get("url") or ""),
    )
    return ProjectOpenResult(
        WebOperationStatus.OPENED,
        identity,
        _identity_of(bound.ops),
        evidence=WebDebugEvidence(
            "projects.bind",
            ("project_identity=verified", "navigation=not-performed"),
        ),
    )


def open_project(
    session: ChatGPTSession,
    requested_name: str | None = None,
    *,
    project_name: str | None = None,
    project_key: str | None = None,
    project_url: str | None = None,
    requested_url: str | None = None,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.1,
    timeout_ms: int | None = None,
) -> ProjectOpenResult:
    """Open one Project and verify identity postconditions.

    M1-WEB-001 compatibility is preserved:
    - ``timeout_seconds`` / ``poll_interval_seconds`` remain accepted.
    - Candidate resolution uses the existing Project observer shape.
    - DOM duplicates with the same durable Project key collapse to one logical
      Project; different Project keys never collapse merely because names match.
    """

    requested_name = str(requested_name or project_name or "").strip()
    project_url = project_url or requested_url
    expected_key = str(project_key or "").strip() or _project_key_from_url(project_url)

    try:
        timeout_seconds = max(0.0, float(timeout_seconds))
        poll_interval_seconds = max(0.0, float(poll_interval_seconds))
    except (TypeError, ValueError):
        raise TypeError("timeout_seconds and poll_interval_seconds must be numeric")

    bound, failure = _ensure_bound_page(session, "projects.open")
    if bound is None:
        return failure

    before, observe_failure = _observe_project_command(bound, "projects.open")
    if before is None:
        return observe_failure

    current_key = _raw_current_key(before)
    logical = _logical_candidates(before)

    # Current-page postcondition: exact durable key wins. If no key was
    # requested, the current Project must still resolve uniquely by name.
    if expected_key is not None and current_key == expected_key:
        name = _name_for_key(logical, current_key) or requested_name
        return _opened_current(bound, requested_name, name, current_key, before, logical)

    if expected_key is None and current_key:
        current_name_candidates = [
            c for c in logical
            if c["project_key"] == current_key and (not requested_name or c["name"] == requested_name)
        ]
        if requested_name and len(current_name_candidates) == 1:
            return _opened_current(
                bound, requested_name, current_name_candidates[0]["name"], current_key, before, logical
            )

    candidates = _select_target_candidates(
        logical,
        requested_name=requested_name,
        expected_key=expected_key,
        project_url=project_url,
    )

    if len(candidates) != 1:
        failure_kind = (
            FailureKind.AMBIGUOUS_OBSERVATION
            if len(candidates) > 1 else FailureKind.TARGET_IDENTITY_MISMATCH
        )
        return _failed(
            bound,
            failure_kind,
            "Target Project could not be resolved uniquely.",
            "projects.open",
            (
                f"candidate_count={len(candidates)}",
                f"raw_candidate_count={len(before.get('candidates') or [])}",
                f"current_project_key={current_key}",
            ),
        )

    target = candidates[0]
    target_key = target["project_key"]
    target_url = target["root_url"] or target["href"]
    if not target_key or not target_url:
        return _failed(
            bound,
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Resolved Project candidate lacks durable key/url identity.",
            "projects.open",
            ("candidate_identity=incomplete",),
        )

    navigation_timeout_ms = (
        int(timeout_ms) if timeout_ms is not None
        else max(0, int(timeout_seconds * 1000))
    )
    try:
        bound.ops.goto(target_url, timeout_ms=navigation_timeout_ms)
    except Exception as exc:
        return _failed(
            bound,
            FailureKind.POSTCONDITION_NOT_ESTABLISHED,
            f"Project navigation failed: {type(exc).__name__}: {exc}",
            "projects.open",
            ("navigation=failed",),
        )

    # Always take one immediate post-navigation observation, even when
    # timeout_seconds == 0. Additional observations are bounded by deadline.
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_raw = None
    while True:
        attempt += 1
        after, observe_failure = _observe_project_command(bound, "projects.open")
        if after is None:
            return observe_failure
        last_raw = after
        after_key = _raw_current_key(after)
        if after_key == target_key:
            identity = ProjectIdentity(
                requested_name=requested_name,
                matched_name=target["name"] or requested_name,
                project_key=target_key,
                url=str(getattr(bound.ops, "url", "") or target_url),
            )
            return ProjectOpenResult(
                WebOperationStatus.OPENED,
                identity,
                _identity_of(bound.ops),
                evidence=WebDebugEvidence(
                    "projects.open",
                    _project_observation_evidence(
                        "before", before, logical, requested_name=requested_name
                    )
                    + _project_observation_evidence(
                        "after", after, _logical_candidates(after), requested_name=requested_name
                    )
                    + (
                        "navigation=completed",
                        "project_identity=verified",
                        "postcondition=target-project-identity-established",
                        f"postcondition_attempts={attempt}",
                    ),
                ),
            )

        if time.monotonic() >= deadline:
            break
        if poll_interval_seconds:
            time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))

    observed_key = _raw_current_key(last_raw or {})
    failure_kind = (
        FailureKind.TARGET_IDENTITY_MISMATCH
        if observed_key is not None and observed_key != target_key
        else FailureKind.POSTCONDITION_NOT_ESTABLISHED
    )
    return _failed(
        bound,
        failure_kind,
        "Navigation completed without establishing the target Project identity.",
        "projects.open",
        (
            f"expected_project_key={target_key}",
            f"observed_project_key={observed_key}",
            f"postcondition_attempts={attempt}",
        ),
    )


def _ensure_bound_page(session: ChatGPTSession, operation: str):
    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return None, ProjectOpenResult(
                WebOperationStatus.FAILED,
                None,
                bind.page,
                bind.failure or FailureKind.SESSION_BINDING_FAILURE,
                bind.reason or "ChatGPT page binding failed.",
                bind.evidence,
            )
        bound = session._bound_page()
    if bound is None:
        return None, ProjectOpenResult(
            WebOperationStatus.FAILED,
            None,
            None,
            FailureKind.SESSION_BINDING_FAILURE,
            "Session reported bound but no internal page exists.",
            WebDebugEvidence(operation, ("bound-page=missing",)),
        )
    return bound, None


def _observe_project_command(bound, operation: str):
    try:
        raw = bound.ops.evaluate(_PROJECT_OBSERVE_SCRIPT)
    except Exception as exc:
        return None, _failed(
            bound,
            FailureKind.UNKNOWN_WEB_STATE,
            f"Project observation failed: {type(exc).__name__}: {exc}",
            operation,
            ("observation=exception",),
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
        return None, _failed(
            bound,
            FailureKind.UNKNOWN_WEB_STATE,
            "Project observation payload is invalid.",
            operation,
            ("observation=invalid-payload",),
        )
    return raw, None


def _logical_candidates(raw: dict) -> list[dict]:
    # Durable Project key is the logical identity. Multiple DOM nodes / URLs
    # for the same key collapse; different keys never collapse by visible name.
    by_key: dict[str, dict] = {}
    for item in raw.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("projectKey") or "").strip()
        if not key:
            continue
        name = str(item.get("name") or "").strip()
        root_url = str(item.get("rootUrl") or "").strip() or None
        href = str(item.get("href") or "").strip() or None
        existing = by_key.get(key)
        candidate = {
            "project_key": key,
            "name": name,
            "root_url": root_url or _project_root_from_url(href),
            "href": href,
        }
        if existing is None:
            by_key[key] = candidate
        else:
            # Prefer a durable rootUrl and non-empty name without changing key.
            if not existing["root_url"] and candidate["root_url"]:
                existing["root_url"] = candidate["root_url"]
            if not existing["href"] and candidate["href"]:
                existing["href"] = candidate["href"]
            if not existing["name"] and candidate["name"]:
                existing["name"] = candidate["name"]
    return list(by_key.values())


def _select_target_candidates(
    logical: list[dict],
    *,
    requested_name: str,
    expected_key: str | None,
    project_url: str | None,
) -> list[dict]:
    if expected_key is not None:
        return [c for c in logical if c["project_key"] == expected_key]

    url_key = _project_key_from_url(project_url)
    if url_key is not None:
        return [c for c in logical if c["project_key"] == url_key]

    return [c for c in logical if c["name"] == requested_name]


def _raw_current_key(raw: dict) -> str | None:
    value = raw.get("currentProjectKey")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _name_for_key(logical: list[dict], key: str) -> str | None:
    for candidate in logical:
        if candidate["project_key"] == key and candidate["name"]:
            return candidate["name"]
    return None


def _project_observation_evidence(
    prefix: str,
    raw: dict,
    logical: list[dict],
    *,
    requested_name: str,
) -> tuple[str, ...]:
    current_key = _raw_current_key(raw)
    current_logical = [c for c in logical if c["project_key"] == current_key]
    matched_name = _name_for_key(logical, current_key) if current_key else None
    raw_candidates = raw.get("candidates") or []
    raw_candidate_count = raw.get("matchingCandidateCount")
    raw_current_count = raw.get("currentMatchingCandidateCount")
    if not isinstance(raw_candidate_count, int):
        raw_candidate_count = len(raw_candidates) if isinstance(raw_candidates, list) else 0
    if not isinstance(raw_current_count, int):
        raw_current_count = sum(
            1
            for item in raw_candidates
            if isinstance(item, dict)
            and str(item.get("projectKey") or "").strip() == (current_key or "")
        )
    return (
        f"{prefix}.url={str(raw.get('url') or '')}",
        f"{prefix}.current_project_key={current_key}",
        f"{prefix}.candidate_count={len(logical)}",
        f"{prefix}.current_candidate_count={len(current_logical)}",
        f"{prefix}.raw_candidate_count={raw_candidate_count}",
        f"{prefix}.raw_current_candidate_count={raw_current_count}",
        f"{prefix}.matched_name={matched_name or requested_name}",
    )


def _opened_current(
    bound,
    requested_name: str,
    matched_name: str,
    key: str,
    before: dict,
    logical: list[dict],
) -> ProjectOpenResult:
    identity = ProjectIdentity(
        requested_name=requested_name,
        matched_name=matched_name or requested_name,
        project_key=key,
        url=str(getattr(bound.ops, "url", "") or ""),
    )
    return ProjectOpenResult(
        WebOperationStatus.OPENED,
        identity,
        _identity_of(bound.ops),
        evidence=WebDebugEvidence(
            "projects.open",
            _project_observation_evidence(
                "before", before, logical, requested_name=requested_name
            )
            + ("postcondition=already-established",),
        ),
    )

def _failed(bound, kind: FailureKind, reason: str, operation: str, observations: tuple[str, ...]):
    return ProjectOpenResult(
        WebOperationStatus.FAILED,
        None,
        _identity_of(bound.ops) if bound is not None else None,
        kind,
        reason,
        WebDebugEvidence(operation, observations),
    )


def _project_key_from_url(url: str | None) -> str | None:
    match = _PROJECT_ROUTE_RE.search(str(url or ""))
    return match.group(1) if match else None


def _project_root_from_url(url: str | None) -> str | None:
    text = str(url or "")
    match = re.match(r"^(https://chatgpt\.com/g/g-p-[^/?#]+)", text)
    return match.group(1) if match else (text or None)


# Public reference aliases. They intentionally preserve one completeness
# interpretation rather than creating alternate meanings.
query_projects = query_project_inventory
evaluate_project_query = build_project_inventory_result
