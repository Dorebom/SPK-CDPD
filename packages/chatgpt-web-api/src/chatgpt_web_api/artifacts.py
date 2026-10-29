from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .messages import _normalize_semantic_text, _route_identity, _sha256
from .models import (
    ArtifactCandidate,
    ArtifactDownloadResult,
    ArtifactMetadata,
    AssistantResponse,
    FailureKind,
    MessageIdentity,
    MessageRole,
    PageIdentity,
    WebDebugEvidence,
    WebOperationEvidenceRecord,
    WebOperationStatus,
)
from .session import ChatGPTSession, _identity_of


_ARTIFACT_DISCOVERY_SCRIPT = r"""
() => {
  /* SHIRABE_ARTIFACT_DISCOVERY_V1 */
  const articleTurns = [...document.querySelectorAll(
    'article[data-testid^="conversation-turn-"], article[data-message-author-role]'
  )];
  const roleTurns = articleTurns.length ? [] : [...document.querySelectorAll('[data-message-author-role]')];
  const unique = [...new Set(articleTurns.length ? articleTurns : roleTurns)];
  const assistantRows = [];
  let assistantIndex = 0;

  const visible = (node) => {
    if (!(node instanceof Element) || !node.isConnected) return false;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    if (node.getAttribute('hidden') !== null || node.getAttribute('aria-hidden') === 'true') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const artifactContainerSelector = [
    '[data-testid*="artifact" i]',
    '[data-testid*="file" i]',
    '[data-testid*="attachment" i]',
    '[class*="artifact" i]',
    '[class*="attachment" i]',
    '[class*="file-card" i]',
    '[class*="file_card" i]'
  ].join(',');

  for (const node of unique) {
    const roleNode = node.matches('[data-message-author-role]')
      ? node
      : node.querySelector('[data-message-author-role]');
    let role = roleNode ? roleNode.getAttribute('data-message-author-role') : null;
    role = String(role || '').toLowerCase();
    if (role !== 'assistant') continue;

    const body = node.querySelector('.markdown, [data-message-author-role] .markdown, [data-message-content]') || roleNode || node;
    let text = String(body.innerText || body.textContent || '');
    text = text.replace(/\u200B/g, '').trim();
    const rawId = node.getAttribute('data-message-id')
      || (roleNode && roleNode.getAttribute('data-message-id'))
      || null;

    const controlNodes = [...new Set([
      ...node.querySelectorAll('a[href], a[download], button, [role="button"]')
    ])];
    const containerIds = new Map();
    let nextContainerId = 0;
    const controls = [];

    for (let i = 0; i < controlNodes.length; i++) {
      const c = controlNodes[i];
      const ownHref = c.href || c.getAttribute('href') || null;
      const download = c.getAttribute('download') || null;
      const label = String(c.getAttribute('aria-label') || '');
      const title = String(c.getAttribute('title') || '');
      const ctext = String(c.innerText || c.textContent || '').trim();
      const dataTestId = c.getAttribute('data-testid') || null;
      const controlRole = c.getAttribute('role') || null;

      let container = c.closest(artifactContainerSelector);
      let containerArtifactHint = !!container;
      if (!container) {
        const ownAnchor = c.closest('a[href], a[download]');
        if (ownAnchor && node.contains(ownAnchor)) {
          container = ownAnchor;
          containerArtifactHint = true;
        }
      }
      if (!container) {
        let probe = c.parentElement;
        let depth = 0;
        while (probe && probe !== node && depth < 5) {
          const probeText = String(probe.innerText || probe.textContent || '').replace(/\u200B/g, '').trim();
          if (/\.zip(?:\s|$|[)\]}>.,;])/i.test(probeText) && probe.querySelector('a[href]')) {
            container = probe;
            containerArtifactHint = true;
            break;
          }
          probe = probe.parentElement;
          depth += 1;
        }
      }
      if (!container) container = c.parentElement && node.contains(c.parentElement) ? c.parentElement : null;

      let containerKey = null;
      if (container) {
        if (!containerIds.has(container)) containerIds.set(container, nextContainerId++);
        containerKey = `response-container-${containerIds.get(container)}`;
      }

      const relatedHrefNodes = [];
      if (c.matches('a[href]')) relatedHrefNodes.push(c);
      const ancestorAnchor = c.closest('a[href]');
      if (ancestorAnchor && node.contains(ancestorAnchor)) relatedHrefNodes.push(ancestorAnchor);
      if (container) relatedHrefNodes.push(...container.querySelectorAll('a[href]'));
      const relatedHrefs = [...new Set(relatedHrefNodes
        .map((a) => a.href || a.getAttribute('href') || '')
        .filter(Boolean))];

      const containerText = container
        ? String(container.innerText || container.textContent || '').replace(/\u200B/g, '').trim()
        : '';
      const containerDataTestId = container ? (container.getAttribute('data-testid') || null) : null;
      const containerTitle = container ? String(container.getAttribute('title') || '') : '';

      controls.push({
        controlIndex: i,
        tag: String(c.tagName || '').toLowerCase(),
        role: controlRole,
        href: ownHref,
        relatedHrefs,
        download,
        text: ctext,
        ariaLabel: label,
        title,
        dataTestId,
        disabled: !!c.disabled || c.getAttribute('aria-disabled') === 'true',
        connected: !!c.isConnected,
        visible: visible(c),
        containerKey,
        containerArtifactHint,
        containerText,
        containerDataTestId,
        containerTitle
      });
    }
    assistantRows.push({assistantIndex, text, rawId, controls});
    assistantIndex += 1;
  }
  return {url: location.href, assistantRows};
}
"""

_BROWSER_FETCH_SCRIPT = r"""
async (href) => {
  /* SHIRABE_ARTIFACT_FETCH_V1 */
  try {
    const response = await fetch(href, {credentials: 'include', redirect: 'follow'});
    const buffer = await response.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode(...bytes.subarray(i, Math.min(i + chunk, bytes.length)));
    }
    return {
      ok: response.ok,
      status: response.status,
      contentType: response.headers.get('content-type'),
      contentDisposition: response.headers.get('content-disposition'),
      contentLength: response.headers.get('content-length'),
      finalUrl: response.url,
      bodyBase64: btoa(binary)
    };
  } catch (error) {
    return {ok: false, fetchError: String(error)};
  }
}
"""


