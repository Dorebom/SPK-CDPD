from __future__ import annotations

from collections import Counter
import hashlib
from typing import Iterable

from .models import (
    ObservationStabilityProof,
    ProjectInventoryBaseline,
    ProjectInventoryCompleteness,
    ProjectObservation,
)


class ProjectCompletenessError(ValueError):
    pass


def evaluate_project_inventory(
    observations: Iterable[ProjectObservation],
    *,
    observed_stable: bool | None = None,
    stability_proof: ObservationStabilityProof | None = None,
    expected: ProjectInventoryBaseline | None = None,
) -> ProjectInventoryCompleteness:
    """Evaluate Project coverage and completeness from explicit proof inputs.

    ``observed_stable`` is retained as a compatibility input for an explicit
    negative/unknown observation.  A truthy stability claim is not accepted as
    a completeness proof without ``ObservationStabilityProof``.  Full inventory
    verification requires both an auditable exhaustive baseline and a matching
    stability proof for the observed logical identity set.
    """

    rows = tuple(observations)
    stable = _validated_stability(rows, observed_stable, stability_proof)

    resolved_keys = tuple(
        row.project_key.strip()
        for row in rows
        if isinstance(row.project_key, str) and row.project_key.strip()
    )
    resolved_count = len(resolved_keys)
    all_observed_resolved = resolved_count == len(rows)

    key_counts = Counter(resolved_keys)
    duplicate_keys = tuple(sorted(key for key, count in key_counts.items() if count > 1))
    resolved_set = set(resolved_keys)

    if expected is None:
        return ProjectInventoryCompleteness(
            observed_count=len(rows),
            resolved_count=resolved_count,
            observed_stable=stable,
            all_observed_resolved=all_observed_resolved,
            expected_identity_count=None,
            known_identity_coverage_complete=None,
            duplicate_project_keys=duplicate_keys,
            proof_exhaustive=False,
            full_inventory_verified=False,
        )

    expected_keys = _validated_expected_keys(expected)
    expected_set = set(expected_keys)
    missing = tuple(sorted(expected_set - resolved_set))
    unexpected = tuple(sorted(resolved_set - expected_set))
    known_coverage = not missing

    full = bool(
        expected.exhaustive
        and stability_proof is not None
        and stable
        and all_observed_resolved
        and not duplicate_keys
        and not missing
        and not unexpected
        and len(rows) == len(expected_keys)
    )

    return ProjectInventoryCompleteness(
        observed_count=len(rows),
        resolved_count=resolved_count,
        observed_stable=stable,
        all_observed_resolved=all_observed_resolved,
        expected_identity_count=len(expected_keys),
        known_identity_coverage_complete=known_coverage,
        missing_expected_project_keys=missing,
        unexpected_project_keys=unexpected,
        duplicate_project_keys=duplicate_keys,
        proof_exhaustive=bool(expected.exhaustive),
        full_inventory_verified=full,
    )


def _validated_stability(
    rows: tuple[ProjectObservation, ...],
    observed_stable: bool | None,
    stability_proof: ObservationStabilityProof | None,
) -> bool:
    if stability_proof is None:
        if observed_stable is True:
            raise ProjectCompletenessError(
                "observed_stable=True is not an auditable completeness proof; "
                "supply ObservationStabilityProof"
            )
        return False

    if observed_stable is not None and bool(observed_stable) != bool(stability_proof.stable):
        raise ProjectCompletenessError(
            "observed_stable conflicts with stability_proof.stable"
        )

    observed_digest = _project_observation_identity_set_digest(rows)
    if stability_proof.identity_set_digest != observed_digest:
        raise ProjectCompletenessError(
            "stability_proof.identity_set_digest does not match the observed Project identity set"
        )
    return bool(stability_proof.stable)


def _validated_expected_keys(expected: ProjectInventoryBaseline) -> tuple[str, ...]:
    if expected.baseline_id is not None and not expected.baseline_id.strip():
        raise ProjectCompletenessError("baseline_id must be non-empty when supplied")

    normalized: list[str] = []
    for value in expected.expected_project_keys:
        if not isinstance(value, str) or not value.strip():
            raise ProjectCompletenessError("expected_project_keys must contain non-empty strings")
        normalized.append(value.strip())

    if len(normalized) != len(set(normalized)):
        raise ProjectCompletenessError("expected_project_keys contains duplicate identities")
    if expected.exhaustive and not normalized:
        raise ProjectCompletenessError("an exhaustive Project baseline must not be empty")

    if expected.exhaustive:
        if not expected.audit_metadata_complete:
            raise ProjectCompletenessError(
                "an exhaustive Project inventory baseline requires baseline_id, "
                "identity_set_digest, authority, and source"
            )
        actual_digest = _project_key_identity_set_digest(normalized)
        if expected.identity_set_digest != actual_digest:
            raise ProjectCompletenessError(
                "exhaustive Project inventory baseline identity_set_digest does not "
                "match expected_project_keys"
            )

    return tuple(normalized)


def _digest_identity_tokens(tokens: Iterable[str]) -> str:
    normalized = sorted({str(token).strip() for token in tokens if str(token).strip()})
    payload = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _project_key_identity_set_digest(project_keys: Iterable[str]) -> str:
    return _digest_identity_tokens(
        f"key:{str(project_key).strip()}"
        for project_key in project_keys
        if str(project_key).strip()
    )


def _project_observation_identity_set_digest(
    observations: Iterable[ProjectObservation],
) -> str:
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
