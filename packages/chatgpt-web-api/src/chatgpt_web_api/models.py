from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class WebOperationStatus(str, Enum):
    BOUND = "BOUND"
    OPENED = "OPENED"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    COMPLETED = "COMPLETED"
    USER_STOPPED = "USER_STOPPED"
    DOWNLOADED = "DOWNLOADED"
    FAILED = "FAILED"


class FailureKind(str, Enum):
    SESSION_BINDING_FAILURE = "SESSION_BINDING_FAILURE"
    TARGET_IDENTITY_MISMATCH = "TARGET_IDENTITY_MISMATCH"
    POSTCONDITION_NOT_ESTABLISHED = "POSTCONDITION_NOT_ESTABLISHED"
    AMBIGUOUS_OBSERVATION = "AMBIGUOUS_OBSERVATION"
    TIMEOUT = "TIMEOUT"
    USER_STOPPED = "USER_STOPPED"
    DISCONNECTED = "DISCONNECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_AMBIGUOUS = "ARTIFACT_AMBIGUOUS"
    ARTIFACT_INCOMPLETE = "ARTIFACT_INCOMPLETE"
    ARTIFACT_COLLISION = "ARTIFACT_COLLISION"
    ARTIFACT_DOWNLOAD_FAILURE = "ARTIFACT_DOWNLOAD_FAILURE"
    UNSAFE_FILENAME = "UNSAFE_FILENAME"
    UNKNOWN_WEB_STATE = "UNKNOWN_WEB_STATE"


class MessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class GenerationState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTED = "STARTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    USER_STOPPED = "USER_STOPPED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class FacadePostcondition(str, Enum):
    TARGET_PROJECT_AND_CHAT_READY = "TARGET_PROJECT_AND_CHAT_READY"


@dataclass(frozen=True)
class PageIdentity:
    url: str
    title: str | None = None
    target_id: str | None = None


@dataclass(frozen=True)
class WebDebugEvidence:
    operation: str
    observations: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionBindResult:
    status: WebOperationStatus
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.status == WebOperationStatus.BOUND and self.page is not None


@dataclass(frozen=True)
class ProjectIdentity:
    requested_name: str
    matched_name: str
    project_key: str
    url: str


@dataclass(frozen=True)
class ProjectOpenResult:
    status: WebOperationStatus
    project: ProjectIdentity | None
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.status == WebOperationStatus.OPENED and self.project is not None


@dataclass(frozen=True)
class ProjectObservation:
    """One Project identity observed through ChatGPT Web.

    A row can be visible yet unresolved.  `project_key=None` deliberately
    preserves that uncertainty rather than inventing an identity.
    """

    name: str
    project_key: str | None
    url: str | None = None
    source: str = "web"

    @property
    def resolved(self) -> bool:
        return bool(self.project_key)


@dataclass(frozen=True)
class ObservationStabilityProof:
    """Auditable proof that repeated Project observations converged."""

    proof_id: str
    sample_count: int
    first_observed_at: str
    last_observed_at: str
    identity_set_digest: str
    strategy_id: str
    stable: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "proof_id",
            "first_observed_at",
            "last_observed_at",
            "identity_set_digest",
            "strategy_id",
            "evidence_ref",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if (
            not isinstance(self.sample_count, int)
            or isinstance(self.sample_count, bool)
            or self.sample_count < 1
        ):
            raise ValueError("sample_count must be an integer >= 1")


@dataclass(frozen=True)
class ProjectInventoryBaseline:
    """Caller-owned expected identity set plus optional audit provenance.

    This model is part of the pre-completeness observation/proof foundation.
    Whether a baseline is sufficient to prove full inventory completeness is
    deliberately decided by the later completeness integration layer.
    """

    expected_project_keys: tuple[str, ...]
    exhaustive: bool = False
    baseline_id: str | None = None
    identity_set_digest: str | None = None
    authority: str | None = None
    source: str | None = None

    @property
    def audit_metadata_complete(self) -> bool:
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                self.baseline_id,
                self.identity_set_digest,
                self.authority,
                self.source,
            )
        )