_ARTIFACT_ACTION_SCRIPT = r"""
(arg) => {
  /* SHIRABE_ARTIFACT_ACTION_V1 SHIRABE_ARTIFACT_ACTION_V2 */
  const expected = String(arg.expectedFilename || '');
  const expectedRawId = arg.responseRawId ? String(arg.responseRawId) : null;
  const expectedIndex = Number(arg.roleIndex);
  const normalizeText = (value) => String(value || '').replace(/\u200B/g, '').trim().split(/\r?\n/).map((line) => line.replace(/[ \t]+$/g, '')).join('\n').trim();
  const expectedText = normalizeText(arg.responseText || '');
  const mode = String(arg.mode || 'preflight');

  const visible = (node) => {
    if (!(node instanceof Element) || !node.isConnected) return false;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    if (node.getAttribute('hidden') !== null || node.getAttribute('aria-hidden') === 'true') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const exactFilenameIn = (value) => {
    const text = String(value || '').trim().replace(/^[\"'`]+|[\"'`]+$/g, '');
    if (!text || !expected) return false;
    if (text === expected) return true;
    return text.split(/\s+/).some((part) => part.replace(/^[([{<]+|[)\]}>.,;]+$/g, '') === expected);
  };
  const artifactContainerSelector = [
    '[data-testid*="artifact" i]', '[data-testid*="file" i]', '[data-testid*="attachment" i]',
    '[class*="artifact" i]', '[class*="attachment" i]', '[class*="file-card" i]', '[class*="file_card" i]'
  ].join(',');
  const responseActionMarkers = [
    'copy', 'feedback', 'good response', 'bad response', 'thumb', 'read aloud',
    'regenerate', 'retry', 'share', 'more actions', 'more options', 'edit message'
  ];

  const articleTurns = [...document.querySelectorAll(
    'article[data-testid^="conversation-turn-"], article[data-message-author-role]'
  )];
  const roleTurns = articleTurns.length ? [] : [...document.querySelectorAll('[data-message-author-role]')];
  const unique = [...new Set(articleTurns.length ? articleTurns : roleTurns)];
  const assistantRows = [];
  let assistantIndex = 0;
  for (const node of unique) {
    const roleNode = node.matches('[data-message-author-role]') ? node : node.querySelector('[data-message-author-role]');
    const role = String(roleNode ? roleNode.getAttribute('data-message-author-role') : '').toLowerCase();
    if (role !== 'assistant') continue;
    const body = node.querySelector('.markdown, [data-message-author-role] .markdown, [data-message-content]') || roleNode || node;
    const text = normalizeText(body.innerText || body.textContent || '');
    const rawId = node.getAttribute('data-message-id') || (roleNode && roleNode.getAttribute('data-message-id')) || null;
    assistantRows.push({assistantIndex, node, text, rawId});
    assistantIndex += 1;
  }

  let matched = assistantRows.filter((row) => expectedRawId && row.rawId === expectedRawId);
  if (!matched.length) {
    matched = assistantRows.filter((row) => row.assistantIndex === expectedIndex && row.text === expectedText);
  }
  if (matched.length !== 1) {
    return {
      ok: false,
      reason: matched.length ? 'response-row-ambiguous' : 'response-row-not-found',
      responseMatchCount: matched.length,
      actionCandidateCount: 0,
      candidateDiagnostics: []
    };
  }

  const row = matched[0].node;
  // IMPORTANT: use the same response-local control universe as S8 discovery. This
  // keeps controlIndex semantics aligned and prevents utility buttons from being
  // resurrected as Artifact actions during S9 preflight.
  const controls = [...new Set([...row.querySelectorAll('a[href], a[download], button, [role="button"]')])];
  const candidates = [];
  const diagnostics = [];
  for (let i = 0; i < controls.length; i++) {
    const c = controls[i];
    const tag = String(c.tagName || '').toLowerCase();
    const role = String(c.getAttribute('role') || '').toLowerCase();
    const ariaLabel = String(c.getAttribute('aria-label') || '');
    const title = String(c.getAttribute('title') || '');
    const dataTestId = String(c.getAttribute('data-testid') || '');
    const text = String(c.innerText || c.textContent || '').trim();
    const disabled = !!c.disabled || c.getAttribute('aria-disabled') === 'true';
    const connected = !!c.isConnected;
    const isVisible = visible(c);
    const active = connected && isVisible && !disabled;
    const actionable = tag === 'button' || role === 'button';

    let container = c.closest(artifactContainerSelector);
    let containerArtifactHint = !!container;
    if (!container) {
      const ownAnchor = c.closest('a[href], a[download]');
      if (ownAnchor && row.contains(ownAnchor)) {
        container = ownAnchor;
        containerArtifactHint = true;
      }
    }
    if (!container) {
      let probe = c.parentElement;
      let depth = 0;
      while (probe && probe !== row && depth < 5) {
        const probeText = String(probe.innerText || probe.textContent || '').replace(/\u200B/g, '').trim();
        if (/\.zip(?:\s|$|[)\]}>.,;])/i.test(probeText) && probe.querySelector('a[href]')) {
          container = probe;
          containerArtifactHint = true;
          break;
        }
        probe = probe.parentElement;
        depth += 1;
      }
    }

    const ownFilenameMatch = [text, ariaLabel, title].some(exactFilenameIn);
    const containerText = containerArtifactHint && container
      ? String(container.innerText || container.textContent || '').replace(/\u200B/g, '').trim()
      : '';
    const containerFilenameMatch = containerArtifactHint && exactFilenameIn(containerText);
    const expectedFilenameMatch = ownFilenameMatch || containerFilenameMatch;
    const ownDescriptor = [text, ariaLabel, title, dataTestId, role, tag].filter(Boolean).join(' ').toLowerCase();
    const artifactMarker = ['download', 'file', '.zip', 'artifact', 'attachment'].some((marker) => ownDescriptor.includes(marker));
    const responseActionMarker = responseActionMarkers.some((marker) => ownDescriptor.includes(marker));
    const artifactLike = active && (expectedFilenameMatch || artifactMarker);

    let semantic = 'non-artifact-control';
    if (!active) semantic = 'inactive-control';
    else if (responseActionMarker && !expectedFilenameMatch && !artifactMarker) semantic = 'non-artifact-response-action';
    else if (artifactLike && expectedFilenameMatch && actionable) semantic = 'logical-action-artifact-candidate';
    else if (artifactLike) semantic = 'artifact-action-correlation-insufficient';

    const selectedCandidate = semantic === 'logical-action-artifact-candidate';
    diagnostics.push({
      index: i,
      tag,
      role: role || null,
      visible: isVisible,
      enabled: !disabled,
      connected,
      actionable,
      artifactLike,
      semantic,
      ariaLabel: ariaLabel || null,
      title: title || null,
      dataTestId: dataTestId || null,
      text: text || null,
      ownFilenameMatch,
      containerArtifactHint,
      containerFilenameMatch,
      expectedFilenameMatch,
      selectedCandidate
    });
    if (selectedCandidate) candidates.push({node: c, index: i});
  }

  if (candidates.length !== 1) {
    return {
      ok: false,
      reason: candidates.length ? 'action-candidate-ambiguous' : 'action-candidate-not-found',
      responseMatchCount: 1,
      actionCandidateCount: candidates.length,
      candidateDiagnostics: diagnostics
    };
  }
  const selected = candidates[0];
  if (mode === 'dispatch') selected.node.click();
  return {
    ok: true,
    reason: mode === 'dispatch' ? 'action-dispatched' : 'action-bound',
    responseMatchCount: 1,
    actionCandidateCount: 1,
    controlIndex: selected.index,
    dispatched: mode === 'dispatch',
    candidateDiagnostics: diagnostics
  };
}
"""

_ACTION_DOWNLOAD_TIMEOUT_MS = 30_000


_GENERATED_HREF_MARKERS = (
    "/backend-api/estuary/",
    "/backend-api/files/",
    "/files/",
    "sandbox:/",
)

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class _BoundArtifactCandidate:
    public: ArtifactCandidate
    href: str | None
    response_identity: MessageIdentity
    action_control_index: int | None = None
    action_container_key: str | None = None
    response_raw_id: str | None = None
    response_text: str | None = None


@dataclass(frozen=True)
class ArtifactDiscoveryFailure:
    failure: FailureKind
    reason: str
    page: PageIdentity | None
    evidence: WebDebugEvidence


@dataclass(frozen=True)
class ArtifactDiscoveryResult:
    candidate: ArtifactCandidate | None
    response_identity: MessageIdentity | None
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.candidate is not None and self.failure is None


