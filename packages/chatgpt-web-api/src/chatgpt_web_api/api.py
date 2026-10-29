from __future__ import annotations

import hashlib
import json
import time
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Any, Callable

from .artifacts import ArtifactDiscoveryFailure, ArtifactDiscoveryResult, discover_zip_artifact, download_zip_artifact
from .chats import start_new_chat
from .composer import attach_files, observe_attachment_transport
from .evidence import debug_evidence_to_dict, evidence_to_dict
from .generation import submit_prompt_and_wait_for_response
from .facade_contract import (
    CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP,
    CAPABILITY_PROJECT_CHAT_OPEN,
    CAPABILITY_PROMPT_EXECUTE_AND_WAIT,
    CONTRACT_MAJOR_VERSION,
    CONTRACT_NAME,
    REQUIRED_CAPABILITIES,
    WEB_FACADE_CONTRACT_V2,
    FacadeContractSpec,
    validate_facade_contract,
    validate_operation_request,
    validate_operation_result,
)
from .models import (
    ArtifactDownloadResult,
    AttachmentTransportObservationResult,
    AssistantResponse,
    ChatIdentity,
    FacadePostcondition,
    FailureKind,
    GenerationState,
    ProjectChatOpenResult,
    PromptExecutionResult,
    PromptExecutionSubmission,
    ResponseZipDownloadResult,
    ProjectIdentity,
    WebDebugEvidence,
    WebOperationEvidenceRecord,
    WebOperationStatus,
)
from .projects import open_project
from .session import ChatGPTSession
from .target_guard import GuardError


@dataclass(frozen=True)
class VerticalSliceStage:
    stage_id: str
    name: str
    status: str
    ok: bool
    failure: str | None = None
    reason: str | None = None
    observations: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerticalSliceSmokeResult:
    result: str
    stages: tuple[VerticalSliceStage, ...]
    project: ProjectIdentity | None = None
    chat: ChatIdentity | None = None
    generation_state: GenerationState | None = None
    response: AssistantResponse | None = None
    artifact_result: ArtifactDownloadResult | None = None
    failure_stage: str | None = None
    failure: FailureKind | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == "PASS" and self.artifact_result is not None and self.artifact_result.ok

    def to_dict(self) -> dict[str, Any]:
        artifact = None
        operation_evidence = None
        if self.artifact_result is not None:
            if self.artifact_result.artifact is not None:
                artifact = {
                    "artifact_id": self.artifact_result.artifact.artifact_id,
                    "filename": self.artifact_result.artifact.filename,
                    "saved_path": self.artifact_result.artifact.saved_path,
                    "size_bytes": self.artifact_result.artifact.size_bytes,
                    "sha256": self.artifact_result.artifact.sha256,
                    "content_type": self.artifact_result.artifact.content_type,
                }
            if self.artifact_result.operation_evidence is not None:
                operation_evidence = evidence_to_dict(self.artifact_result.operation_evidence)

        response = None
        if self.response is not None:
            response = {
                "role": self.response.identity.role.value,
                "role_index": self.response.identity.role_index,
                "semantic_text_sha256": self.response.identity.semantic_text_sha256,
                "message_id_sha256": self.response.identity.message_id_sha256,
                "text_length": len(self.response.text),
            }

        project = asdict(self.project) if self.project is not None else None
        chat = asdict(self.chat) if self.chat is not None else None
        return {
            "schema_version": 1,
            "result": self.result,
            "failure_stage": self.failure_stage,
            "failure": self.failure.value if self.failure is not None else None,
            "reason": self.reason,
            "stages": [asdict(stage) for stage in self.stages],
            "project": project,
            "chat": chat,
            "generation": {"state": self.generation_state.value} if self.generation_state is not None else None,
            "response": response,
            "artifact": artifact,
            "artifact_operation_evidence": operation_evidence,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


@dataclass(frozen=True)
class AttachmentTransportObservationSmokeResult:
    result: str
    stages: tuple[VerticalSliceStage, ...]
    project: ProjectIdentity | None = None
    chat: ChatIdentity | None = None
    observation: AttachmentTransportObservationResult | None = None
    failure_stage: str | None = None
    failure: FailureKind | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == "PASS" and self.observation is not None and self.observation.ok

    def to_dict(self) -> dict[str, Any]:
        evidence = None
        if self.observation is not None and self.observation.evidence is not None:
            evidence = evidence_to_dict(self.observation.evidence)
        return {
            "schema_version": 1,
            "probe": "M1-WEBFIX-008_STAGE1_ATTACHMENT_TRANSPORT_OBSERVATION",
            "result": self.result,
            "failure_stage": self.failure_stage,
            "failure": self.failure.value if self.failure is not None else None,
            "reason": self.reason,
            "stages": [asdict(stage) for stage in self.stages],
            "project": asdict(self.project) if self.project is not None else None,
            "chat": asdict(self.chat) if self.chat is not None else None,
            "attachment_transport": {
                "stage": self.observation.stage,
                "status": self.observation.status.value,
                "file_assigned": False,
                "prompt_dispatched": False,
                "evidence": evidence,
            } if self.observation is not None else None,
        }


@dataclass(frozen=True)
class AttachmentVerticalSliceSmokeResult:
    result: str
    stages: tuple[VerticalSliceStage, ...]
    attachment_filename: str
    expected_marker: str
    expected_artifact: str
    project: ProjectIdentity | None = None
    chat: ChatIdentity | None = None
    generation_state: GenerationState | None = None
    response: AssistantResponse | None = None
    artifact_result: ArtifactDownloadResult | None = None
    attachment_evidence: WebDebugEvidence | None = None
    response_marker_verified: bool = False
    zip_marker_verified: bool = False
    failure_stage: str | None = None
    failure: FailureKind | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.result == "PASS"
            and self.response_marker_verified
            and self.zip_marker_verified
            and self.artifact_result is not None
            and self.artifact_result.ok
        )

    def to_dict(self) -> dict[str, Any]:
        artifact = None
        operation_evidence = None
        if self.artifact_result is not None:
            if self.artifact_result.artifact is not None:
                artifact = {
                    "artifact_id": self.artifact_result.artifact.artifact_id,
                    "filename": self.artifact_result.artifact.filename,
                    "saved_path": self.artifact_result.artifact.saved_path,
                    "size_bytes": self.artifact_result.artifact.size_bytes,
                    "sha256": self.artifact_result.artifact.sha256,
                    "content_type": self.artifact_result.artifact.content_type,
                }
            if self.artifact_result.operation_evidence is not None:
                operation_evidence = evidence_to_dict(self.artifact_result.operation_evidence)
        response = None
        if self.response is not None:
            response = {
                "role": self.response.identity.role.value,
                "role_index": self.response.identity.role_index,
                "semantic_text_sha256": self.response.identity.semantic_text_sha256,
                "message_id_sha256": self.response.identity.message_id_sha256,
                "text_length": len(self.response.text),
            }
        attachment_operation_evidence = evidence_to_dict(self.attachment_evidence) if self.attachment_evidence is not None else None
        return {
            "schema_version": 1,
            "probe": "M1-WEBFIX-009_STAGE2_NATIVE_PICKER_ATTACHMENT_VERTICAL_SLICE",
            "result": self.result,
            "failure_stage": self.failure_stage,
            "failure": self.failure.value if self.failure is not None else None,
            "reason": self.reason,
            "stages": [asdict(stage) for stage in self.stages],
            "attachment": {"filename": self.attachment_filename},
            "attachment_operation_evidence": attachment_operation_evidence,
            "expected_marker": self.expected_marker,
            "expected_artifact": self.expected_artifact,
            "project": asdict(self.project) if self.project is not None else None,
            "chat": asdict(self.chat) if self.chat is not None else None,
            "generation": {"state": self.generation_state.value} if self.generation_state is not None else None,
            "response": response,
            "response_marker_verified": self.response_marker_verified,
            "zip_marker_verified": self.zip_marker_verified,
            "artifact": artifact,
            "artifact_operation_evidence": operation_evidence,
        }


