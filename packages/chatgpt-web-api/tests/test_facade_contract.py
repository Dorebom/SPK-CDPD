from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from strict_fakes import make_bound_session

from chatgpt_web_api import api as public_api
from chatgpt_web_api.facade_contract import (
    CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP,
    CAPABILITY_PROJECT_CHAT_OPEN,
    CAPABILITY_PROMPT_EXECUTE_AND_WAIT,
    CONTRACT_MAJOR_VERSION,
    CONTRACT_NAME,
    REQUIRED_CAPABILITIES,
    FacadeContractFailureKind,
    FacadeContractViolation,
    validate_facade_contract,
    validate_operation_result,
)
from chatgpt_web_api.models import (
    ArtifactDownloadResult,
    ArtifactMetadata,
    AssistantResponse,
    ChatIdentity,
    ChatOpenResult,
    ComposerState,
    FacadePostcondition,
    FailureKind,
    GenerationState,
    MessageIdentity,
    MessageRole,
    PageIdentity,
    ProjectChatOpenResult,
    ProjectIdentity,
    ProjectOpenResult,
    PromptResponseResult,
    PromptSubmissionResult,
    ResponseZipDownloadResult,
    WebDebugEvidence,
    WebOperationEvidenceRecord,
    WebOperationStatus,
)


def _message_identity() -> MessageIdentity:
    return MessageIdentity(
        project_key="g-p-contract",
        conversation_id="conv-contract",
        role=MessageRole.ASSISTANT,
        role_index=0,
        semantic_text_sha256="a" * 64,
        message_id_sha256="b" * 64,
    )


def _chat() -> ChatIdentity:
    return ChatIdentity(
        project_key="g-p-contract",
        url="https://chatgpt.com/g/g-p-contract/c/conv-contract",
        conversation_id="conv-contract",
        client_ref="execution-M1-IFC-001",
    )


def _project() -> ProjectIdentity:
    return ProjectIdentity(
        requested_name="SPK-SHIRABE",
        matched_name="SPK-SHIRABE",
        project_key="g-p-contract",
        url="https://chatgpt.com/g/g-p-contract",
    )


def _evidence(operation: str) -> WebDebugEvidence:
    return WebDebugEvidence(operation=operation, observations=("contract-test=true",))


def _operation_record(identity: MessageIdentity, artifact: ArtifactMetadata) -> WebOperationEvidenceRecord:
    return WebOperationEvidenceRecord(
        operation_id="op-contract",
        operation="artifacts.download",
        outcome="DOWNLOADED",
        started_at="2026-08-13T00:00:00Z",
        completed_at="2026-08-13T00:00:01Z",
        project_key=identity.project_key,
        conversation_id=identity.conversation_id,
        response_identity=identity,
        artifact=artifact,
    )


def test_provider_exposes_one_explicit_v2_contract_without_browser_access() -> None:
    facade = public_api.create_facade(session=object())  # type: ignore[arg-type]

    assert facade.contract_name == CONTRACT_NAME == "chatgpt-web.facade"
    assert facade.contract_version == CONTRACT_MAJOR_VERSION == 2
    assert facade.capabilities == REQUIRED_CAPABILITIES
    assert public_api.get_facade_contract().capabilities == REQUIRED_CAPABILITIES
    assert validate_facade_contract(facade) is public_api.get_facade_contract()


def test_provider_contract_has_exact_alias_free_request_surface() -> None:
    facade = public_api.create_facade(session=object())  # type: ignore[arg-type]
    spec = validate_facade_contract(facade)

    assert spec.operation(CAPABILITY_PROJECT_CHAT_OPEN).request_fields == (
        "project_name",
        "project_key",
        "client_ref",
        "reuse",
    )
    assert spec.operation(CAPABILITY_PROMPT_EXECUTE_AND_WAIT).request_fields == ("chat", "prompt", "attachments")
    assert spec.operation(CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP).request_fields == (
        "response",
        "expected_filename",
        "destination_dir",
    )
    assert spec.operation(CAPABILITY_PROJECT_CHAT_OPEN).result_fields == (
        "ok",
        "project_identity",
        "chat_identity",
        "postcondition",
        "failure_kind",
        "reason",
        "operation_evidence",
    )
    assert spec.operation(CAPABILITY_PROMPT_EXECUTE_AND_WAIT).result_fields == (
        "submission",
        "completed",
        "response",
        "generation_state",
        "failure_kind",
        "reason",
        "operation_evidence",
    )
    assert spec.operation(CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP).result_fields == (
        "ok",
        "artifact",
        "response_binding_verified",
        "failure_kind",
        "reason",
        "operation_evidence",
    )


def test_api_all_declares_normative_surface_and_excludes_lower_level_reexports() -> None:
    assert {"ChatGPTWebFacadeV2", "create_facade", "get_facade_contract"} <= set(public_api.__all__)
    assert "open_project" not in public_api.__all__
    assert "start_new_chat" not in public_api.__all__
    assert "submit_prompt_and_wait_for_response" not in public_api.__all__
    assert "download_zip_artifact" not in public_api.__all__


def test_unknown_major_version_is_rejected_fail_closed() -> None:
    facade = public_api.create_facade(session=object())  # type: ignore[arg-type]
    with pytest.raises(FacadeContractViolation) as exc_info:
        validate_facade_contract(facade, expected_major_version=1)
    assert exc_info.value.kind is FacadeContractFailureKind.UNSUPPORTED_MAJOR_VERSION