@dataclass(frozen=True)
class ProjectObservationQueryResult:
    """Project observation result independent of completeness semantics."""

    projects: tuple[ProjectObservation, ...]
    observed_stable: bool
    page: PageIdentity | None = None
    evidence: WebDebugEvidence | None = None
    failure: FailureKind | None = None
    reason: str | None = None
    stability_proof: ObservationStabilityProof | None = None
    observed_identity_set_digest: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def stability_proof_id(self) -> str | None:
        return self.stability_proof.proof_id if self.stability_proof is not None else None


# M1-WEB-006 completeness model integration.
@dataclass(frozen=True)
class ProjectInventoryCompleteness:
    observed_count: int
    resolved_count: int
    observed_stable: bool
    all_observed_resolved: bool
    expected_identity_count: int | None
    known_identity_coverage_complete: bool | None
    missing_expected_project_keys: tuple[str, ...] = ()
    unexpected_project_keys: tuple[str, ...] = ()
    duplicate_project_keys: tuple[str, ...] = ()
    proof_exhaustive: bool = False
    full_inventory_verified: bool = False

    @property
    def complete(self) -> bool:
        """Compatibility-friendly alias with intentionally strict semantics."""
        return self.full_inventory_verified


@dataclass(frozen=True)
class ProjectInventoryQueryResult:
    projects: tuple[ProjectObservation, ...]
    completeness: ProjectInventoryCompleteness
    page: PageIdentity | None = None
    evidence: WebDebugEvidence | None = None
    failure: FailureKind | None = None
    reason: str | None = None
    stability_proof: ObservationStabilityProof | None = None
    expected_baseline: ProjectInventoryBaseline | None = None
    observed_identity_set_digest: str | None = None

    @property
    def complete(self) -> bool:
        return self.completeness.full_inventory_verified

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def stability_proof_id(self) -> str | None:
        return self.stability_proof.proof_id if self.stability_proof is not None else None

    @property
    def baseline_id(self) -> str | None:
        return self.expected_baseline.baseline_id if self.expected_baseline is not None else None


@dataclass(frozen=True)
class ComposerState:
    present: bool
    visible: bool
    editable: bool
    empty: bool
    text_length: int = 0

    @property
    def input_ready(self) -> bool:
        return self.present and self.visible and self.editable


@dataclass(frozen=True)
class ComposerReadinessResult:
    status: WebOperationStatus
    composer: ComposerState | None
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.status == WebOperationStatus.READY and self.composer is not None




@dataclass(frozen=True)
class ComposerAttachmentResult:
    status: WebOperationStatus
    filenames: tuple[str, ...]
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.status == WebOperationStatus.READY and bool(self.filenames)


@dataclass(frozen=True)
class AttachmentTransportObservationResult:
    """Stage-1 picker-transport observation; never assigns a file or Prompt."""

    status: WebOperationStatus
    stage: str
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.status == WebOperationStatus.READY and self.stage == "picker-transport-observed"


@dataclass(frozen=True)
class ChatIdentity:
    """Chat identity visible to callers without SHIRABE role semantics.

    A new, unsent Project Chat can exist before ChatGPT allocates a server-side
    conversation id. Such an identity is provisional and must not be used for
    restart/reconcile binding. Once a prompt is submitted and a durable
    conversation id is observed, M1-WEB-003 promotes the identity.
    """

    project_key: str
    url: str
    conversation_id: str | None = None
    provisional_id: str | None = None
    client_ref: str | None = None

    @property
    def provisional(self) -> bool:
        return self.conversation_id is None


@dataclass(frozen=True)
class ChatOpenResult:
    status: WebOperationStatus
    chat: ChatIdentity | None
    composer: ComposerState | None
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return (
            self.status == WebOperationStatus.READY
            and self.chat is not None
            and self.composer is not None
            and self.composer.input_ready
        )


@dataclass(frozen=True)
class ProjectChatOpenResult:
    """Versioned facade result for ``project_chat.open``.

    This is intentionally distinct from lower-level Project/Chat operation
    results so the public facade can preserve a stable semantic boundary while
    internal Web operations evolve independently.
    """

    status: WebOperationStatus
    project_identity: ProjectIdentity | None
    chat_identity: ChatIdentity | None
    postcondition: FacadePostcondition | None
    failure_kind: FailureKind | None = None
    reason: str | None = None
    operation_evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return (
            self.status == WebOperationStatus.READY
            and self.project_identity is not None
            and self.chat_identity is not None
            and self.postcondition is FacadePostcondition.TARGET_PROJECT_AND_CHAT_READY
        )