def _require_operation(session: Any) -> tuple[FailureKind, str] | None:
    """Check the injected session guard without changing the v2 request shape."""
    check = getattr(session, "check_operation", None)
    if not callable(check) or not all(callable(getattr(session, name, None)) for name in (
        "begin_operation", "end_operation", "require_reconciliation",
    )):
        return FailureKind.SESSION_BINDING_FAILURE, "BINDING_REQUIRED"
    try:
        check(mutation=True)
    except GuardError as error:
        return error.failure_kind, error.code
    except Exception:
        # A failing guard is not permission to fall through into a provider.
        return FailureKind.SESSION_BINDING_FAILURE, "BINDING_RECORD_INVALID"
    return None


def _require_reconciliation(session: Any) -> None:
    """Best-effort state notification must never replace a provider outcome."""
    try:
        session.require_reconciliation()
    except BaseException:
        pass


def _invoke_once(session: Any, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except BaseException:
        # The lower operation may already have dispatched. Do not retry it or
        # replace its exception with a guard, hook, or diagnostic exception.
        _require_reconciliation(session)
        raise


def _binding_postcheck_reason(result: Any) -> str | None:
    """Read our failure marker without inspecting arbitrary provider properties."""
    if not isinstance(result, (ProjectChatOpenResult, PromptExecutionResult, ResponseZipDownloadResult)):
        return None
    evidence = result.operation_evidence
    if isinstance(evidence, WebDebugEvidence):
        marker = evidence.details.get("binding_postcheck")
        if isinstance(marker, dict) and marker.get("status") == "FAILED":
            return GuardError(marker.get("reason")).code
    elif isinstance(evidence, WebOperationEvidenceRecord):
        prefix = "binding-postcheck-reason="
        for item in evidence.observations:
            if item.startswith(prefix):
                return GuardError(item[len(prefix):]).code
    return None


def _binding_postcheck_failed_result(
    result: Any, error: BaseException, *, operation_id: str | None = None,
) -> Any:
    """Expose final Binding failure using existing v2 fields, retaining facts.

    The aggregate operation fails, while confirmed response/identity/artifact
    objects and send observations retain their original meaning. The caller's
    operation ID is correlation metadata, never a replacement for a lower-level
    artifact operation ID. An unrelated diagnostic hook failure does not use
    this path. Non-v2 objects remain untouched; the wrapper still exposes the
    Binding failure in its operation record and terminal event.
    """
    if not isinstance(result, (ProjectChatOpenResult, PromptExecutionResult, ResponseZipDownloadResult)):
        return result
    guard_error = error if isinstance(error, GuardError) else GuardError("TARGET_UNVERIFIED")
    # If the direct Facade already detected the failure, the wrapper augments
    # the same observation rather than replacing the original outcome snapshot.
    previous_reason = _binding_postcheck_reason(result)
    reason = previous_reason or guard_error.code
    failure = GuardError(reason).failure_kind
    evidence = result.operation_evidence
    markers = ("binding-postcheck=FAILED", "binding-postcheck-reason=" + reason,
               "binding-postcheck-failure-kind=" + failure.value)
    if isinstance(evidence, WebDebugEvidence) or not isinstance(result, ResponseZipDownloadResult):
        if not isinstance(evidence, WebDebugEvidence):
            operation = (CAPABILITY_PROJECT_CHAT_OPEN if isinstance(result, ProjectChatOpenResult)
                         else CAPABILITY_PROMPT_EXECUTE_AND_WAIT)
            evidence = WebDebugEvidence(operation)
        details = dict(evidence.details)
        if previous_reason:
            postcheck = dict(details["binding_postcheck"])
        else:
            postcheck = {"status": "FAILED", "reason": reason, "failure_kind": failure.value,
                         "original_failure_kind": getattr(result.failure_kind, "value", None),
                         "original_reason": result.reason}
            if isinstance(result, PromptExecutionResult):
                postcheck.update(original_completed=result.completed,
                                 original_generation_state=result.generation_state.value)
            else:
                postcheck.update(original_ok=result.ok)
        if operation_id is not None:
            postcheck["operation_id"] = operation_id
        details["binding_postcheck"] = postcheck
        observations = evidence.observations if previous_reason else evidence.observations + markers
        evidence = replace(evidence, observations=observations, details=details)
    elif isinstance(evidence, WebOperationEvidenceRecord):
        observations = evidence.observations
        if not previous_reason:
            observations += markers + ("provider-outcome=" + evidence.outcome,)
            if evidence.failure is not None:
                observations += ("provider-failure-kind=" + evidence.failure.value,)
            if evidence.reason is not None:
                observations += ("provider-reason=" + evidence.reason,)
        if operation_id is not None:
            marker = "instrumented-operation-id=" + operation_id
            if marker not in observations:
                observations += (marker,)
        evidence = replace(evidence, outcome="BINDING_POSTCHECK_FAILED",
                           failure=evidence.failure or failure,
                           reason=evidence.reason or reason, observations=observations)
    elif evidence is None and operation_id is not None:
        # A custom v2 Provider may omit evidence for an already-failed download.
        # The standard domain download always supplies its own operation record.
        # Create only a wrapper-owned postcheck observation, using the existing
        # caller ID; do not invent original transfer timing or a provider ID.
        observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        identity = result.artifact.response_identity if result.artifact is not None else None
        observations = markers + ("original-evidence=absent", "timestamp-scope=postcheck-observation",
                                  "instrumented-operation-id=" + operation_id,
                                  "provider-ok=" + str(result.ok).lower())
        if result.failure_kind is not None:
            observations += ("provider-failure-kind=" + result.failure_kind.value,)
        if result.reason is not None:
            observations += ("provider-reason=" + result.reason,)
        evidence = WebOperationEvidenceRecord(
            operation_id=operation_id, operation="artifact.download_response_zip.binding_postcheck",
            outcome="BINDING_POSTCHECK_FAILED", started_at=observed, completed_at=observed,
            project_key=identity.project_key if identity else None,
            conversation_id=identity.conversation_id if identity else None,
            response_identity=identity, artifact=result.artifact, failure=failure,
            reason=reason, observations=observations,
        )
    updates = {"failure_kind": result.failure_kind or failure,
               "reason": result.reason or reason, "operation_evidence": evidence}
    if isinstance(result, PromptExecutionResult):
        # completed describes operation success. generation_state/response are
        # retained, so a proven response is not relabelled UNKNOWN or unsent.
        return replace(result, completed=False, **updates)
    if isinstance(result, ProjectChatOpenResult):
        return replace(result, status=WebOperationStatus.FAILED, **updates)
    return replace(result, ok=False, **updates)


def _finish_operation(session: Any, result: Any) -> Any:
    """Separate final Binding failure from already-established operation facts."""
    try:
        session.check_operation(mutation=False)
        if isinstance(result, PromptExecutionResult):
            if not result.completed and (result.submission.activation_attempted or result.submission.ok):
                _require_reconciliation(session)
        if getattr(result, "failure_kind", None) is FailureKind.RECONCILE_REQUIRED:
            _require_reconciliation(session)
    except BaseException as error:
        _require_reconciliation(session)
        return _binding_postcheck_failed_result(result, error)
    return result


def _rejected_operation(capability: str, failure: FailureKind, reason: str, request: dict[str, Any]) -> Any:
    """Construct an existing v2 result for an admission failure, before dispatch."""
    evidence = WebDebugEvidence(capability, (reason, "provider-dispatch=0"))
    if capability == CAPABILITY_PROJECT_CHAT_OPEN:
        result = ProjectChatOpenResult(
            status=WebOperationStatus.FAILED, project_identity=None,
            chat_identity=None, postcondition=None, failure_kind=failure,
            reason=reason, operation_evidence=evidence,
        )
    elif capability == CAPABILITY_PROMPT_EXECUTE_AND_WAIT:
        result = PromptExecutionResult(
            submission=PromptExecutionSubmission(ok=False), completed=False,
            response=None, generation_state=GenerationState.NOT_STARTED,
            failure_kind=failure, reason=reason, operation_evidence=evidence,
        )
    else:
        result = ResponseZipDownloadResult(
            ok=False, artifact=None, response_binding_verified=False,
            failure_kind=failure, reason=reason, operation_evidence=evidence,
        )
    validate_operation_result(capability, result, request=request)
    return result


def _reserved_facade_operation(capability: str):
    """Hold one ownership reservation across dispatch, conversion, and validation.

    The explicit v2 signatures remain on the wrapped methods. Request validation
    is offline; no reservation is acquired for an invalid request. A conversion
    or result-validation fault after dispatch preserves the original exception
    and makes later mutation require reconciliation.
    """
    def decorate(function):
        declared = signature(function)

        @wraps(function)
        def reserved(self, *args, **kwargs):
            arguments = declared.bind(self, *args, **kwargs)
            request = {name: value for name, value in arguments.arguments.items() if name != "self"}
            validate_operation_request(capability, request)
            rejected = _require_operation(self.session)
            if rejected is not None:
                return _rejected_operation(capability, *rejected, request)
            try:
                self.session.begin_operation()
            except GuardError as error:
                return _rejected_operation(capability, error.failure_kind, error.code, request)
            except Exception:
                return _rejected_operation(capability, FailureKind.SESSION_BINDING_FAILURE,
                                           "BINDING_RECORD_INVALID", request)
            try:
                return function(self, *args, **kwargs)
            except BaseException:
                _require_reconciliation(self.session)
                raise
            finally:
                try:
                    self.session.end_operation()
                except BaseException:
                    _require_reconciliation(self.session)

        return reserved
    return decorate


class ChatGPTWebFacadeV2:
    """Explicit provider for ``chatgpt-web.facade/v2``.

    The three methods below are the normative Stable Runtime boundary.  Their
    names and keyword-only request fields are versioned contract surface; Web
    implementation details remain private to this package.
    """

    contract_name = CONTRACT_NAME
    contract_version = CONTRACT_MAJOR_VERSION
    capabilities = REQUIRED_CAPABILITIES

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:9222",
        *,
        session: ChatGPTSession | None = None,
    ) -> None:
        self.cdp_url = cdp_url
        self.session = session or ChatGPTSession(cdp_url)

    @_reserved_facade_operation(CAPABILITY_PROJECT_CHAT_OPEN)
    def open_project_chat(
        self,
        *,
        project_name: str,
        project_key: str,
        client_ref: str,
        reuse: bool,
    ) -> ProjectChatOpenResult:
        request = {
            "project_name": project_name,
            "project_key": project_key,
            "client_ref": client_ref,
            "reuse": reuse,
        }
        validate_operation_request(CAPABILITY_PROJECT_CHAT_OPEN, request)

        rejected = _require_operation(self.session)
        if rejected is not None:
            failure, reason = rejected
            result = ProjectChatOpenResult(
                status=WebOperationStatus.FAILED,
                project_identity=None,
                chat_identity=None,
                postcondition=None,
                failure_kind=failure,
                reason=reason,
                operation_evidence=WebDebugEvidence("project_chat.open", (reason, "provider-dispatch=0")),
            )
            validate_operation_result(CAPABILITY_PROJECT_CHAT_OPEN, result, request=request)
            return result

        project_result = _invoke_once(
            self.session,
            open_project,
            self.session,
            project_name=project_name,
            project_key=project_key,
        )
        if not project_result.ok or project_result.project is None:
            result = ProjectChatOpenResult(
                status=project_result.status,
                project_identity=project_result.project,
                chat_identity=None,
                postcondition=None,
                failure_kind=project_result.failure or FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                reason=project_result.reason or "Target Project postcondition was not established.",
                operation_evidence=project_result.evidence,
            )
            validate_operation_result(CAPABILITY_PROJECT_CHAT_OPEN, result, request=request)
            return _finish_operation(self.session, result)

        # ``reuse`` is a permission, not a requirement: M1 v1 may always choose
        # the safer fresh-Chat path rather than guessing an existing Chat.
        chat_result = _invoke_once(
            self.session,
            start_new_chat,
            self.session,
            project_result.project,
            client_ref=client_ref,
        )
        if not chat_result.ok or chat_result.chat is None:
            result = ProjectChatOpenResult(
                status=chat_result.status,
                project_identity=project_result.project,
                chat_identity=chat_result.chat,
                postcondition=None,
                failure_kind=chat_result.failure or FailureKind.POSTCONDITION_NOT_ESTABLISHED,
                reason=chat_result.reason or "Target Project Chat readiness was not established.",
                operation_evidence=chat_result.evidence or project_result.evidence,
            )
            validate_operation_result(CAPABILITY_PROJECT_CHAT_OPEN, result, request=request)
            return _finish_operation(self.session, result)

        result = ProjectChatOpenResult(
            status=WebOperationStatus.READY,
            project_identity=project_result.project,
            chat_identity=chat_result.chat,
            postcondition=FacadePostcondition.TARGET_PROJECT_AND_CHAT_READY,
            operation_evidence=chat_result.evidence or project_result.evidence,
        )
        validate_operation_result(CAPABILITY_PROJECT_CHAT_OPEN, result, request=request)
        return _finish_operation(self.session, result)

    @_reserved_facade_operation(CAPABILITY_PROMPT_EXECUTE_AND_WAIT)
    def execute_prompt_and_wait(
        self,
        *,
        chat: ChatIdentity,
        prompt: str,
        attachments: tuple[dict[str, Any], ...],
    ) -> PromptExecutionResult:
        request = {"chat": chat, "prompt": prompt, "attachments": attachments}
        validate_operation_request(CAPABILITY_PROMPT_EXECUTE_AND_WAIT, request)

        rejected = _require_operation(self.session)
        if rejected is not None:
            failure, reason = rejected
            result = PromptExecutionResult(
                submission=PromptExecutionSubmission(ok=False),
                completed=False,
                response=None,
                generation_state=GenerationState.NOT_STARTED,
                failure_kind=failure,
                reason=reason,
                operation_evidence=WebDebugEvidence("prompt.execute_and_wait", (reason, "provider-dispatch=0")),
            )
            validate_operation_result(CAPABILITY_PROMPT_EXECUTE_AND_WAIT, result, request=request)
            return result

        raw = _invoke_once(self.session, submit_prompt_and_wait_for_response, self.session, chat, prompt, attachments=attachments)
        submission_ok = raw.submission is not None and raw.submission.ok
        submission_debug = debug_evidence_to_dict(getattr(raw.submission, "evidence", None))
        submission_details = submission_debug.get("details", {}) if isinstance(submission_debug, dict) else {}
        result = PromptExecutionResult(
            submission=PromptExecutionSubmission(
                ok=submission_ok,
                activation_attempted=bool(submission_details.get("activation_attempted")),
                trusted_click=bool(submission_details.get("trusted_click")),
                web_submit_established=bool(submission_details.get("web_submit_established")),
                diagnostics=submission_debug,
            ),
            completed=raw.completed,
            response=raw.response,
            generation_state=raw.generation_state,
            failure_kind=(
                raw.failure
                if not raw.completed
                else None
            ) or (
                FailureKind.POSTCONDITION_NOT_ESTABLISHED
                if not raw.completed
                else None
            ),
            reason=raw.reason,
            operation_evidence=raw.evidence,
        )
        validate_operation_result(CAPABILITY_PROMPT_EXECUTE_AND_WAIT, result, request=request)
        return _finish_operation(self.session, result)

    @_reserved_facade_operation(CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP)
    def download_response_zip(
        self,
        *,
        response: AssistantResponse,
        expected_filename: str,
        destination_dir: str | Path,
    ) -> ResponseZipDownloadResult:
        request = {
            "response": response,
            "expected_filename": expected_filename,
            "destination_dir": destination_dir,
        }
        validate_operation_request(CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP, request)

        rejected = _require_operation(self.session)
        if rejected is not None:
            failure, reason = rejected
            result = ResponseZipDownloadResult(
                ok=False,
                artifact=None,
                response_binding_verified=False,
                failure_kind=failure,
                reason=reason,
            )
            validate_operation_result(CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP, result, request=request)
            return result

        raw = _invoke_once(
            self.session,
            download_zip_artifact,
            self.session,
            response,
            destination_dir,
            expected_filename=expected_filename,
        )
        artifact = raw.artifact
        response_binding_verified = bool(
            raw.ok
            and artifact is not None
            and raw.response_identity == response.identity
            and artifact.response_identity == response.identity
        )
        result = ResponseZipDownloadResult(
            ok=raw.ok,
            artifact=artifact,
            response_binding_verified=response_binding_verified,
            failure_kind=(
                raw.failure
                if not raw.ok
                else None
            ) or (
                FailureKind.POSTCONDITION_NOT_ESTABLISHED
                if not raw.ok
                else None
            ),
            reason=raw.reason,
            operation_evidence=raw.operation_evidence,
        )
        validate_operation_result(
            CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP,
            result,
            request=request,
        )
        return _finish_operation(self.session, result)