def discover_zip_artifact(
    session: ChatGPTSession,
    response: AssistantResponse | MessageIdentity,
    *,
    expected_filename: str | None = None,
) -> ArtifactDiscoveryResult:
    """Rebind to exactly one assistant response and discover exactly one ZIP artifact.

    Signed/authenticated href values stay internal and are never returned in the
    public candidate or operation evidence.
    """

    target = response.identity if isinstance(response, AssistantResponse) else response
    if target.role != MessageRole.ASSISTANT:
        return ArtifactDiscoveryResult(
            candidate=None,
            response_identity=None,
            page=None,
            failure=FailureKind.TARGET_IDENTITY_MISMATCH,
            reason="Artifact discovery requires an assistant response identity.",
            evidence=WebDebugEvidence("artifacts.discover", ("target_role!=ASSISTANT",)),
        )

    if expected_filename is not None:
        unsafe = _unsafe_filename_reason(expected_filename)
        if unsafe:
            return ArtifactDiscoveryResult(
                candidate=None,
                response_identity=target,
                page=None,
                failure=FailureKind.UNSAFE_FILENAME,
                reason=unsafe,
                evidence=WebDebugEvidence("artifacts.discover", ("expected_filename=unsafe",)),
            )

    observation = _observe_response_artifacts(session, target, expected_filename=expected_filename)
    if isinstance(observation, ArtifactDiscoveryFailure):
        return ArtifactDiscoveryResult(
            candidate=None,
            response_identity=target,
            page=observation.page,
            failure=observation.failure,
            reason=observation.reason,
            evidence=observation.evidence,
        )

    bound_candidates, ambiguous_controls, page_identity, observations = observation
    if expected_filename is not None:
        bound_candidates = [c for c in bound_candidates if c.public.filename == expected_filename]

    if ambiguous_controls:
        return ArtifactDiscoveryResult(
            candidate=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_AMBIGUOUS,
            reason="File/download-like controls could not be classified safely on the target response.",
            evidence=WebDebugEvidence(
                "artifacts.discover",
                tuple(observations + [f"ambiguous_control_count={len(ambiguous_controls)}"]),
            ),
        )
    if not bound_candidates:
        return ArtifactDiscoveryResult(
            candidate=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_NOT_FOUND,
            reason="No unambiguous ZIP artifact was found on the target assistant response.",
            evidence=WebDebugEvidence("artifacts.discover", tuple(observations + ["zip_candidate_count=0"])),
        )
    if len(bound_candidates) != 1:
        return ArtifactDiscoveryResult(
            candidate=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_AMBIGUOUS,
            reason="Multiple ZIP artifacts match the target assistant response.",
            evidence=WebDebugEvidence(
                "artifacts.discover",
                tuple(observations + [f"zip_candidate_count={len(bound_candidates)}"]),
            ),
        )

    selected = bound_candidates[0]
    return ArtifactDiscoveryResult(
        candidate=selected.public,
        response_identity=target,
        page=page_identity,
        evidence=WebDebugEvidence(
            "artifacts.discover",
            tuple(observations + ["zip_candidate_count=1", "response_binding=verified"]),
            {"candidate_class": selected.public.candidate_class},
        ),
    )