@dataclass(frozen=True)
class PromptExecutionSubmission:
    ok: bool
    activation_attempted: bool = False
    trusted_click: bool = False
    web_submit_established: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageIdentity:
    """Stable-enough Web message identity for rebinding later operations."""

    project_key: str
    conversation_id: str | None
    role: MessageRole
    role_index: int
    semantic_text_sha256: str
    message_id_sha256: str | None = None


@dataclass(frozen=True)
class Message:
    identity: MessageIdentity
    text: str


@dataclass(frozen=True)
class MessageSnapshot:
    project_key: str
    conversation_id: str | None
    messages: tuple[Message, ...]
    user_count: int
    assistant_count: int

    @property
    def latest_user(self) -> Message | None:
        for message in reversed(self.messages):
            if message.identity.role == MessageRole.USER:
                return message
        return None

    @property
    def latest_assistant(self) -> Message | None:
        for message in reversed(self.messages):
            if message.identity.role == MessageRole.ASSISTANT:
                return message
        return None


@dataclass(frozen=True)
class PromptSubmissionResult:
    status: WebOperationStatus
    chat: ChatIdentity | None
    user_message: Message | None
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return (
            self.status == WebOperationStatus.SUBMITTED
            and self.chat is not None
            and self.user_message is not None
        )


@dataclass(frozen=True)
class GenerationObservation:
    state: GenerationState
    stop_visible: bool
    user_stop_requested: bool
    streaming_visible: bool
    page: PageIdentity | None
    evidence: WebDebugEvidence | None = None


@dataclass(frozen=True)
class AssistantResponse:
    identity: MessageIdentity
    text: str


@dataclass(frozen=True)
class PromptExecutionResult:
    """Versioned facade result for ``prompt.execute_and_wait``."""

    submission: PromptExecutionSubmission
    completed: bool
    response: AssistantResponse | None
    generation_state: GenerationState
    failure_kind: FailureKind | None = None
    reason: str | None = None
    operation_evidence: WebDebugEvidence | None = None


@dataclass(frozen=True)
class PromptResponseResult:
    status: WebOperationStatus
    generation_state: GenerationState
    chat: ChatIdentity | None
    submission: PromptSubmissionResult | None
    response: AssistantResponse | None
    page: PageIdentity | None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def completed(self) -> bool:
        return self.status == WebOperationStatus.COMPLETED and self.response is not None

    @property
    def user_stopped(self) -> bool:
        return self.status == WebOperationStatus.USER_STOPPED


@dataclass(frozen=True)
class ArtifactCandidate:
    artifact_id: str
    filename: str
    response_identity: MessageIdentity
    candidate_class: str


@dataclass(frozen=True)
class ArtifactMetadata:
    artifact_id: str
    filename: str
    saved_path: str
    size_bytes: int
    sha256: str
    response_identity: MessageIdentity
    content_type: str | None = None


@dataclass(frozen=True)
class WebOperationEvidenceRecord:
    operation_id: str
    operation: str
    outcome: str
    started_at: str
    completed_at: str
    project_key: str | None
    conversation_id: str | None
    response_identity: MessageIdentity | None
    artifact: ArtifactMetadata | None
    failure: FailureKind | None = None
    reason: str | None = None
    observations: tuple[str, ...] = ()
    trace_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactDownloadResult:
    status: WebOperationStatus
    artifact: ArtifactMetadata | None
    response_identity: MessageIdentity | None
    page: PageIdentity | None
    operation_evidence: WebOperationEvidenceRecord | None = None
    failure: FailureKind | None = None
    reason: str | None = None
    evidence: WebDebugEvidence | None = None

    @property
    def ok(self) -> bool:
        return self.status == WebOperationStatus.DOWNLOADED and self.artifact is not None

@dataclass(frozen=True)
class ResponseZipDownloadResult:
    """Versioned facade result for ``artifact.download_response_zip``."""

    ok: bool
    artifact: ArtifactMetadata | None
    response_binding_verified: bool
    failure_kind: FailureKind | None = None
    reason: str | None = None
    operation_evidence: WebOperationEvidenceRecord | None = None