def create_facade(
    cdp_url: str = "http://127.0.0.1:9222",
    *,
    session: ChatGPTSession | None = None,
) -> ChatGPTWebFacadeV2:
    """Create and validate the generic Web Facade v2 provider."""

    facade = ChatGPTWebFacadeV2(cdp_url=cdp_url, session=session)
    validate_facade_contract(facade)
    return facade


def get_facade_contract() -> FacadeContractSpec:
    """Return the immutable provider contract descriptor without Web access."""

    return WEB_FACADE_CONTRACT_V2


class ChatGPTWebAPI:
    """M1 public facade over verified ChatGPT Web domain operations.

    This facade coordinates existing Web operations.  It deliberately does not
    know SHIRABE Work, Interaction STD, response schemas, Assessment, or Gate
    semantics.  Callers receive postcondition-bearing Web results only.
    """

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:9222",
        *,
        session: ChatGPTSession | None = None,
    ) -> None:
        self.cdp_url = cdp_url
        self.session = session or ChatGPTSession(cdp_url)

    def run_attachment_transport_observation(
        self,
        *,
        project_name: str,
        project_key: str | None = None,
        project_url: str | None = None,
        client_ref: str = "m1-webfix-008-stage1",
        project_timeout_seconds: float = 15.0,
        attachment_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.1,
    ) -> AttachmentTransportObservationSmokeResult:
        """Observe Project→new Chat→causal picker transport and stop."""

        stages: list[VerticalSliceStage] = []
        bind = self.session.bind_chatgpt_page()
        stages.append(_stage("S1", "SESSION_BOUND", bind.status.value, bind.ok, bind.failure, bind.reason, bind.evidence))
        if not bind.ok:
            return AttachmentTransportObservationSmokeResult("FAIL", tuple(stages), failure_stage="S1", failure=bind.failure, reason=bind.reason)
        project_result = open_project(
            self.session, project_name, project_key=project_key, project_url=project_url,
            timeout_seconds=project_timeout_seconds, poll_interval_seconds=min(0.25, max(0.01, poll_interval_seconds)),
        )
        stages.append(_stage("S2", "PROJECT_VERIFIED", project_result.status.value, project_result.ok, project_result.failure, project_result.reason, project_result.evidence))
        if not project_result.ok or project_result.project is None:
            return AttachmentTransportObservationSmokeResult("FAIL", tuple(stages), project=project_result.project, failure_stage="S2", failure=project_result.failure, reason=project_result.reason)
        chat_result = start_new_chat(self.session, project_result.project, client_ref=client_ref)
        stages.append(_stage("S3", "NEW_CHAT_READY", chat_result.status.value, chat_result.ok, chat_result.failure, chat_result.reason, chat_result.evidence))
        if not chat_result.ok or chat_result.chat is None:
            return AttachmentTransportObservationSmokeResult("FAIL", tuple(stages), project=project_result.project, chat=chat_result.chat, failure_stage="S3", failure=chat_result.failure, reason=chat_result.reason)
        observed = observe_attachment_transport(self.session, timeout_seconds=attachment_timeout_seconds)
        stages.append(_stage("S4", "PICKER_TRANSPORT_OBSERVED_NO_UPLOAD", observed.status.value, observed.ok, observed.failure, observed.reason, observed.evidence))
        if not observed.ok:
            return AttachmentTransportObservationSmokeResult("FAIL", tuple(stages), project=project_result.project, chat=chat_result.chat, observation=observed, failure_stage="S4", failure=observed.failure, reason=observed.reason)
        return AttachmentTransportObservationSmokeResult("PASS", tuple(stages), project=project_result.project, chat=chat_result.chat, observation=observed, reason=observed.reason)

    def run_attachment_vertical_slice_smoke(
        self,
        *,
        project_name: str,
        attachment_file: str | Path,
        expected_marker: str,
        expected_artifact: str,
        output_dir: str | Path,
        project_key: str | None = None,
        project_url: str | None = None,
        client_ref: str = "m1-webfix-009-stage2",
        project_timeout_seconds: float = 15.0,
        chat_timeout_seconds: float = 15.0,
        attachment_timeout_seconds: float = 15.0,
        submit_timeout_seconds: float = 10.0,
        generation_timeout_seconds: float = 900.0,
        artifact_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.5,
        stable_samples_required: int = 3,
    ) -> AttachmentVerticalSliceSmokeResult:
        """Run the Stage-2 real attachment vertical slice through the native picker bridge."""

        stages: list[VerticalSliceStage] = []
        attachment_path = Path(attachment_file).expanduser().resolve()
        marker = str(expected_marker)
        artifact_name = str(expected_artifact)

        def failed(stage_id: str, failure: FailureKind | None, reason: str | None, **kwargs: Any) -> AttachmentVerticalSliceSmokeResult:
            return AttachmentVerticalSliceSmokeResult(
                result="FAIL",
                stages=tuple(stages),
                attachment_filename=attachment_path.name,
                expected_marker=marker,
                expected_artifact=artifact_name,
                failure_stage=stage_id,
                failure=failure,
                reason=reason,
                **kwargs,
            )

        if not attachment_path.is_file():
            return failed("S0", FailureKind.POSTCONDITION_NOT_ESTABLISHED, "Stage-2 attachment file does not exist.")
        try:
            attachment_text = attachment_path.read_text(encoding="utf-8")
        except Exception as exc:
            return failed("S0", FailureKind.POSTCONDITION_NOT_ESTABLISHED, f"Stage-2 attachment must be UTF-8 text: {type(exc).__name__}: {exc}")
        if not marker or marker not in attachment_text:
            return failed("S0", FailureKind.POSTCONDITION_NOT_ESTABLISHED, "Expected marker is not present in the Stage-2 attachment.")

        bind = self.session.bind_chatgpt_page()
        stages.append(_stage("S1", "SESSION_BOUND", bind.status.value, bind.ok, bind.failure, bind.reason, bind.evidence))
        if not bind.ok:
            return failed("S1", bind.failure, bind.reason)

        project_result = open_project(
            self.session,
            project_name,
            project_key=project_key,
            project_url=project_url,
            timeout_seconds=project_timeout_seconds,
            poll_interval_seconds=min(0.25, max(0.01, poll_interval_seconds)),
        )
        stages.append(_stage("S2", "PROJECT_VERIFIED", project_result.status.value, project_result.ok, project_result.failure, project_result.reason, project_result.evidence))
        if not project_result.ok or project_result.project is None:
            return failed("S2", project_result.failure, project_result.reason, project=project_result.project)
        project = project_result.project

        chat_result = start_new_chat(
            self.session,
            project,
            client_ref=client_ref,
            timeout_seconds=chat_timeout_seconds,
            poll_interval_seconds=min(0.25, max(0.01, poll_interval_seconds)),
        )
        stages.append(_stage("S3", "NEW_CHAT_READY", chat_result.status.value, chat_result.ok, chat_result.failure, chat_result.reason, chat_result.evidence))
        if not chat_result.ok or chat_result.chat is None:
            return failed("S3", chat_result.failure, chat_result.reason, project=project, chat=chat_result.chat)
        chat = chat_result.chat

        raw_bytes = attachment_path.read_bytes()
        descriptor = ({
            "path": str(attachment_path),
            "filename": attachment_path.name,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "media_type": "text/markdown",
            "size_bytes": len(raw_bytes),
        },)
        attached = attach_files(
            self.session,
            chat,
            descriptor,
            timeout_seconds=attachment_timeout_seconds,
            poll_interval_seconds=min(0.1, max(0.01, poll_interval_seconds)),
        )
        stages.append(_stage("S4", "ATTACHMENT_ENTITY_VERIFIED", attached.status.value, attached.ok, attached.failure, attached.reason, attached.evidence))
        if not attached.ok:
            return failed("S4", attached.failure, attached.reason, project=project, chat=chat, attachment_evidence=attached.evidence)

        prompt = _build_stage2_attachment_prompt(artifact_name)
        response_result = submit_prompt_and_wait_for_response(
            self.session,
            chat,
            prompt,
            attachments=(),
            submit_timeout_seconds=submit_timeout_seconds,
            generation_timeout_seconds=generation_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
        )
        submission_ok = response_result.submission is not None and response_result.submission.ok
        stages.append(_stage("S5", "PROMPT_SUBMITTED", response_result.submission.status.value if response_result.submission else WebOperationStatus.FAILED.value, submission_ok, response_result.submission.failure if response_result.submission else response_result.failure, response_result.submission.reason if response_result.submission else response_result.reason, response_result.submission.evidence if response_result.submission else response_result.evidence))
        if not submission_ok:
            return failed("S5", response_result.failure, response_result.reason, project=project, chat=response_result.chat or chat, generation_state=response_result.generation_state, attachment_evidence=attached.evidence)

        stages.append(_stage("S6", "RESPONSE_COMPLETED", response_result.status.value, response_result.completed, response_result.failure, response_result.reason, response_result.evidence))
        if not response_result.completed or response_result.response is None:
            return failed("S6", response_result.failure, response_result.reason, project=project, chat=response_result.chat or chat, generation_state=response_result.generation_state, response=response_result.response, attachment_evidence=attached.evidence)
        response = response_result.response

        response_marker_ok = marker in response.text
        stages.append(VerticalSliceStage("S7", "ATTACHMENT_MARKER_READ", "VERIFIED" if response_marker_ok else "FAILED", response_marker_ok, None if response_marker_ok else FailureKind.POSTCONDITION_NOT_ESTABLISHED.value, None if response_marker_ok else "Assistant response did not contain the marker from the attachment.", (f"marker_sha256={hashlib.sha256(marker.encode('utf-8')).hexdigest()}",)))
        if not response_marker_ok:
            return failed("S7", FailureKind.POSTCONDITION_NOT_ESTABLISHED, "Assistant response did not contain the marker from the attachment.", project=project, chat=response_result.chat or chat, generation_state=response_result.generation_state, response=response, attachment_evidence=attached.evidence)

        discovery = self._wait_for_artifact(response, artifact_name, timeout_seconds=artifact_timeout_seconds, poll_interval_seconds=poll_interval_seconds)
        discovery_ok = not isinstance(discovery, ArtifactDiscoveryFailure) and discovery.ok
        stages.append(_stage("S8", "ARTIFACT_BOUND", "BOUND" if discovery_ok else WebOperationStatus.FAILED.value, discovery_ok, discovery.failure, discovery.reason, discovery.evidence))
        if not discovery_ok:
            return failed("S8", discovery.failure, discovery.reason, project=project, chat=response_result.chat or chat, generation_state=response_result.generation_state, response=response, response_marker_verified=True, attachment_evidence=attached.evidence)

        download = download_zip_artifact(self.session, response, output_dir, expected_filename=artifact_name)
        stages.append(_stage("S9", "ZIP_DOWNLOADED", download.status.value, download.ok, download.failure, download.reason, download.evidence))
        if not download.ok or download.artifact is None:
            return failed("S9", download.failure, download.reason, project=project, chat=response_result.chat or chat, generation_state=response_result.generation_state, response=response, artifact_result=download, response_marker_verified=True, attachment_evidence=attached.evidence)

        zip_marker_ok, zip_reason = _verify_stage2_marker_zip(Path(download.artifact.saved_path), marker)
        stages.append(VerticalSliceStage("S10", "ZIP_MARKER_VERIFIED", "VERIFIED" if zip_marker_ok else "FAILED", zip_marker_ok, None if zip_marker_ok else FailureKind.POSTCONDITION_NOT_ESTABLISHED.value, None if zip_marker_ok else zip_reason, ("member=marker.txt",)))
        if not zip_marker_ok:
            return failed("S10", FailureKind.POSTCONDITION_NOT_ESTABLISHED, zip_reason, project=project, chat=response_result.chat or chat, generation_state=response_result.generation_state, response=response, artifact_result=download, response_marker_verified=True, attachment_evidence=attached.evidence)

        return AttachmentVerticalSliceSmokeResult(
            result="PASS",
            stages=tuple(stages),
            attachment_filename=attachment_path.name,
            expected_marker=marker,
            expected_artifact=artifact_name,
            project=project,
            chat=response_result.chat or chat,
            generation_state=response_result.generation_state,
            response=response,
            artifact_result=download,
            attachment_evidence=attached.evidence,
            response_marker_verified=True,
            zip_marker_verified=True,
            reason="Native picker bridge attachment vertical slice passed.",
        )

    def run_vertical_slice_smoke(
        self,
        *,
        project_name: str,
        prompt: str,
        expected_artifact: str,
        output_dir: str | Path,
        project_key: str | None = None,
        project_url: str | None = None,
        client_ref: str = "m1-web-005-smoke",
        project_timeout_seconds: float = 15.0,
        chat_timeout_seconds: float = 15.0,
        submit_timeout_seconds: float = 10.0,
        generation_timeout_seconds: float = 900.0,
        artifact_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.5,
        stable_samples_required: int = 3,
    ) -> VerticalSliceSmokeResult:
        stages: list[VerticalSliceStage] = []

        bind = self.session.bind_chatgpt_page()
        stages.append(_stage("S1", "SESSION_BOUND", bind.status.value, bind.ok, bind.failure, bind.reason, bind.evidence))
        if not bind.ok:
            return _failed(stages, "S1", bind.failure, bind.reason)

        project_result = open_project(
            self.session,
            project_name,
            project_key=project_key,
            project_url=project_url,
            timeout_seconds=project_timeout_seconds,
            poll_interval_seconds=min(0.25, max(0.01, poll_interval_seconds)),
        )
        stages.append(
            _stage(
                "S2",
                "PROJECT_VERIFIED",
                project_result.status.value,
                project_result.ok,
                project_result.failure,
                project_result.reason,
                project_result.evidence,
            )
        )
        if not project_result.ok or project_result.project is None:
            return _failed(stages, "S2", project_result.failure, project_result.reason)
        project = project_result.project

        chat_result = start_new_chat(
            self.session,
            project,
            client_ref=client_ref,
            timeout_seconds=chat_timeout_seconds,
            poll_interval_seconds=min(0.25, max(0.01, poll_interval_seconds)),
        )
        stages.append(
            _stage(
                "S3",
                "NEW_CHAT_READY",
                chat_result.status.value,
                chat_result.ok,
                chat_result.failure,
                chat_result.reason,
                chat_result.evidence,
            )
        )
        if not chat_result.ok or chat_result.chat is None:
            return _failed(stages, "S3", chat_result.failure, chat_result.reason, project=project)

        response_result = submit_prompt_and_wait_for_response(
            self.session,
            chat_result.chat,
            prompt,
            submit_timeout_seconds=submit_timeout_seconds,
            generation_timeout_seconds=generation_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            stable_samples_required=stable_samples_required,
        )
        submission_ok = response_result.submission is not None and response_result.submission.ok
        stages.append(
            _stage(
                "S4",
                "PROMPT_SUBMITTED",
                response_result.submission.status.value if response_result.submission is not None else WebOperationStatus.FAILED.value,
                submission_ok,
                response_result.submission.failure if response_result.submission is not None else response_result.failure,
                response_result.submission.reason if response_result.submission is not None else response_result.reason,
                response_result.submission.evidence if response_result.submission is not None else response_result.evidence,
            )
        )
        if not submission_ok:
            return _failed(
                stages,
                "S4",
                response_result.failure,
                response_result.reason,
                project=project,
                chat=response_result.chat or chat_result.chat,
                generation_state=response_result.generation_state,
            )

        generation_started = response_result.generation_state in {
            GenerationState.STARTED,
            GenerationState.RUNNING,
            GenerationState.COMPLETED,
            GenerationState.USER_STOPPED,
        }
        stages.append(
            _stage(
                "S5",
                "GENERATION_STARTED",
                response_result.generation_state.value,
                generation_started,
                None if generation_started else response_result.failure,
                None if generation_started else response_result.reason,
                response_result.evidence,
            )
        )
        if not generation_started:
            return _failed(
                stages,
                "S5",
                response_result.failure,
                response_result.reason,
                project=project,
                chat=response_result.chat,
                generation_state=response_result.generation_state,
            )

        stages.append(
            _stage(
                "S6",
                "RESPONSE_COMPLETED",
                response_result.status.value,
                response_result.completed,
                response_result.failure,
                response_result.reason,
                response_result.evidence,
            )
        )
        if not response_result.completed:
            return _failed(
                stages,
                "S6",
                response_result.failure,
                response_result.reason,
                project=project,
                chat=response_result.chat,
                generation_state=response_result.generation_state,
                response=response_result.response,
            )

        response = response_result.response
        assert response is not None
        stages.append(
            VerticalSliceStage(
                stage_id="S7",
                name="RESPONSE_IDENTIFIED",
                status="IDENTIFIED",
                ok=True,
                observations=(
                    f"role_index={response.identity.role_index}",
                    f"semantic_text_sha256={response.identity.semantic_text_sha256}",
                    f"message_id_present={response.identity.message_id_sha256 is not None}",
                ),
            )
        )

        discovery = self._wait_for_artifact(
            response,
            expected_artifact,
            timeout_seconds=artifact_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        discovery_ok = not isinstance(discovery, ArtifactDiscoveryFailure) and discovery.ok
        if isinstance(discovery, ArtifactDiscoveryFailure):
            d_failure, d_reason, d_evidence = discovery.failure, discovery.reason, discovery.evidence
            d_status = WebOperationStatus.FAILED.value
        else:
            d_failure, d_reason, d_evidence = discovery.failure, discovery.reason, discovery.evidence
            d_status = "BOUND" if discovery.ok else WebOperationStatus.FAILED.value
        stages.append(_stage("S8", "ARTIFACT_BOUND", d_status, discovery_ok, d_failure, d_reason, d_evidence))
        if not discovery_ok:
            return _failed(
                stages,
                "S8",
                d_failure,
                d_reason,
                project=project,
                chat=response_result.chat,
                generation_state=response_result.generation_state,
                response=response,
            )

        download = download_zip_artifact(
            self.session,
            response,
            output_dir,
            expected_filename=expected_artifact,
        )
        stages.append(
            _stage(
                "S9",
                "ZIP_DOWNLOADED",
                download.status.value,
                download.ok,
                download.failure,
                download.reason,
                download.evidence,
            )
        )
        if not download.ok:
            return _failed(
                stages,
                "S9",
                download.failure,
                download.reason,
                project=project,
                chat=response_result.chat,
                generation_state=response_result.generation_state,
                response=response,
                artifact_result=download,
            )

        return VerticalSliceSmokeResult(
            result="PASS",
            stages=tuple(stages),
            project=project,
            chat=response_result.chat,
            generation_state=response_result.generation_state,
            response=response,
            artifact_result=download,
        )

    def _wait_for_artifact(
        self,
        response: AssistantResponse,
        expected_filename: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> ArtifactDiscoveryResult | ArtifactDiscoveryFailure:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            result = discover_zip_artifact(self.session, response, expected_filename=expected_filename)
            if result.ok:
                return result
            if result.failure != FailureKind.ARTIFACT_NOT_FOUND:
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(max(0.01, poll_interval_seconds))


def _stage(
    stage_id: str,
    name: str,
    status: str,
    ok: bool,
    failure: FailureKind | None,
    reason: str | None,
    evidence: WebDebugEvidence | None,
) -> VerticalSliceStage:
    return VerticalSliceStage(
        stage_id=stage_id,
        name=name,
        status=status,
        ok=ok,
        failure=failure.value if failure is not None else None,
        reason=reason,
        observations=tuple(evidence.observations) if evidence is not None else (),
    )


def _failed(
    stages: list[VerticalSliceStage],
    failure_stage: str,
    failure: FailureKind | None,
    reason: str | None,
    *,
    project: ProjectIdentity | None = None,
    chat: ChatIdentity | None = None,
    generation_state: GenerationState | None = None,
    response: AssistantResponse | None = None,
    artifact_result: ArtifactDownloadResult | None = None,
) -> VerticalSliceSmokeResult:
    return VerticalSliceSmokeResult(
        result="FAIL",
        stages=tuple(stages),
        project=project,
        chat=chat,
        generation_state=generation_state,
        response=response,
        artifact_result=artifact_result,
        failure_stage=failure_stage,
        failure=failure,
        reason=reason,
    )




def _build_stage2_attachment_prompt(expected_artifact: str) -> str:
    return (
        "Read the attached Markdown file. Find its `marker:` field. "
        f"Reply with that exact marker, then create a ZIP named `{expected_artifact}` containing a UTF-8 file `marker.txt` whose contents are exactly the marker value followed by one newline. "
        "Do not infer or invent the marker from this instruction; use the attached file."
    )

def _verify_stage2_marker_zip(path: Path, marker: str) -> tuple[bool, str | None]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if names.count("marker.txt") != 1:
                return False, "Stage-2 ZIP must contain exactly one marker.txt at archive root."
            info = archive.getinfo("marker.txt")
            if info.file_size > 65536:
                return False, "Stage-2 marker.txt is unexpectedly large."
            payload = archive.read("marker.txt")
    except Exception as exc:
        return False, f"Stage-2 ZIP marker verification failed: {type(exc).__name__}: {exc}"
    expected = (marker + "\n").encode("utf-8")
    if payload != expected:
        return False, "Stage-2 marker.txt contents did not exactly match the attachment marker."
    return True, None

def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


__all__ = [
    "ChatGPTWebFacadeV2",
    "create_facade",
    "get_facade_contract",
    "ChatGPTWebAPI",
    "VerticalSliceSmokeResult",
    "VerticalSliceStage",
    "prompt_sha256",
]
