from __future__ import annotations

import hashlib
from typing import Iterable

from .models import (
    FailureKind,
    ObservationStabilityProof,
    ProjectObservation,
    ProjectObservationQueryResult,
    WebDebugEvidence,
)
from .session import ChatGPTSession, _identity_of


# M1-WEBFIX-007 Project observation / proof foundation.
# This module must remain independent of M1-WEB-006 completeness semantics.
# Project inventory observation script; intentionally distinct from the
# M1-WEB-001 command observer in projects.py.
_PROJECT_QUERY_SCRIPT = r"""
() => {
  /* SHIRABE_PROJECT_QUERY_V1 */
  const rows = [];
  const seen = new Set();
  const nodes = [...document.querySelectorAll('a[href*="/g/g-p-"]')];
  for (const node of nodes) {
    const href = String(node.href || node.getAttribute('href') || '');
    const match = href.match(/\/g\/(g-p-[A-Za-z0-9]+)(?:-|\/|$)/);
    if (!match) continue;
    const projectKey = match[1];
    const raw = String(
      node.getAttribute('aria-label')
      || node.innerText
      || node.textContent
      || ''
    ).trim();
    let name = raw;
    const chatProject = raw.match(/,\s*chat in project\s+(.+)$/i);
    if (chatProject) name = chatProject[1].trim();
    const key = `${projectKey}\u0000${name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({name, projectKey, url: href, source: 'projects-section-anchor'});
  }
  return {url: location.href, projects: rows};
}
"""


def _digest_identity_tokens(tokens: Iterable[str]) -> str:
    normalized = sorted({str(token).strip() for token in tokens if str(token).strip()})
    payload = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def project_key_identity_set_digest(project_keys: Iterable[str]) -> str:
    """Return the canonical SHA-256 identity-set digest for durable Project keys."""

    return _digest_identity_tokens(
        f"key:{str(project_key).strip()}"
        for project_key in project_keys
        if str(project_key).strip()
    )


def project_observation_identity_set_digest(
    observations: Iterable[ProjectObservation],
) -> str:
    """Digest the observed logical identity set without discarding unresolved rows."""

    tokens: list[str] = []
    for row in observations:
        if isinstance(row.project_key, str) and row.project_key.strip():
            tokens.append(f"key:{row.project_key.strip()}")
        else:
            tokens.append(
                "unresolved:"
                + str(row.name or "").strip()
                + "\0"
                + str(row.url or "").strip()
            )
    return _digest_identity_tokens(tokens)


def _resolve_stability_for_observations(
    rows: tuple[ProjectObservation, ...],
    *,
    stability_proof: ObservationStabilityProof | None,
    observed_stable: bool | None,
) -> tuple[bool, str]:
    observed_digest = project_observation_identity_set_digest(rows)
    if stability_proof is None:
        if observed_stable is True:
            raise ValueError(
                "observed_stable=True is not an auditable stability assertion; "
                "supply ObservationStabilityProof"
            )
        return False, observed_digest

    if observed_stable is not None and bool(observed_stable) != bool(stability_proof.stable):
        raise ValueError("observed_stable conflicts with stability_proof.stable")
    if stability_proof.identity_set_digest != observed_digest:
        raise ValueError(
            "stability_proof.identity_set_digest does not match the observed Project identity set"
        )
    return bool(stability_proof.stable), observed_digest


def query_project_observations(
    session: ChatGPTSession,
    *,
    stability_proof: ObservationStabilityProof | None = None,
    observed_stable: bool | None = None,
) -> ProjectObservationQueryResult:
    """Observe Project identities without making a completeness claim.

    This is the M1-WEBFIX-007 foundation boundary.  A truthy stability claim is
    accepted only through an auditable ``ObservationStabilityProof`` whose
    identity-set digest matches the actual Web observation.
    """

    if stability_proof is None and observed_stable is True:
        raise ValueError(
            "observed_stable=True is not an auditable stability assertion; "
            "supply ObservationStabilityProof"
        )
    if (
        stability_proof is not None
        and observed_stable is not None
        and bool(observed_stable) != bool(stability_proof.stable)
    ):
        raise ValueError("observed_stable conflicts with stability_proof.stable")

    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return ProjectObservationQueryResult(
                projects=(),
                observed_stable=False,
                page=bind.page,
                evidence=bind.evidence,
                failure=bind.failure or FailureKind.SESSION_BINDING_FAILURE,
                reason=bind.reason or "ChatGPT page binding failed.",
                stability_proof=stability_proof,
                observed_identity_set_digest=project_observation_identity_set_digest(()),
            )
        bound = session._bound_page()

    if bound is None:
        return ProjectObservationQueryResult(
            projects=(),
            observed_stable=False,
            failure=FailureKind.SESSION_BINDING_FAILURE,
            reason="Session reported bound but no internal page exists.",
            evidence=WebDebugEvidence("projects.observe", ("bound-page=missing",)),
            stability_proof=stability_proof,
            observed_identity_set_digest=project_observation_identity_set_digest(()),
        )

    try:
        raw = bound.ops.evaluate(_PROJECT_QUERY_SCRIPT)
    except Exception as exc:
        return ProjectObservationQueryResult(
            projects=(),
            observed_stable=False,
            page=_identity_of(bound.ops),
            failure=FailureKind.UNKNOWN_WEB_STATE,
            reason=f"Project observation failed: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence("projects.observe", ("observation=exception",)),
            stability_proof=stability_proof,
            observed_identity_set_digest=project_observation_identity_set_digest(()),
        )

    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
        return ProjectObservationQueryResult(
            projects=(),
            observed_stable=False,
            page=_identity_of(bound.ops),
            failure=FailureKind.UNKNOWN_WEB_STATE,
            reason="Project observation payload is invalid.",
            evidence=WebDebugEvidence("projects.observe", ("observation=invalid-payload",)),
            stability_proof=stability_proof,
            observed_identity_set_digest=project_observation_identity_set_digest(()),
        )

    rows: list[ProjectObservation] = []
    for item in raw["projects"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        key = str(item.get("projectKey") or "").strip() or None
        url = str(item.get("url") or item.get("rootUrl") or "").strip() or None
        rows.append(
            ProjectObservation(
                name=name,
                project_key=key,
                url=url,
                source=str(item.get("source") or "web"),
            )
        )

    row_tuple = tuple(rows)
    stable, observed_digest = _resolve_stability_for_observations(
        row_tuple,
        stability_proof=stability_proof,
        observed_stable=observed_stable,
    )
    evidence = WebDebugEvidence(
        "projects.observe",
        (
            f"observed_project_count={len(rows)}",
            f"observed_stable={str(stable).lower()}",
            f"observed_identity_set_digest={observed_digest}",
            f"resolved_project_count={sum(1 for row in rows if row.resolved)}",
            f"stability_proof_id={stability_proof.proof_id if stability_proof else 'absent'}",
            f"stability_strategy={stability_proof.strategy_id if stability_proof else 'absent'}",
            f"stability_evidence_ref={stability_proof.evidence_ref if stability_proof else 'absent'}",
        ),
    )
    return ProjectObservationQueryResult(
        projects=row_tuple,
        observed_stable=stable,
        page=_identity_of(bound.ops),
        evidence=evidence,
        stability_proof=stability_proof,
        observed_identity_set_digest=observed_digest,
    )