def download_zip_artifact(
    session: ChatGPTSession,
    response: AssistantResponse | MessageIdentity,
    destination_dir: str | Path,
    *,
    expected_filename: str | None = None,
    operation_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> ArtifactDownloadResult:
    """Download one response-bound ZIP and return integrity-bearing Web evidence."""

    started = _utc_now()
    operation_id = operation_id_factory()
    target = response.identity if isinstance(response, AssistantResponse) else response

    discovery_internal = _discover_internal(session, target, expected_filename=expected_filename)
    if isinstance(discovery_internal, ArtifactDownloadResult):
        completed = _utc_now()
        return _attach_operation_evidence(discovery_internal, operation_id, started, completed)

    candidate, page_identity, discovery_evidence = discovery_internal
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output_path = destination / candidate.public.filename

    if output_path.exists():
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_COLLISION,
            reason=f"Refusing to overwrite existing artifact: {output_path.name}",
            evidence=WebDebugEvidence(
                "artifacts.download",
                discovery_evidence.observations + ("destination_collision=true",),
            ),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    if candidate.href is None:
        temp_path = destination / f".{candidate.public.filename}.{operation_id}.part"
        result = _download_action_bound_artifact(
            session,
            target,
            candidate,
            page_identity,
            discovery_evidence,
            output_path,
            temp_path,
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        bound = session._bound_page() if bind.ok else None
    if bound is None:
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.SESSION_BINDING_FAILURE,
            reason="ChatGPT page is not bound for artifact download.",
            evidence=WebDebugEvidence("artifacts.download", ("bound-page=missing",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    try:
        raw_fetch = bound.ops.evaluate(_BROWSER_FETCH_SCRIPT, candidate.href) or {}
    except Exception as exc:
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Browser-context artifact fetch failed: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence("artifacts.download", ("browser_fetch=exception",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    if not bool(raw_fetch.get("ok")):
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Artifact transfer did not succeed (HTTP/status={raw_fetch.get('status')}, error={raw_fetch.get('fetchError')}).",
            evidence=WebDebugEvidence("artifacts.download", ("browser_fetch=failed",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    try:
        body = base64.b64decode(str(raw_fetch.get("bodyBase64") or ""), validate=True)
    except Exception as exc:
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_INCOMPLETE,
            reason=f"Downloaded body could not be decoded: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence("artifacts.download", ("body_decode=failed",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    declared_length = _parse_content_length(raw_fetch.get("contentLength"))
    if declared_length is not None and declared_length != len(body):
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_INCOMPLETE,
            reason=f"Transferred byte length mismatch: declared={declared_length}, observed={len(body)}.",
            evidence=WebDebugEvidence("artifacts.download", ("content_length_match=false",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    disposition_name = _filename_from_content_disposition(raw_fetch.get("contentDisposition"))
    if disposition_name is not None and disposition_name != candidate.public.filename:
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.TARGET_IDENTITY_MISMATCH,
            reason=(
                "Artifact filename changed between response discovery and transfer: "
                f"discovered={candidate.public.filename!r}, transferred={disposition_name!r}."
            ),
            evidence=WebDebugEvidence("artifacts.download", ("transfer_filename_match=false",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    if not _is_valid_zip(body):
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_INCOMPLETE,
            reason="Downloaded artifact is not a complete readable ZIP archive.",
            evidence=WebDebugEvidence("artifacts.download", ("zip_integrity=false",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    digest = hashlib.sha256(body).hexdigest()
    try:
        with output_path.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_COLLISION,
            reason=f"Refusing to overwrite existing artifact: {output_path.name}",
            evidence=WebDebugEvidence("artifacts.download", ("destination_collision=true",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())
    except Exception as exc:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        result = ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Artifact could not be saved: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence("artifacts.download", ("save=failed",)),
        )
        return _attach_operation_evidence(result, operation_id, started, _utc_now())

    metadata = ArtifactMetadata(
        artifact_id=candidate.public.artifact_id,
        filename=candidate.public.filename,
        saved_path=str(output_path),
        size_bytes=len(body),
        sha256=digest,
        response_identity=target,
        content_type=str(raw_fetch.get("contentType")) if raw_fetch.get("contentType") else None,
    )
    result = ArtifactDownloadResult(
        status=WebOperationStatus.DOWNLOADED,
        artifact=metadata,
        response_identity=target,
        page=_identity_of(bound.ops),
        evidence=WebDebugEvidence(
            "artifacts.download",
            discovery_evidence.observations
            + (
                "response_binding=verified",
                "artifact_transport=direct-href",
                "transfer_complete=true",
                "zip_integrity=true",
                f"size_bytes={len(body)}",
                f"sha256={digest}",
            ),
            {"candidate_class": candidate.public.candidate_class},
        ),
    )
    return _attach_operation_evidence(result, operation_id, started, _utc_now())


def _download_action_bound_artifact(
    session: ChatGPTSession,
    target: MessageIdentity,
    candidate: _BoundArtifactCandidate,
    page_identity: PageIdentity,
    discovery_evidence: WebDebugEvidence,
    output_path: Path,
    temp_path: Path,
) -> ArtifactDownloadResult:
    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        bound = session._bound_page() if bind.ok else None
    if bound is None:
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.SESSION_BINDING_FAILURE,
            reason="ChatGPT page is not bound for action-based artifact download.",
            evidence=WebDebugEvidence("artifacts.download", ("bound-page=missing",)),
        )

    action_arg = {
        "mode": "preflight",
        "expectedFilename": candidate.public.filename,
        "responseRawId": candidate.response_raw_id,
        "roleIndex": target.role_index,
        "responseText": candidate.response_text or "",
    }
    base_observations = list(discovery_evidence.observations) + [
        "artifact_binding=verified",
        "artifact_transport=action",
        "direct_transfer_href=false",
    ]
    try:
        preflight = bound.ops.evaluate(_ARTIFACT_ACTION_SCRIPT, action_arg) or {}
    except Exception as exc:
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Artifact action preflight failed: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence(
                "artifacts.download",
                tuple(base_observations + ["download_action_bound=false", "action_preflight=exception"]),
            ),
        )

    response_matches = _safe_int(preflight.get("responseMatchCount"), 0)
    action_candidates = _safe_int(preflight.get("actionCandidateCount"), 0)
    preflight_reason = str(preflight.get("reason") or "unknown")
    base_observations.extend(
        (
            f"action_preflight.response_match_count={response_matches}",
            f"action_preflight.action_candidate_count={action_candidates}",
            f"action_preflight.reason={preflight_reason}",
        )
    )
    base_observations.extend(_action_preflight_candidate_observations(preflight))
    if response_matches != 1:
        failure = FailureKind.TARGET_IDENTITY_MISMATCH if response_matches == 0 else FailureKind.AMBIGUOUS_OBSERVATION
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=failure,
            reason="Target assistant response could not be rebound uniquely for Artifact action dispatch.",
            evidence=WebDebugEvidence("artifacts.download", tuple(base_observations + ["download_action_bound=false"])),
        )
    if action_candidates != 1 or not bool(preflight.get("ok")):
        failure = FailureKind.ARTIFACT_NOT_FOUND if action_candidates == 0 else FailureKind.ARTIFACT_AMBIGUOUS
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=failure,
            reason="Response-bound Artifact action could not be rebound uniquely before download.",
            evidence=WebDebugEvidence("artifacts.download", tuple(base_observations + ["download_action_bound=false"])),
        )

    base_observations.append("download_action_bound=true")
    dispatch_arg = dict(action_arg)
    dispatch_arg["mode"] = "dispatch"
    try:
        transport = session._download_from_bound_action(
            _ARTIFACT_ACTION_SCRIPT,
            dispatch_arg,
            temp_path,
            expected_filename=candidate.public.filename,
            timeout_ms=_ACTION_DOWNLOAD_TIMEOUT_MS,
        )
    except Exception as exc:
        _remove_if_exists(temp_path)
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Action-based artifact download transport failed: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence(
                "artifacts.download",
                tuple(base_observations + ["download_event_observed=false", "transfer_complete=false"]),
            ),
        )

    event_observed = bool(transport.get("eventObserved"))
    transfer_complete = bool(transport.get("transferComplete"))
    suggested_match = transport.get("suggestedFilenameMatch")
    status = str(transport.get("status") or "unknown")
    dispatch_result = transport.get("dispatchResult") if isinstance(transport.get("dispatchResult"), dict) else {}
    dispatch_ok = bool(dispatch_result.get("ok"))
    observations = base_observations + [
        f"download_dispatch_ok={str(dispatch_ok).lower()}",
        f"download_event_observed={str(event_observed).lower()}",
        f"suggested_filename_match={_bool_observation(suggested_match)}",
        f"transfer_complete={str(transfer_complete).lower()}",
        f"download_transport_state={status}",
    ]

    if not bool(transport.get("ok")):
        _remove_if_exists(temp_path)
        if status == "timeout":
            failure = FailureKind.TIMEOUT
            reason = "Timed out waiting for the response-bound Artifact download event."
        elif status == "filename-mismatch":
            failure = FailureKind.TARGET_IDENTITY_MISMATCH
            reason = "Downloaded Artifact filename did not match the response-bound expected filename."
        else:
            failure = FailureKind.ARTIFACT_DOWNLOAD_FAILURE
            reason = f"Action-based Artifact download did not complete safely (state={status})."
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=failure,
            reason=reason,
            evidence=WebDebugEvidence("artifacts.download", tuple(observations + ["zip_integrity=false"])),
        )

    if not temp_path.exists():
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_INCOMPLETE,
            reason="Browser reported a completed Artifact download but no saved transfer file exists.",
            evidence=WebDebugEvidence(
                "artifacts.download",
                tuple(observations + ["saved_file_exists=false", "zip_integrity=false"]),
            ),
        )

    try:
        body = temp_path.read_bytes()
    except Exception as exc:
        _remove_if_exists(temp_path)
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Downloaded Artifact could not be read from the completed transfer: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence(
                "artifacts.download",
                tuple(observations + ["saved_file_read=false", "zip_integrity=false"]),
            ),
        )

    if not body or not _is_valid_zip(body):
        _remove_if_exists(temp_path)
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_INCOMPLETE,
            reason="Downloaded artifact is empty or not a complete readable ZIP archive.",
            evidence=WebDebugEvidence(
                "artifacts.download",
                tuple(observations + [f"size_bytes={len(body)}", "zip_integrity=false"]),
            ),
        )

    digest = hashlib.sha256(body).hexdigest()
    try:
        with output_path.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _remove_if_exists(temp_path)
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_COLLISION,
            reason=f"Refusing to overwrite existing artifact: {output_path.name}",
            evidence=WebDebugEvidence("artifacts.download", tuple(observations + ["destination_collision=true"])),
        )
    except Exception as exc:
        _remove_if_exists(temp_path)
        _remove_if_exists(output_path)
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=_identity_of(bound.ops),
            failure=FailureKind.ARTIFACT_DOWNLOAD_FAILURE,
            reason=f"Artifact could not be committed to its final destination: {type(exc).__name__}: {exc}",
            evidence=WebDebugEvidence("artifacts.download", tuple(observations + ["final_save=false"])),
        )
    finally:
        _remove_if_exists(temp_path)

    metadata = ArtifactMetadata(
        artifact_id=candidate.public.artifact_id,
        filename=candidate.public.filename,
        saved_path=str(output_path),
        size_bytes=len(body),
        sha256=digest,
        response_identity=target,
        content_type=None,
    )
    return ArtifactDownloadResult(
        status=WebOperationStatus.DOWNLOADED,
        artifact=metadata,
        response_identity=target,
        page=_identity_of(bound.ops),
        evidence=WebDebugEvidence(
            "artifacts.download",
            tuple(
                observations
                + [
                    "response_binding=verified",
                    "artifact_binding=verified",
                    "zip_integrity=true",
                    f"size_bytes={len(body)}",
                    f"sha256={digest}",
                ]
            ),
            {"candidate_class": candidate.public.candidate_class},
        ),
    )


def _bool_observation(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(bool(value)).lower()


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _discover_internal(
    session: ChatGPTSession,
    target: MessageIdentity,
    *,
    expected_filename: str | None,
) -> tuple[_BoundArtifactCandidate, PageIdentity, WebDebugEvidence] | ArtifactDownloadResult:
    if target.role != MessageRole.ASSISTANT:
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=None,
            failure=FailureKind.TARGET_IDENTITY_MISMATCH,
            reason="Artifact download requires an assistant response identity.",
            evidence=WebDebugEvidence("artifacts.download", ("target_role!=ASSISTANT",)),
        )
    if expected_filename is not None:
        unsafe = _unsafe_filename_reason(expected_filename)
        if unsafe:
            return ArtifactDownloadResult(
                status=WebOperationStatus.FAILED,
                artifact=None,
                response_identity=target,
                page=None,
                failure=FailureKind.UNSAFE_FILENAME,
                reason=unsafe,
                evidence=WebDebugEvidence("artifacts.download", ("expected_filename=unsafe",)),
            )

    observation = _observe_response_artifacts(session, target, expected_filename=expected_filename)
    if isinstance(observation, ArtifactDiscoveryFailure):
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=observation.page,
            failure=observation.failure,
            reason=observation.reason,
            evidence=observation.evidence,
        )
    candidates, ambiguous, page_identity, observations = observation
    if expected_filename is not None:
        candidates = [c for c in candidates if c.public.filename == expected_filename]
    if ambiguous:
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_AMBIGUOUS,
            reason="File/download-like controls could not be classified safely on the target response.",
            evidence=WebDebugEvidence("artifacts.download", tuple(observations + [f"ambiguous_control_count={len(ambiguous)}"])),
        )
    if not candidates:
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_NOT_FOUND,
            reason="No unambiguous ZIP artifact was found on the target assistant response.",
            evidence=WebDebugEvidence("artifacts.download", tuple(observations + ["zip_candidate_count=0"])),
        )
    if len(candidates) != 1:
        return ArtifactDownloadResult(
            status=WebOperationStatus.FAILED,
            artifact=None,
            response_identity=target,
            page=page_identity,
            failure=FailureKind.ARTIFACT_AMBIGUOUS,
            reason="Multiple ZIP artifacts match the target assistant response.",
            evidence=WebDebugEvidence("artifacts.download", tuple(observations + [f"zip_candidate_count={len(candidates)}"])),
        )
    return candidates[0], page_identity, WebDebugEvidence(
        "artifacts.discover",
        tuple(observations + ["zip_candidate_count=1", "response_binding=verified"]),
        {"candidate_class": candidates[0].public.candidate_class},
    )