def test_missing_required_capability_is_rejected_fail_closed() -> None:
    facade = public_api.create_facade(session=object())  # type: ignore[arg-type]
    broken = SimpleNamespace(
        contract_name=facade.contract_name,
        contract_version=facade.contract_version,
        capabilities=facade.capabilities - {CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP},
        open_project_chat=facade.open_project_chat,
        execute_prompt_and_wait=facade.execute_prompt_and_wait,
        download_response_zip=facade.download_response_zip,
    )
    with pytest.raises(FacadeContractViolation) as exc_info:
        validate_facade_contract(broken)
    assert exc_info.value.kind is FacadeContractFailureKind.MISSING_CAPABILITY


def test_success_result_without_required_evidence_is_contract_violation() -> None:
    result = ProjectChatOpenResult(
        status=WebOperationStatus.READY,
        project_identity=_project(),
        chat_identity=_chat(),
        postcondition=FacadePostcondition.TARGET_PROJECT_AND_CHAT_READY,
        operation_evidence=None,
    )
    assert result.ok
    with pytest.raises(FacadeContractViolation) as exc_info:
        validate_operation_result(CAPABILITY_PROJECT_CHAT_OPEN, result)
    assert exc_info.value.kind is FacadeContractFailureKind.RESULT_SEMANTIC_INVALID


def test_success_artifact_without_response_binding_is_contract_violation(tmp_path: Path) -> None:
    identity = _message_identity()
    artifact = ArtifactMetadata(
        artifact_id="artifact-contract",
        filename="candidate.zip",
        saved_path=str(tmp_path / "candidate.zip"),
        size_bytes=10,
        sha256="c" * 64,
        response_identity=identity,
    )
    result = ResponseZipDownloadResult(
        ok=True,
        artifact=artifact,
        response_binding_verified=False,
        operation_evidence=_operation_record(identity, artifact),
    )
    with pytest.raises(FacadeContractViolation) as exc_info:
        validate_operation_result(
            CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP,
            result,
            request={
                "response": AssistantResponse(identity=identity, text="done"),
                "expected_filename": "candidate.zip",
                "destination_dir": tmp_path,
            },
        )
    assert exc_info.value.kind is FacadeContractFailureKind.RESULT_SEMANTIC_INVALID


def test_open_project_chat_normalizes_lower_results_to_facade_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    chat = _chat()
    composer = ComposerState(present=True, visible=True, editable=True, empty=True)

    monkeypatch.setattr(
        public_api,
        "open_project",
        lambda *args, **kwargs: ProjectOpenResult(
            status=WebOperationStatus.OPENED,
            project=project,
            page=PageIdentity(url=project.url),
            evidence=_evidence("projects.open"),
        ),
    )
    monkeypatch.setattr(
        public_api,
        "start_new_chat",
        lambda *args, **kwargs: ChatOpenResult(
            status=WebOperationStatus.READY,
            chat=chat,
            composer=composer,
            page=PageIdentity(url=chat.url),
            evidence=_evidence("chats.start_new"),
        ),
    )

    result = public_api.create_facade(session=make_bound_session()).open_project_chat(  # type: ignore[arg-type]
        project_name="SPK-SHIRABE",
        project_key="g-p-contract",
        client_ref="execution-M1-IFC-001",
        reuse=False,
    )
    assert result.ok
    assert result.project_identity == project
    assert result.chat_identity == chat
    assert result.postcondition is FacadePostcondition.TARGET_PROJECT_AND_CHAT_READY


def test_execute_prompt_normalizes_required_completion_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _chat()
    identity = _message_identity()
    response = AssistantResponse(identity=identity, text="completed")
    submission = PromptSubmissionResult(
        status=WebOperationStatus.SUBMITTED,
        chat=chat,
        user_message=SimpleNamespace(),  # only .ok semantics are consumed by the facade
        page=PageIdentity(url=chat.url),
        evidence=_evidence("generation.submit"),
    )
    raw = PromptResponseResult(
        status=WebOperationStatus.COMPLETED,
        generation_state=GenerationState.COMPLETED,
        chat=chat,
        submission=submission,
        response=response,
        page=PageIdentity(url=chat.url),
        evidence=_evidence("generation.wait"),
    )
    monkeypatch.setattr(public_api, "submit_prompt_and_wait_for_response", lambda *args, **kwargs: raw)

    result = public_api.create_facade(session=make_bound_session()).execute_prompt_and_wait(  # type: ignore[arg-type]
        chat=chat,
        prompt="execute ticket",
        attachments=(),
    )
    assert result.submission.ok
    assert result.completed
    assert result.response == response
    assert result.failure_kind is None


def test_download_response_zip_enforces_exact_filename_integrity_and_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = _message_identity()
    response = AssistantResponse(identity=identity, text="artifact ready")
    artifact = ArtifactMetadata(
        artifact_id="artifact-contract",
        filename="candidate.zip",
        saved_path=str(tmp_path / "candidate.zip"),
        size_bytes=123,
        sha256="d" * 64,
        response_identity=identity,
        content_type="application/zip",
    )
    raw = ArtifactDownloadResult(
        status=WebOperationStatus.DOWNLOADED,
        artifact=artifact,
        response_identity=identity,
        page=PageIdentity(url="https://chatgpt.com/c/conv-contract"),
        operation_evidence=_operation_record(identity, artifact),
    )
    monkeypatch.setattr(public_api, "download_zip_artifact", lambda *args, **kwargs: raw)

    result = public_api.create_facade(session=make_bound_session()).download_response_zip(  # type: ignore[arg-type]
        response=response,
        expected_filename="candidate.zip",
        destination_dir=tmp_path,
    )
    assert result.ok
    assert result.response_binding_verified
    assert result.artifact == artifact
