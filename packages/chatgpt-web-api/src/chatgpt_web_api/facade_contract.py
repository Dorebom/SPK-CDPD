from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .models import (
    FacadePostcondition,
    MessageRole,
    ProjectChatOpenResult,
    PromptExecutionResult,
    ResponseZipDownloadResult,
)

CONTRACT_NAME = "chatgpt-web.facade"
CONTRACT_MAJOR_VERSION = 2

CAPABILITY_PROJECT_CHAT_OPEN = "project_chat.open"
CAPABILITY_PROMPT_EXECUTE_AND_WAIT = "prompt.execute_and_wait"
CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP = "artifact.download_response_zip"

REQUIRED_CAPABILITIES = frozenset(
    {
        CAPABILITY_PROJECT_CHAT_OPEN,
        CAPABILITY_PROMPT_EXECUTE_AND_WAIT,
        CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP,
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FacadeContractFailureKind(str, Enum):
    CONTRACT_NAME_MISMATCH = "CONTRACT_NAME_MISMATCH"
    UNSUPPORTED_MAJOR_VERSION = "UNSUPPORTED_MAJOR_VERSION"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    MISSING_OPERATION = "MISSING_OPERATION"
    REQUEST_SHAPE_MISMATCH = "REQUEST_SHAPE_MISMATCH"
    REQUEST_SEMANTIC_INVALID = "REQUEST_SEMANTIC_INVALID"
    RESULT_SHAPE_MISMATCH = "RESULT_SHAPE_MISMATCH"
    RESULT_SEMANTIC_INVALID = "RESULT_SEMANTIC_INVALID"


class FacadeContractViolation(RuntimeError):
    """Fail-closed signal for an invalid provider/request/result boundary."""

    def __init__(self, kind: FacadeContractFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class OperationContract:
    capability: str
    method_name: str
    request_fields: tuple[str, ...]
    result_fields: tuple[str, ...]
    result_type: type[Any]


@dataclass(frozen=True)
class FacadeContractSpec:
    name: str
    major_version: int
    operations: tuple[OperationContract, ...]

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(operation.capability for operation in self.operations)

    def operation(self, capability: str) -> OperationContract:
        for operation in self.operations:
            if operation.capability == capability:
                return operation
        raise FacadeContractViolation(
            FacadeContractFailureKind.MISSING_CAPABILITY,
            f"Unknown facade capability: {capability}",
        )


WEB_FACADE_CONTRACT_V2 = FacadeContractSpec(
    name=CONTRACT_NAME,
    major_version=CONTRACT_MAJOR_VERSION,
    operations=(
        OperationContract(
            capability=CAPABILITY_PROJECT_CHAT_OPEN,
            method_name="open_project_chat",
            request_fields=("project_name", "project_key", "client_ref", "reuse"),
            result_fields=(
                "ok",
                "project_identity",
                "chat_identity",
                "postcondition",
                "failure_kind",
                "reason",
                "operation_evidence",
            ),
            result_type=ProjectChatOpenResult,
        ),
        OperationContract(
            capability=CAPABILITY_PROMPT_EXECUTE_AND_WAIT,
            method_name="execute_prompt_and_wait",
            request_fields=("chat", "prompt", "attachments"),
            result_fields=(
                "submission",
                "completed",
                "response",
                "generation_state",
                "failure_kind",
                "reason",
                "operation_evidence",
            ),
            result_type=PromptExecutionResult,
        ),
        OperationContract(
            capability=CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP,
            method_name="download_response_zip",
            request_fields=("response", "expected_filename", "destination_dir"),
            result_fields=(
                "ok",
                "artifact",
                "response_binding_verified",
                "failure_kind",
                "reason",
                "operation_evidence",
            ),
            result_type=ResponseZipDownloadResult,
        ),
    ),
)


def validate_facade_contract(
    facade: Any,
    *,
    expected_name: str = CONTRACT_NAME,
    expected_major_version: int = CONTRACT_MAJOR_VERSION,
    required_capabilities: frozenset[str] = REQUIRED_CAPABILITIES,
) -> FacadeContractSpec:
    """Validate provider identity, capabilities, and exact callable request shape.

    This is deliberately Browser-free.  A Stable consumer can run it during
    preflight before any Web/CDP side effect is possible.
    """

    if getattr(facade, "contract_name", None) != expected_name:
        raise FacadeContractViolation(
            FacadeContractFailureKind.CONTRACT_NAME_MISMATCH,
            f"Expected contract name {expected_name!r}.",
        )
    if getattr(facade, "contract_version", None) != expected_major_version:
        raise FacadeContractViolation(
            FacadeContractFailureKind.UNSUPPORTED_MAJOR_VERSION,
            f"Expected facade major version {expected_major_version}.",
        )

    capabilities = getattr(facade, "capabilities", None)
    if not isinstance(capabilities, (set, frozenset)):
        raise FacadeContractViolation(
            FacadeContractFailureKind.MISSING_CAPABILITY,
            "Facade capabilities must be an explicit set/frozenset.",
        )
    missing = sorted(required_capabilities - frozenset(capabilities))
    if missing:
        raise FacadeContractViolation(
            FacadeContractFailureKind.MISSING_CAPABILITY,
            "Facade is missing required capabilities: " + ", ".join(missing),
        )

    spec = WEB_FACADE_CONTRACT_V2
    for operation in spec.operations:
        method = getattr(facade, operation.method_name, None)
        if not callable(method):
            raise FacadeContractViolation(
                FacadeContractFailureKind.MISSING_OPERATION,
                f"Facade capability {operation.capability} requires method {operation.method_name}.",
            )
        _validate_callable_request_shape(method, operation)
    return spec


def validate_operation_request(capability: str, request: Mapping[str, Any]) -> None:
    operation = WEB_FACADE_CONTRACT_V2.operation(capability)
    actual = frozenset(request)
    expected = frozenset(operation.request_fields)
    if actual != expected:
        raise FacadeContractViolation(
            FacadeContractFailureKind.REQUEST_SHAPE_MISMATCH,
            f"{capability} request fields must be exactly {operation.request_fields}; got {tuple(request)}.",
        )

    if capability == CAPABILITY_PROJECT_CHAT_OPEN:
        _require_non_empty_string(request["project_name"], "project_name")
        _require_non_empty_string(request["project_key"], "project_key")
        _require_non_empty_string(request["client_ref"], "client_ref")
        if not isinstance(request["reuse"], bool):
            _request_invalid("reuse must be bool")
    elif capability == CAPABILITY_PROMPT_EXECUTE_AND_WAIT:
        if request["chat"] is None:
            _request_invalid("chat must be a verified Chat identity")
        _require_non_empty_string(request["prompt"], "prompt")
        attachments = request["attachments"]
        if not isinstance(attachments, (tuple, list)):
            _request_invalid("attachments must be a tuple/list")
        for index, item in enumerate(attachments):
            if not isinstance(item, Mapping):
                _request_invalid(f"attachments[{index}] must be a mapping")
            expected = {"path", "filename", "sha256", "media_type", "size_bytes"}
            if set(item) != expected:
                _request_invalid(f"attachments[{index}] fields must be exactly {sorted(expected)!r}")
            _require_non_empty_string(item["path"], f"attachments[{index}].path")
            _require_non_empty_string(item["filename"], f"attachments[{index}].filename")
            _require_non_empty_string(item["media_type"], f"attachments[{index}].media_type")
            sha = item["sha256"]
            if not isinstance(sha, str) or _SHA256_RE.fullmatch(sha) is None:
                _request_invalid(f"attachments[{index}].sha256 must be lowercase SHA-256")
            if not isinstance(item["size_bytes"], int) or item["size_bytes"] < 0:
                _request_invalid(f"attachments[{index}].size_bytes must be non-negative int")
    elif capability == CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP:
        if request["response"] is None:
            _request_invalid("response must be an Assistant response identity")
        _require_non_empty_string(request["expected_filename"], "expected_filename")
        destination = request["destination_dir"]
        if destination is None or not str(destination).strip():
            _request_invalid("destination_dir must be non-empty")


def validate_operation_result(
    capability: str,
    result: Any,
    *,
    request: Mapping[str, Any] | None = None,
) -> None:
    operation = WEB_FACADE_CONTRACT_V2.operation(capability)
    if not isinstance(result, operation.result_type):
        raise FacadeContractViolation(
            FacadeContractFailureKind.RESULT_SHAPE_MISMATCH,
            f"{capability} result must be {operation.result_type.__name__}; got {type(result).__name__}.",
        )
    for field_name in operation.result_fields:
        if not hasattr(result, field_name):
            raise FacadeContractViolation(
                FacadeContractFailureKind.RESULT_SHAPE_MISMATCH,
                f"{capability} result is missing required field {field_name!r}.",
            )

    if capability == CAPABILITY_PROJECT_CHAT_OPEN:
        _validate_project_chat_result(result)
    elif capability == CAPABILITY_PROMPT_EXECUTE_AND_WAIT:
        _validate_prompt_result(result)
    elif capability == CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP:
        _validate_artifact_result(result, request=request)


def _validate_callable_request_shape(method: Any, operation: OperationContract) -> None:
    signature = inspect.signature(method)
    params = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.name != "self"
    ]
    names = tuple(parameter.name for parameter in params)
    if names != operation.request_fields:
        raise FacadeContractViolation(
            FacadeContractFailureKind.REQUEST_SHAPE_MISMATCH,
            f"{operation.method_name} signature must expose exactly {operation.request_fields}; got {names}.",
        )
    if any(parameter.kind is not inspect.Parameter.KEYWORD_ONLY for parameter in params):
        raise FacadeContractViolation(
            FacadeContractFailureKind.REQUEST_SHAPE_MISMATCH,
            f"{operation.method_name} request fields must be keyword-only.",
        )


def _validate_project_chat_result(result: ProjectChatOpenResult) -> None:
    if result.ok:
        if result.project_identity is None or result.chat_identity is None:
            _result_invalid("project_chat.open ok=true requires project_identity and chat_identity")
        if result.postcondition is not FacadePostcondition.TARGET_PROJECT_AND_CHAT_READY:
            _result_invalid("project_chat.open ok=true requires TARGET_PROJECT_AND_CHAT_READY postcondition")
        if result.operation_evidence is None:
            _result_invalid("project_chat.open ok=true requires operation_evidence")
    elif result.failure_kind is None:
        _result_invalid("project_chat.open ok=false requires typed failure_kind")


def _validate_prompt_result(result: PromptExecutionResult) -> None:
    if result.completed:
        if not result.submission.ok:
            _result_invalid("prompt.execute_and_wait completed=true requires submission.ok=true")
        if result.response is None:
            _result_invalid("prompt.execute_and_wait completed=true requires response")
        identity = result.response.identity
        if identity.role is not MessageRole.ASSISTANT or identity.role_index < 0:
            _result_invalid("completed response requires Assistant role identity and non-negative role_index")
        durable = identity.message_id_sha256 or identity.semantic_text_sha256
        if not isinstance(durable, str) or _SHA256_RE.fullmatch(durable) is None:
            _result_invalid("completed response requires a durable SHA-256 identity/fingerprint")
        if result.operation_evidence is None:
            _result_invalid("prompt.execute_and_wait completed=true requires operation_evidence")
    elif result.failure_kind is None:
        _result_invalid("prompt.execute_and_wait completed=false requires typed failure_kind")


def _validate_artifact_result(
    result: ResponseZipDownloadResult,
    *,
    request: Mapping[str, Any] | None,
) -> None:
    if result.ok:
        if result.artifact is None:
            _result_invalid("artifact.download_response_zip ok=true requires artifact")
        if result.response_binding_verified is not True:
            _result_invalid("artifact.download_response_zip ok=true requires response_binding_verified=true")
        if _SHA256_RE.fullmatch(result.artifact.sha256) is None:
            _result_invalid("artifact.download_response_zip ok=true requires artifact SHA-256")
        if result.artifact.size_bytes < 0:
            _result_invalid("artifact.download_response_zip artifact size must be non-negative")
        if result.operation_evidence is None:
            _result_invalid("artifact.download_response_zip ok=true requires operation_evidence")
        if request is not None and result.artifact.filename != request.get("expected_filename"):
            _result_invalid("artifact filename does not match exact expected_filename")
    elif result.failure_kind is None:
        _result_invalid("artifact.download_response_zip ok=false requires typed failure_kind")


def _require_non_empty_string(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _request_invalid(f"{name} must be a non-empty string")


def _request_invalid(message: str) -> None:
    raise FacadeContractViolation(FacadeContractFailureKind.REQUEST_SEMANTIC_INVALID, message)


def _result_invalid(message: str) -> None:
    raise FacadeContractViolation(FacadeContractFailureKind.RESULT_SEMANTIC_INVALID, message)


__all__ = [
    "CAPABILITY_ARTIFACT_DOWNLOAD_RESPONSE_ZIP",
    "CAPABILITY_PROJECT_CHAT_OPEN",
    "CAPABILITY_PROMPT_EXECUTE_AND_WAIT",
    "CONTRACT_MAJOR_VERSION",
    "CONTRACT_NAME",
    "FacadeContractFailureKind",
    "FacadeContractSpec",
    "FacadeContractViolation",
    "OperationContract",
    "REQUIRED_CAPABILITIES",
    "WEB_FACADE_CONTRACT_V2",
    "WEB_FACADE_CONTRACT_CURRENT",
    "validate_facade_contract",
    "validate_operation_request",
    "validate_operation_result",
]

# v2 is the normative Mandatory Dogfood provider contract.
WEB_FACADE_CONTRACT_CURRENT = WEB_FACADE_CONTRACT_V2