@dataclass(frozen=True)
class _ClassifiedArtifactControl:
    candidate: _BoundArtifactCandidate | None
    active: bool
    artifact_like: bool
    ambiguous: bool
    control_index: int
    container_key: str | None
    container_artifact_hint: bool
    filename_candidates: tuple[str, ...]
    expected_filename_match: bool
    selection_reason: str


def _observe_response_artifacts(
    session: ChatGPTSession,
    target: MessageIdentity,
    *,
    expected_filename: str | None = None,
) -> (
    tuple[list[_BoundArtifactCandidate], list[_ClassifiedArtifactControl], PageIdentity, list[str]]
    | ArtifactDiscoveryFailure
):
    bound = session._bound_page()
    if bound is None:
        bind = session.bind_chatgpt_page()
        if not bind.ok:
            return ArtifactDiscoveryFailure(
                bind.failure or FailureKind.SESSION_BINDING_FAILURE,
                bind.reason or "ChatGPT page binding failed.",
                bind.page,
                bind.evidence or WebDebugEvidence("artifacts.discover", ("session-bind=failed",)),
            )
        bound = session._bound_page()
    if bound is None:
        return ArtifactDiscoveryFailure(
            FailureKind.SESSION_BINDING_FAILURE,
            "Session reported bound but no internal page exists.",
            None,
            WebDebugEvidence("artifacts.discover", ("bound-page=missing",)),
        )

    try:
        raw = bound.ops.evaluate(_ARTIFACT_DISCOVERY_SCRIPT) or {}
    except Exception as exc:
        return ArtifactDiscoveryFailure(
            FailureKind.UNKNOWN_WEB_STATE,
            f"Artifact discovery observation failed: {type(exc).__name__}: {exc}",
            _identity_of(bound.ops),
            WebDebugEvidence("artifacts.discover", ("artifact-observation=failed",)),
        )

    url = str(raw.get("url") or bound.ops.url)
    project_key, conversation_id = _route_identity(url)
    if project_key != target.project_key or (
        target.conversation_id is not None and conversation_id != target.conversation_id
    ):
        return ArtifactDiscoveryFailure(
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Current page does not match the target assistant response Project/conversation.",
            _identity_of(bound.ops),
            WebDebugEvidence(
                "artifacts.discover",
                (
                    f"expected_project_key={target.project_key}",
                    f"observed_project_key={project_key}",
                    f"expected_conversation_id={target.conversation_id}",
                    f"observed_conversation_id={conversation_id}",
                ),
            ),
        )

    rows = raw.get("assistantRows") or []
    if not isinstance(rows, list):
        return ArtifactDiscoveryFailure(
            FailureKind.UNKNOWN_WEB_STATE,
            "Artifact discovery returned an invalid assistant row payload.",
            _identity_of(bound.ops),
            WebDebugEvidence("artifacts.discover", ("assistantRows=invalid",)),
        )

    matched_rows: list[dict[str, Any]] = []
    if target.message_id_sha256:
        matched_rows = [
            row for row in rows
            if isinstance(row, dict)
            and row.get("rawId")
            and _sha256(str(row.get("rawId"))) == target.message_id_sha256
        ]
    if not matched_rows:
        matched_rows = [
            row for row in rows
            if isinstance(row, dict)
            and int(row.get("assistantIndex", -1)) == target.role_index
            and _sha256(_normalize_semantic_text(str(row.get("text") or ""))) == target.semantic_text_sha256
        ]

    observations = [
        f"assistant_row_count={len(rows)}",
        f"target_role_index={target.role_index}",
        f"target_message_id_fingerprint={'present' if target.message_id_sha256 else 'absent'}",
        f"target_row_match_count={len(matched_rows)}",
    ]
    if len(matched_rows) == 0:
        return ArtifactDiscoveryFailure(
            FailureKind.TARGET_IDENTITY_MISMATCH,
            "Target assistant response could not be rebound in the current page.",
            _identity_of(bound.ops),
            WebDebugEvidence("artifacts.discover", tuple(observations)),
        )
    if len(matched_rows) > 1:
        return ArtifactDiscoveryFailure(
            FailureKind.AMBIGUOUS_OBSERVATION,
            "Target assistant response identity matched multiple rendered responses.",
            _identity_of(bound.ops),
            WebDebugEvidence("artifacts.discover", tuple(observations)),
        )

    controls = matched_rows[0].get("controls") or []
    if not isinstance(controls, list):
        return ArtifactDiscoveryFailure(
            FailureKind.UNKNOWN_WEB_STATE,
            "Target response artifact controls payload is invalid.",
            _identity_of(bound.ops),
            WebDebugEvidence("artifacts.discover", tuple(observations + ["controls=invalid"])),
        )

    matched_row = matched_rows[0]
    response_raw_id = str(matched_row.get("rawId") or "").strip() or None
    response_text = _normalize_semantic_text(str(matched_row.get("text") or ""))

    classified_controls: list[_ClassifiedArtifactControl] = []
    for control in controls:
        if not isinstance(control, dict):
            continue
        classified_controls.append(
            _classify_control(
                control,
                target,
                expected_filename=expected_filename,
                response_raw_id=response_raw_id,
                response_text=response_text,
            )
        )

    candidate_controls = [item for item in classified_controls if item.candidate is not None]
    logical_by_id: dict[str, _BoundArtifactCandidate] = {}
    for item in candidate_controls:
        assert item.candidate is not None
        logical_by_id.setdefault(item.candidate.public.artifact_id, item.candidate)
    logical_candidates = list(logical_by_id.values())

    active_artifact_like_count = sum(1 for item in classified_controls if item.active and item.artifact_like)
    exact_expected = (
        [candidate for candidate in logical_candidates if candidate.public.filename == expected_filename]
        if expected_filename is not None
        else list(logical_candidates)
    )

    candidate_container_keys = {
        item.container_key
        for item in candidate_controls
        if item.container_key is not None and item.container_artifact_hint
    }
    raw_controls_by_index = {
        _safe_int(control.get("controlIndex"), -1): control
        for control in controls
        if isinstance(control, dict)
    }
    stem_companion_candidates: list[_ClassifiedArtifactControl] = []
    if expected_filename is not None and len(exact_expected) == 1:
        stem_companion_candidates = [
            item
            for item in classified_controls
            if item.ambiguous
            and _is_expected_filename_stem_companion(
                item,
                raw_controls_by_index.get(item.control_index),
                expected_filename,
            )
        ]

    ambiguous: list[_ClassifiedArtifactControl] = []
    absorbed_duplicate_count = 0
    absorbed_stem_companion_count = 0
    nonmatching_artifact_control_count = 0
    for item in classified_controls:
        if not item.ambiguous:
            continue
        if item.container_artifact_hint and item.container_key in candidate_container_keys:
            absorbed_duplicate_count += 1
            continue
        if expected_filename is not None and item.filename_candidates and not item.expected_filename_match:
            nonmatching_artifact_control_count += 1
            continue
        if len(stem_companion_candidates) == 1 and item is stem_companion_candidates[0]:
            absorbed_stem_companion_count += 1
            continue
        ambiguous.append(item)
    action_bound_artifact_count = sum(
        1 for candidate in logical_candidates if candidate.public.candidate_class == "response-action"
    )

    if ambiguous:
        selection_reason = "unresolved-artifact-control-ambiguity"
    elif (
        expected_filename is not None
        and len(exact_expected) == 1
        and absorbed_stem_companion_count == 1
        and exact_expected[0].public.candidate_class == "response-action"
    ):
        selection_reason = "single-exact-expected-action-artifact-with-stem-companion"
    elif (
        expected_filename is not None
        and len(exact_expected) == 1
        and absorbed_stem_companion_count == 1
    ):
        selection_reason = "single-exact-expected-logical-artifact-with-stem-companion"
    elif (
        expected_filename is not None
        and len(exact_expected) == 1
        and exact_expected[0].public.candidate_class == "response-action"
    ):
        selection_reason = "single-exact-expected-action-artifact"
    elif expected_filename is not None and len(exact_expected) == 1:
        selection_reason = "single-exact-expected-logical-artifact"
    elif expected_filename is not None and len(exact_expected) > 1:
        selection_reason = "multiple-exact-expected-logical-artifacts"
    elif expected_filename is not None:
        selection_reason = "expected-logical-artifact-not-found"
    elif len(logical_candidates) == 1:
        selection_reason = "single-logical-zip-artifact"
    elif len(logical_candidates) > 1:
        selection_reason = "multiple-logical-zip-artifacts"
    else:
        selection_reason = "logical-zip-artifact-not-found"

    observations.extend(
        (
            f"control_count={len(controls)}",
            f"raw_control_count={len(controls)}",
            f"active_artifact_like_count={active_artifact_like_count}",
            f"classified_zip_count={len(candidate_controls)}",
            f"logical_artifact_count={len(logical_candidates)}",
            f"action_bound_artifact_count={action_bound_artifact_count}",
            f"exact_expected_match_count={len(exact_expected) if expected_filename is not None else 0}",
            f"ambiguous_control_count={len(ambiguous)}",
            f"absorbed_duplicate_control_count={absorbed_duplicate_count}",
            f"stem_companion_candidate_count={len(stem_companion_candidates)}",
            f"absorbed_stem_companion_count={absorbed_stem_companion_count}",
            f"absorbed_stem_companion_control_index={stem_companion_candidates[0].control_index if absorbed_stem_companion_count == 1 else -1}",
            f"nonmatching_artifact_control_count={nonmatching_artifact_control_count}",
            f"selection_reason={selection_reason}",
        )
    )
    for item in classified_controls:
        observations.extend(_control_evidence_observations(item, controls))

    return logical_candidates, ambiguous, _identity_of(bound.ops), observations



def _is_expected_filename_stem_companion(
    item: _ClassifiedArtifactControl,
    raw_control: dict[str, Any] | None,
    expected_filename: str,
) -> bool:
    """Absorb one response-local display-only stem only after exact Artifact identity exists.

    This is intentionally narrower than generic href-less control suppression: the
    control must be the unresolved filename-less artifact-like control observed in
    the same assistant response, have no transfer semantics, and expose text equal
    to the exact basename stem of the already-unique expected ZIP.
    """

    if raw_control is None:
        return False
    if not item.active or not item.artifact_like or not item.ambiguous:
        return False
    if item.selection_reason != "artifact-filename-not-unique":
        return False
    if item.filename_candidates or item.expected_filename_match:
        return False

    hrefs = _control_href_candidates(raw_control)
    download = str(raw_control.get("download") or "").strip()
    if hrefs or download:
        return False

    text = str(raw_control.get("text") or "").strip()
    if not text:
        return False
    expected_stem = Path(expected_filename).stem
    return text == expected_stem

def _classify_control(
    control: dict[str, Any],
    target: MessageIdentity,
    *,
    expected_filename: str | None = None,
    response_raw_id: str | None = None,
    response_text: str | None = None,
) -> _ClassifiedArtifactControl:
    index = _safe_int(control.get("controlIndex"), -1)
    disabled = bool(control.get("disabled"))
    connected = control.get("connected") is not False
    visible = control.get("visible") is not False
    active = not disabled and connected and visible
    container_key = str(control.get("containerKey") or "").strip() or None
    container_artifact_hint = bool(control.get("containerArtifactHint"))

    hrefs = _control_href_candidates(control)
    download = str(control.get("download") or "").strip()
    text = str(control.get("text") or "").strip()
    aria = str(control.get("ariaLabel") or "").strip()
    title = str(control.get("title") or "").strip()
    data_test_id = str(control.get("dataTestId") or "").strip()
    role = str(control.get("role") or "").strip()
    tag = str(control.get("tag") or "").strip()
    container_text = str(control.get("containerText") or "").strip()
    container_test_id = str(control.get("containerDataTestId") or "").strip()
    container_title = str(control.get("containerTitle") or "").strip()

    filename_candidates = _candidate_filenames(
        download=download,
        text=text,
        aria=aria,
        title=title,
        container_text=container_text if container_artifact_hint else "",
        hrefs=hrefs,
        expected_filename=expected_filename,
    )
    expected_match = expected_filename is not None and expected_filename in filename_candidates

    own_descriptor = " ".join(
        value for value in (download, text, aria, title, data_test_id, role, tag) if value
    ).lower()
    container_descriptor = " ".join(
        value for value in (container_test_id, container_title) if value
    ).lower()
    artifact_marker = any(
        marker in own_descriptor
        for marker in ("download", "file", ".zip", "artifact", "attachment")
    ) or (
        container_artifact_hint
        and any(marker in container_descriptor for marker in ("file", "artifact", "attachment"))
    )
    response_action_marker = any(
        marker in own_descriptor
        for marker in (
            "copy", "feedback", "good response", "bad response", "thumb", "read aloud",
            "regenerate", "retry", "share", "more actions", "more options", "edit message",
        )
    )
    generated_hrefs = [href for href in hrefs if _is_generated_href(href)]
    artifact_like = active and bool(
        filename_candidates
        or artifact_marker
        or (container_artifact_hint and generated_hrefs)
    )

    if not active:
        return _ClassifiedArtifactControl(
            None, active, False, False, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "inactive-control"
        )
    if response_action_marker and not filename_candidates and not artifact_marker:
        return _ClassifiedArtifactControl(
            None, active, False, False, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "non-artifact-response-action"
        )
    if not artifact_like:
        return _ClassifiedArtifactControl(
            None, active, False, False, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "non-artifact-control"
        )

    filename: str | None
    if expected_match:
        filename = expected_filename
    elif len(filename_candidates) == 1:
        filename = filename_candidates[0]
    else:
        filename = None

    href_by_identity: dict[str, str] = {}
    ranked_generated = sorted(generated_hrefs, key=_generated_href_priority)
    if ranked_generated:
        best_rank = _generated_href_priority(ranked_generated[0])[0]
        for href in ranked_generated:
            if _generated_href_priority(href)[0] != best_rank:
                continue
            href_by_identity.setdefault(_sanitized_href_identity(href), href)
    explicit_href = str(control.get("href") or "").strip()
    if not href_by_identity and explicit_href and download:
        href_by_identity.setdefault(_sanitized_href_identity(explicit_href), explicit_href)

    if filename is None:
        return _ClassifiedArtifactControl(
            None, active, True, True, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "artifact-filename-not-unique"
        )
    if _unsafe_filename_reason(filename):
        return _ClassifiedArtifactControl(
            None, active, True, True, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "artifact-filename-unsafe"
        )
    if not href_by_identity:
        actionable = tag.lower() == "button" or role.lower() == "button"
        if expected_filename is not None and expected_match and actionable:
            action_identity = (
                f"container:{container_key}"
                if container_artifact_hint and container_key is not None
                else f"control:{index}"
            )
            artifact_id = hashlib.sha256(
                (
                    f"{target.project_key}|{target.conversation_id}|{target.role_index}|"
                    f"{target.message_id_sha256 or target.semantic_text_sha256}|{filename}|action|{action_identity}"
                ).encode("utf-8")
            ).hexdigest()
            candidate = _BoundArtifactCandidate(
                public=ArtifactCandidate(
                    artifact_id=artifact_id,
                    filename=filename,
                    response_identity=target,
                    candidate_class="response-action",
                ),
                href=None,
                response_identity=target,
                action_control_index=index,
                action_container_key=container_key,
                response_raw_id=response_raw_id,
                response_text=response_text,
            )
            return _ClassifiedArtifactControl(
                candidate, active, True, False, index, container_key, container_artifact_hint,
                filename_candidates, expected_match, "logical-action-artifact-candidate"
            )
        return _ClassifiedArtifactControl(
            None, active, True, True, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "artifact-href-not-established"
        )
    if len(href_by_identity) != 1:
        return _ClassifiedArtifactControl(
            None, active, True, True, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "artifact-href-not-unique"
        )

    href_identity, href = next(iter(href_by_identity.items()))
    explicit_download = bool(download)
    card_correlated = container_artifact_hint and bool(filename_candidates)
    generated_href = _is_generated_href(href)
    if not explicit_download and not (generated_href and (artifact_marker or card_correlated)):
        return _ClassifiedArtifactControl(
            None, active, True, True, index, container_key, container_artifact_hint,
            filename_candidates, expected_match, "artifact-semantic-correlation-insufficient"
        )

    candidate_class = "strong" if explicit_download else "response-card"
    artifact_id = hashlib.sha256(
        (
            f"{target.project_key}|{target.conversation_id}|{target.role_index}|"
            f"{target.message_id_sha256 or target.semantic_text_sha256}|{filename}|{href_identity}"
        ).encode("utf-8")
    ).hexdigest()
    candidate = _BoundArtifactCandidate(
        public=ArtifactCandidate(
            artifact_id=artifact_id,
            filename=filename,
            response_identity=target,
            candidate_class=candidate_class,
        ),
        href=href,
        response_identity=target,
    )
    return _ClassifiedArtifactControl(
        candidate, active, True, False, index, container_key, container_artifact_hint,
        filename_candidates, expected_match, "logical-artifact-candidate"
    )


def _candidate_filenames(
    *,
    download: str,
    text: str,
    aria: str,
    title: str,
    container_text: str,
    hrefs: tuple[str, ...],
    expected_filename: str | None,
) -> tuple[str, ...]:
    names: list[str] = []
    for value in (download, text, aria, title, container_text):
        if not value:
            continue
        if expected_filename is not None and _contains_exact_filename(value, expected_filename):
            names.append(expected_filename)
        names.extend(_extract_zip_names(value))
    for href in hrefs:
        try:
            name = unquote(Path(urlparse(href).path).name)
        except Exception:
            name = ""
        if name.lower().endswith(".zip"):
            names.append(name)
    return tuple(dict.fromkeys(name for name in names if name))


def _extract_zip_names(value: str) -> list[str]:
    result: list[str] = []
    for match in re.finditer(r'([^\s/\\<>:"|?*]+\.zip)(?=$|[\s)\]}>,;])', value, flags=re.IGNORECASE):
        name = match.group(1).strip("'\"`([{<")
        if name:
            result.append(Path(name).name)
    stripped = value.strip().strip("'\"`")
    if stripped.lower().endswith(".zip") and not any(ch.isspace() for ch in stripped):
        result.append(Path(stripped).name)
    return list(dict.fromkeys(result))


def _contains_exact_filename(value: str, expected_filename: str) -> bool:
    pattern = rf'(?<![^\s/\\<>:"|?*]){re.escape(expected_filename)}(?![^\s/\\<>:"|?*])'
    return re.search(pattern, value) is not None or value.strip().strip("'\"`") == expected_filename


def _control_href_candidates(control: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    own = str(control.get("href") or "").strip()
    if own:
        values.append(own)
    related = control.get("relatedHrefs") or []
    if isinstance(related, (list, tuple)):
        values.extend(str(value).strip() for value in related if str(value).strip())
    return tuple(dict.fromkeys(values))


def _is_generated_href(href: str) -> bool:
    lowered = href.lower()
    return any(marker in lowered for marker in _GENERATED_HREF_MARKERS)


def _generated_href_priority(href: str) -> tuple[int, str]:
    lowered = href.lower()
    if "/backend-api/estuary/" in lowered or "/backend-api/files/" in lowered:
        return (0, _sanitized_href_identity(href))
    if "/files/" in lowered:
        return (1, _sanitized_href_identity(href))
    if lowered.startswith("sandbox:/"):
        return (2, _sanitized_href_identity(href))
    return (3, _sanitized_href_identity(href))



def _action_preflight_candidate_observations(preflight: dict[str, Any]) -> tuple[str, ...]:
    raw = preflight.get("candidateDiagnostics") or []
    if not isinstance(raw, list):
        return ("action_preflight.candidate_diagnostics=invalid",)
    observations: list[str] = [f"action_preflight.observed_control_count={len(raw)}"]
    for ordinal, item in enumerate(raw):
        if not isinstance(item, dict):
            observations.append(f"action_preflight.candidate.{ordinal}.payload=invalid")
            continue
        prefix = f"action_preflight.candidate.{ordinal}"
        observations.extend(
            (
                f"{prefix}.control_index={_safe_int(item.get('index'), -1)}",
                f"{prefix}.tag={_safe_evidence_text(item.get('tag'))}",
                f"{prefix}.role={_safe_evidence_text(item.get('role'))}",
                f"{prefix}.visible={str(bool(item.get('visible'))).lower()}",
                f"{prefix}.enabled={str(bool(item.get('enabled'))).lower()}",
                f"{prefix}.connected={str(bool(item.get('connected'))).lower()}",
                f"{prefix}.actionable={str(bool(item.get('actionable'))).lower()}",
                f"{prefix}.artifact_like={str(bool(item.get('artifactLike'))).lower()}",
                f"{prefix}.semantic={_safe_evidence_text(item.get('semantic'))}",
                f"{prefix}.aria_label={_safe_evidence_text(item.get('ariaLabel'))}",
                f"{prefix}.title={_safe_evidence_text(item.get('title'))}",
                f"{prefix}.data_testid={_safe_evidence_text(item.get('dataTestId'))}",
                f"{prefix}.text={_safe_evidence_text(item.get('text'))}",
                f"{prefix}.own_filename_match={str(bool(item.get('ownFilenameMatch'))).lower()}",
                f"{prefix}.container_artifact_hint={str(bool(item.get('containerArtifactHint'))).lower()}",
                f"{prefix}.container_filename_match={str(bool(item.get('containerFilenameMatch'))).lower()}",
                f"{prefix}.expected_filename_match={str(bool(item.get('expectedFilenameMatch'))).lower()}",
                f"{prefix}.selected={str(bool(item.get('selectedCandidate'))).lower()}",
            )
        )
    return tuple(observations)

def _control_evidence_observations(
    item: _ClassifiedArtifactControl,
    raw_controls: list[Any],
) -> tuple[str, ...]:
    raw = next(
        (
            control for control in raw_controls
            if isinstance(control, dict) and _safe_int(control.get("controlIndex"), -1) == item.control_index
        ),
        {},
    )
    prefix = f"control.{item.control_index}"
    hrefs = _control_href_candidates(raw) if isinstance(raw, dict) else ()
    generated_count = sum(1 for href in hrefs if _is_generated_href(href))
    container_text = str(raw.get("containerText") or "") if isinstance(raw, dict) else ""
    container_filenames = _candidate_filenames(
        download="",
        text="",
        aria="",
        title="",
        container_text=container_text if item.container_artifact_hint else "",
        hrefs=(),
        expected_filename=None,
    )
    return (
        f"{prefix}.tag={_safe_evidence_text(raw.get('tag'))}",
        f"{prefix}.role={_safe_evidence_text(raw.get('role'))}",
        f"{prefix}.visible={str(raw.get('visible') is not False).lower()}",
        f"{prefix}.enabled={str(not bool(raw.get('disabled'))).lower()}",
        f"{prefix}.connected={str(raw.get('connected') is not False).lower()}",
        f"{prefix}.artifact_like={str(item.artifact_like).lower()}",
        f"{prefix}.semantic={item.selection_reason}",
        f"{prefix}.aria_label={_safe_evidence_text(raw.get('ariaLabel'))}",
        f"{prefix}.title={_safe_evidence_text(raw.get('title'))}",
        f"{prefix}.data_testid={_safe_evidence_text(raw.get('dataTestId'))}",
        f"{prefix}.text={_safe_evidence_text(raw.get('text'))}",
        f"{prefix}.href_candidate_count={len(hrefs)}",
        f"{prefix}.generated_href_count={generated_count}",
        f"{prefix}.download_present={str(bool(str(raw.get('download') or '').strip())).lower()}",
        f"{prefix}.container_key={item.container_key or 'none'}",
        f"{prefix}.container_artifact_hint={str(item.container_artifact_hint).lower()}",
        f"{prefix}.container_text_length={len(container_text)}",
        f"{prefix}.container_text_fingerprint={_short_fingerprint(container_text)}",
        f"{prefix}.filename_candidates={','.join(item.filename_candidates) or 'none'}",
        f"{prefix}.container_filename_candidates={','.join(container_filenames) or 'none'}",
        f"{prefix}.extension_candidates={','.join(sorted({Path(name).suffix.lower() for name in item.filename_candidates})) or 'none'}",
        f"{prefix}.expected_filename_match={str(item.expected_filename_match).lower()}",
    )


def _safe_evidence_text(value: Any, *, limit: int = 120) -> str:
    text = re.sub(r'https?://\S+|sandbox:/\S+', '<url-redacted>', str(value or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return "none"
    return text[:limit] + ("…" if len(text) > limit else "")


def _short_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else "none"


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _unsafe_filename_reason(filename: str) -> str | None:
    if not filename or filename in {".", ".."}:
        return "Artifact filename is empty or invalid."
    if "/" in filename or "\\" in filename:
        return "Artifact filename must not contain path separators."
    if any(ord(ch) < 32 for ch in filename) or re.search(r'[<>:"|?*]', filename):
        return "Artifact filename contains unsafe characters."
    stem = filename.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        return "Artifact filename uses a reserved Windows device name."
    if filename.endswith(" ") or filename.endswith("."):
        return "Artifact filename must not end in space or period."
    if not filename.lower().endswith(".zip"):
        return "M1-WEB-004 accepts ZIP artifacts only."
    return None


def _filename_from_content_disposition(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    match = re.search(r"filename\*=UTF-8''([^;]+)", text, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_content_length(value: Any) -> int | None:
    try:
        parsed = int(str(value))
        return parsed if parsed >= 0 else None
    except Exception:
        return None


def _is_valid_zip(body: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            return archive.testzip() is None
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def _sanitized_href_identity(href: str) -> str:
    try:
        parsed = urlparse(href)
        # Query/fragment may contain signed credentials; they are intentionally omitted.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "unparseable"


def _attach_operation_evidence(
    result: ArtifactDownloadResult,
    operation_id: str,
    started_at: str,
    completed_at: str,
) -> ArtifactDownloadResult:
    observations = result.evidence.observations if result.evidence else ()
    record = WebOperationEvidenceRecord(
        operation_id=operation_id,
        operation="chatgpt_web_api.artifact.download_zip",
        outcome=result.status.value,
        started_at=started_at,
        completed_at=completed_at,
        project_key=result.response_identity.project_key if result.response_identity else None,
        conversation_id=result.response_identity.conversation_id if result.response_identity else None,
        response_identity=result.response_identity,
        artifact=result.artifact,
        failure=result.failure,
        reason=result.reason,
        observations=tuple(observations),
    )
    return ArtifactDownloadResult(
        status=result.status,
        artifact=result.artifact,
        response_identity=result.response_identity,
        page=result.page,
        operation_evidence=record,
        failure=result.failure,
        reason=result.reason,
        evidence=result.evidence,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
