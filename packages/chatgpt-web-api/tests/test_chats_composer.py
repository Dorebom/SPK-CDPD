from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chatgpt_web_api.chats import bind_chat, start_new_chat
from chatgpt_web_api.composer import (
    _CONTENTEDITABLE_PROMPT_INPUT_HELPERS_JS,
    _PROMPT_INPUT_SCRIPT,
    _PROMPT_SUBMIT_SCRIPT,
    _PROMPT_TEXT_CORRELATION_HELPERS_JS,
    _SEND_ACTION_DISPATCH_SCRIPT,
    attach_files,
    observe_attachment_transport,
    observe_composer_readiness,
    submit_prompt,
)
from chatgpt_web_api.models import (
    ChatIdentity,
    FailureKind,
    ProjectIdentity,
    WebOperationStatus,
)
from chatgpt_web_api.session import ChatGPTSession, _PlaywrightPageOps
from strict_fakes import make_bound_session, isolated_page_ops


@dataclass
class FakePage:
    _url: str
    focused: bool = True
    _target_id: str = "target-1"
    chat_observations: list[dict[str, Any]] = field(default_factory=list)
    composer_observations: list[dict[str, Any]] = field(default_factory=list)
    message_observations: list[dict[str, Any]] = field(default_factory=list)
    submit_actions: list[dict[str, Any]] = field(default_factory=list)
    trusted_click_actions: list[dict[str, Any]] = field(default_factory=list)
    attachment_observations: list[dict[str, Any]] = field(default_factory=list)
    attachment_baselines: list[dict[str, Any]] = field(default_factory=list)
    uploaded_files: list[tuple[str, ...]] = field(default_factory=list)
    attachment_transport_observations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return "ChatGPT"

    def target_id(self) -> str:
        return self._target_id

    def evaluate(self, expression: str, arg: Any | None = None) -> Any:
        # The real cleanup script is a successful no-op on these synthetic
        # Pages; an unmodelled exception would now correctly restrict mutation.
        if "removeAttribute('data-shirabe-send-action')" in expression:
            return None
        if "document.visibilityState" in expression:
            return self.focused
        if "SHIRABE_CHAT_OBSERVE_V1" in expression:
            return _next(self.chat_observations)
        if "SHIRABE_COMPOSER_OBSERVE_V1" in expression:
            return _next(self.composer_observations)
        if "SHIRABE_MESSAGE_OBSERVE_V1" in expression:
            row = dict(_next(self.message_observations))
            if row.get("url"):
                self._url = row["url"]
            return row
        if "SHIRABE_ATTACHMENT_OBSERVE_V2" in expression:
            if isinstance(arg, dict) and arg.get("phase") == "baseline":
                if self.attachment_baselines:
                    return _next(self.attachment_baselines)
                return {
                    "ok": False, "reason": "attachment-entity-identity-pending",
                    "observed": [], "expectedCount": len(arg.get("expectedNames") or []), "observedCount": 0,
                    "rootReason": "closest-form", "scannedVisibleCount": 4, "candidateCount": 0,
                    "candidates": [], "candidateSignatures": [], "newCandidateCount": 0,
                    "newCandidates": [], "newAttachmentCandidateCount": 0, "newAttachmentCandidates": [],
                }
            return _next(self.attachment_observations)
        if "SHIRABE_ATTACHMENT_OBSERVE_V1" in expression:
            return _next(self.attachment_observations)
        if "SHIRABE_PROMPT_INPUT_V1" in expression:
            return _next(self.submit_actions)
        if "SHIRABE_SEND_ACTION_SELECT_V2" in expression:
            return _next(self.submit_actions)
        if "SHIRABE_PROMPT_SUBMIT_V1" in expression:
            return _next(self.submit_actions)
        raise AssertionError(f"unexpected evaluate script: {expression[:100]}")

    def trusted_click_marked_action(self, selector: str, *, timeout_ms: int) -> dict[str, Any]:
        if self.trusted_click_actions:
            return _next(self.trusted_click_actions)
        return {
            "ok": True,
            "reason": "trusted-click-delivered",
            "activationAttempted": True,
            "trustedClick": True,
            "locatorCount": 1,
            "trustedClickEventCount": 1,
            "clickEventCount": 1,
        }

    def goto(self, url: str, *, timeout_ms: int) -> None:
        self._url = url

    def observe_composer_attachment_transport(self, **kwargs) -> dict[str, Any]:
        if self.attachment_transport_observations:
            return _next(self.attachment_transport_observations)
        return {
            "ok": True, "reason": "filechooser-observed",
            "transport": "composer-action-filechooser-v6", "stage": "filechooser-observed",
            "actionRootReason": "minimal-semantic-neighborhood", "actionRootCandidateCount": 1,
            "actionRootSelectedDepth": 2,
            "actionRootDetails": [{"depth":1,"rawInteractiveCount":0,"activeInteractiveCount":0,"attachmentCandidateCount":0,"logicalAttachmentCount":0},{"depth":2,"rawInteractiveCount":4,"activeInteractiveCount":3,"attachmentCandidateCount":1,"logicalAttachmentCount":1}],
            "attachmentActionReason": "single-attachment-action",
            "attachmentRawActionCount": 4, "attachmentActiveActionCount": 1, "attachmentLogicalActionCount": 1,
            "attachmentSelectedKind": "attachment-action", "attachmentSelectedSignature": "attachment-action|attach",
            "attachmentActionDetails": [{"kind":"attachment-action","aria":"attach files","signature":"attachment-action|attach"}],
            "attachmentClickReason": "filechooser-observed",
            "menuRootCount": 0, "menuRawActionCount": 0, "menuActiveActionCount": 0, "menuLogicalActionCount": 0,
            "fileChooserEventObserved": True, "fileChooserMultiple": False, "fileChooserAccept": ".md",
            "fileAssigned": False, "promptDispatched": False,
        }

    def upload_files_to_composer(self, file_paths, **kwargs) -> dict[str, Any]:
        values = tuple(str(path) for path in file_paths)
        self.uploaded_files.append(values)
        return {
            "ok": True,
            "reason": "files-assigned-via-filechooser",
            "transport": "composer-action-filechooser-v6",
            "stage": "filechooser-set-files",
            "attachmentActionReason": "single-attachment-action",
            "attachmentLogicalActionCount": 1,
            "fileChooserEventObserved": True,
            "fileChooserMultiple": False,
            "fileChooserAccept": ".md",
            "fileCount": len(values),
        }


class FakeTransport:
    def __init__(self, pages: list[FakePage]) -> None:
        self._pages = pages

    def connect(self, cdp_url: str) -> None:
        return None

    def pages(self) -> tuple[FakePage, ...]:
        return tuple(self._pages)

    def close(self) -> None:
        return None


def _next(values: list[dict[str, Any]]) -> dict[str, Any]:
    assert values, "fake observation sequence is empty"
    if len(values) > 1:
        return values.pop(0)
    return values[0]


def composer(
    *,
    empty: bool = True,
    editable: bool = True,
    count: int = 1,
    active_count: int | None = None,
    logical_count: int | None = None,
    selection_reason: str | None = None,
) -> dict[str, Any]:
    result = {
        "present": (logical_count if logical_count is not None else count) > 0,
        "visible": (active_count if active_count is not None else count) > 0,
        "editable": editable,
        "empty": empty,
        "textLength": 0 if empty else 5,
        "candidateCount": count,
    }
    if active_count is not None:
        result["activeCandidateCount"] = active_count
    if logical_count is not None:
        result["logicalCandidateCount"] = logical_count
    if selection_reason is not None:
        result["selectionReason"] = selection_reason
    return result


def chat_obs(url: str, key: str, *, conversation_id: str | None = None, messages: int = 0) -> dict[str, Any]:
    return {
        "url": url,
        "projectKey": key,
        "projectSegment": "ignored-by-python-observer",
        "conversationId": conversation_id,
        "isProjectRoot": conversation_id is None and "/c/" not in url and (url.rstrip('/').endswith('/project') or url.rstrip('/').endswith(key + '-spk-shirabe')),
        "messageCount": messages,
    }


def project(key: str = "g-p-abc123") -> ProjectIdentity:
    return ProjectIdentity(
        requested_name="SPK-SHIRABE",
        matched_name="SPK-SHIRABE",
        project_key=key,
        url=f"https://chatgpt.com/g/{key}-spk-shirabe/c/existing",
    )


def durable_chat(key: str = "g-p-abc123", conversation_id: str = "conv-1") -> ChatIdentity:
    return ChatIdentity(
        project_key=key,
        url=f"https://chatgpt.com/g/{key}-spk-shirabe/c/{conversation_id}",
        conversation_id=conversation_id,
        client_ref="execution",
    )


def message_rows(*rows: tuple[str, str, str | None], url: str | None = None) -> dict[str, Any]:
    return {
        "url": url or durable_chat().url,
        "rows": [{"role": role, "text": text, "rawId": raw_id} for role, text, raw_id in rows],
    }


def test_composer_readiness_succeeds_for_visible_editable_empty_surface() -> None:
    page = FakePage("https://chatgpt.com/", composer_observations=[composer(empty=True)])
    session = make_bound_session(transport=FakeTransport([page]))
    result = observe_composer_readiness(session, require_empty=True)
    assert result.ok
    assert result.status == WebOperationStatus.READY
    assert result.composer is not None and result.composer.input_ready and result.composer.empty


def test_composer_readiness_fails_closed_when_multiple_candidates_exist() -> None:
    page = FakePage("https://chatgpt.com/", composer_observations=[composer(count=2)])
    session = make_bound_session(transport=FakeTransport([page]))
    result = observe_composer_readiness(session)
    assert not result.ok
    assert result.failure == FailureKind.AMBIGUOUS_OBSERVATION


def test_composer_readiness_accepts_two_raw_candidates_when_they_resolve_to_one_logical_composer() -> None:
    page = FakePage(
        "https://chatgpt.com/",
        composer_observations=[
            composer(
                count=2,
                active_count=2,
                logical_count=1,
                selection_reason="logical-dedup",
            )
        ],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = observe_composer_readiness(session, require_empty=True)
    assert result.ok
    assert result.evidence is not None
    assert "composer.raw_candidate_count=2" in result.evidence.observations
    assert "composer.active_candidate_count=2" in result.evidence.observations
    assert "composer.logical_candidate_count=1" in result.evidence.observations
    assert "composer.selection_reason=logical-dedup" in result.evidence.observations


def test_composer_readiness_ignores_non_active_duplicate_when_one_logical_composer_remains() -> None:
    page = FakePage(
        "https://chatgpt.com/",
        composer_observations=[
            composer(
                count=2,
                active_count=1,
                logical_count=1,
                selection_reason="single-active-candidate",
            )
        ],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = observe_composer_readiness(session)
    assert result.ok
    assert result.evidence is not None
    assert "composer.raw_candidate_count=2" in result.evidence.observations
    assert "composer.active_candidate_count=1" in result.evidence.observations
    assert "composer.logical_candidate_count=1" in result.evidence.observations


def test_composer_readiness_still_fails_when_two_independent_logical_composers_remain() -> None:
    page = FakePage(
        "https://chatgpt.com/",
        composer_observations=[
            composer(
                count=2,
                active_count=2,
                logical_count=2,
                selection_reason="multiple-logical-composers",
            )
        ],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = observe_composer_readiness(session)
    assert not result.ok
    assert result.failure == FailureKind.AMBIGUOUS_OBSERVATION
    assert result.evidence is not None
    assert "composer.logical_candidate_count=2" in result.evidence.observations


def test_prompt_input_and_reconfirmation_share_one_correlation_implementation() -> None:
    assert "SHIRABE_PROMPT_CORRELATION_V3" in _PROMPT_TEXT_CORRELATION_HELPERS_JS
    assert "SHIRABE_DOM_PROMPT_SERIALIZER_V1" in _PROMPT_TEXT_CORRELATION_HELPERS_JS
    assert _PROMPT_TEXT_CORRELATION_HELPERS_JS in _PROMPT_INPUT_SCRIPT
    assert _PROMPT_TEXT_CORRELATION_HELPERS_JS in _SEND_ACTION_DISPATCH_SCRIPT
    assert ".replace(/\\r\\n?/g, '\\n')" in _PROMPT_TEXT_CORRELATION_HELPERS_JS
    assert ".replace(/\\u00A0/g, ' ')" in _PROMPT_TEXT_CORRELATION_HELPERS_JS
    assert "[\\u200B\\u200C\\u200D\\uFEFF]" in _PROMPT_TEXT_CORRELATION_HELPERS_JS
    assert "inputExpectedFingerprint" in _PROMPT_INPUT_SCRIPT
    assert "inputObservedFingerprint" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "inputFirstDifferenceIndex" in _SEND_ACTION_DISPATCH_SCRIPT


def test_prompt_correlation_uses_dom_aware_contenteditable_serialization() -> None:
    helpers = _PROMPT_TEXT_CORRELATION_HELPERS_JS
    assert "const serializePromptDomNode" in helpers
    assert "const serializeContentEditablePrompt" in helpers
    assert "if (tag === 'BR') return '\\n'" in helpers
    assert "PROMPT_BLOCK_TAGS.has(tag)" in helpers
    assert "'P', 'DIV', 'LI', 'PRE', 'BLOCKQUOTE'" in helpers
    assert "source = 'dom-serialized-contenteditable'" in helpers
    assert "serializationVersion = 'prompt-dom-v1'" in helpers
    assert "rawText = serializeContentEditablePrompt(el)" in helpers
    assert "innerText" not in helpers
    assert "textContent" not in helpers

def test_submit_prompt_records_reconfirmation_difference_diagnostics_without_prompt_body() -> None:
    prompt = "same length"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[composer()],
        message_observations=[message_rows(url=chat.url)],
        submit_actions=[
            {
                "ok": True,
                "reason": "prompt-input-established",
                "logicalCandidateCount": 1,
                "inputTextLength": len(prompt),
                "inputRawTextLength": len(prompt),
                "inputMatchesPrompt": True,
                "inputNormalizationVersion": "prompt-text-v3",
                "inputSerializationVersion": "prompt-dom-v1",
                "inputTextSource": "dom-serialized-contenteditable",
                "inputExpectedFingerprint": "aaaaaaaa",
                "inputObservedFingerprint": "aaaaaaaa",
                "inputFirstDifferenceIndex": -1,
            },
            {
                "ok": False,
                "reason": "prompt-input-not-established",
                "retryable": False,
                "logicalCandidateCount": 1,
                "inputTextLength": len(prompt),
                "inputRawTextLength": len(prompt),
                "inputMatchesPrompt": False,
                "inputNormalizationVersion": "prompt-text-v3",
                "inputSerializationVersion": "prompt-dom-v1",
                "inputTextSource": "dom-serialized-contenteditable",
                "inputExpectedFingerprint": "aaaaaaaa",
                "inputObservedFingerprint": "bbbbbbbb",
                "inputFirstDifferenceIndex": 4,
                "inputExpectedCodePoint": 32,
                "inputObservedCodePoint": 160,
            },
        ],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.POSTCONDITION_NOT_ESTABLISHED
    assert result.evidence is not None
    observations = result.evidence.observations
    assert "action.input.normalization_version=prompt-text-v3" in observations
    assert "action.input.serialization_version=prompt-dom-v1" in observations
    assert "action.input.text_source=dom-serialized-contenteditable" in observations
    assert "action.input.expected_fingerprint=aaaaaaaa" in observations
    assert "action.input.confirmed.normalization_version=prompt-text-v3" in observations
    assert "action.input.confirmed.serialization_version=prompt-dom-v1" in observations
    assert "action.input.confirmed.text_source=dom-serialized-contenteditable" in observations
    assert "action.input.confirmed.expected_fingerprint=aaaaaaaa" in observations
    assert "action.input.confirmed.observed_fingerprint=bbbbbbbb" in observations
    assert "action.input.confirmed.first_difference_index=4" in observations
    assert "action.input.confirmed.expected_codepoint=32" in observations
    assert "action.input.confirmed.observed_codepoint=160" in observations
    assert all(prompt not in item for item in observations)


def test_contenteditable_prompt_input_uses_native_editing_commands_without_textcontent_assignment() -> None:
    helpers = _CONTENTEDITABLE_PROMPT_INPUT_HELPERS_JS
    assert "SHIRABE_CONTENTEDITABLE_NATIVE_INPUT_V1" in helpers
    assert "document.createRange()" in helpers
    assert "window.getSelection()" in helpers
    assert "range.selectNodeContents(el)" in helpers
    assert "range.collapse(false)" in helpers
    assert "document.execCommand('insertText', false, line)" in helpers
    assert "document.execCommand('insertParagraph', false, null)" in helpers
    assert "el.addEventListener('beforeinput'" in helpers
    assert "el.addEventListener('input'" in helpers
    assert "contenteditable-exec-command-v1" in helpers
    assert "textContent = prompt" not in _PROMPT_INPUT_SCRIPT
    assert "el.innerHTML =" not in _PROMPT_INPUT_SCRIPT


def test_contenteditable_multiline_input_emits_native_method_diagnostics() -> None:
    prompt = "first line\nsecond line\n\nthird line"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[composer()],
        message_observations=[message_rows(url=chat.url)],
        submit_actions=[
            {
                "ok": False,
                "reason": "prompt-input-not-established",
                "logicalCandidateCount": 1,
                "selectionReason": "single-active-candidate",
                "inputMethod": "contenteditable-exec-command-v1",
                "inputSelectionEstablished": True,
                "inputLineBreakCount": 3,
                "inputInsertTextCommandCount": 3,
                "inputInsertParagraphCommandCount": 3,
                "inputBeforeInputEventCount": 3,
                "inputEventCount": 6,
                "inputTextLength": len(prompt),
                "inputRawTextLength": len(prompt),
                "inputMatchesPrompt": False,
                "inputNormalizationVersion": "prompt-text-v3",
                "inputSerializationVersion": "prompt-dom-v1",
                "inputTextSource": "dom-serialized-contenteditable",
                "inputExpectedFingerprint": "aaaaaaaa",
                "inputObservedFingerprint": "bbbbbbbb",
                "inputFirstDifferenceIndex": 10,
            },
        ],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.evidence is not None
    observations = result.evidence.observations
    assert "action.input.native.method=contenteditable-exec-command-v1" in observations
    assert "action.input.native.selection_established=True" in observations
    assert "action.input.native.line_break_count=3" in observations
    assert "action.input.native.insert_text_command_count=3" in observations
    assert "action.input.native.insert_paragraph_command_count=3" in observations
    assert "action.input.native.beforeinput_event_count=3" in observations
    assert "action.input.native.input_event_count=6" in observations


def test_prompt_input_and_send_action_are_split_across_browser_evaluations() -> None:
    assert "SHIRABE_PROMPT_INPUT_V1" in _PROMPT_INPUT_SCRIPT
    assert "SHIRABE_SEND_ACTION_SELECT_V2" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "actionRoot.querySelectorAll" not in _PROMPT_INPUT_SCRIPT
    assert "el.dispatchEvent(new InputEvent" in _PROMPT_INPUT_SCRIPT
    assert "insertContentEditablePrompt(el, prompt)" in _PROMPT_INPUT_SCRIPT
    assert "textContent = prompt" not in _PROMPT_INPUT_SCRIPT
    assert "inputMatchesPrompt" in _PROMPT_INPUT_SCRIPT
    assert "const actionRoot = el.closest('form')" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "actionRoot.querySelectorAll" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "selectedSend.button.click()" not in _SEND_ACTION_DISPATCH_SCRIPT
    assert "data-shirabe-send-action" in _SEND_ACTION_DISPATCH_SCRIPT


def test_prompt_submit_scripts_use_logical_composer_and_send_action_gates() -> None:
    assert "composer-logical-candidate-count" in _PROMPT_SUBMIT_SCRIPT
    assert "logicalCandidateCount" in _PROMPT_SUBMIT_SCRIPT
    assert "activeCandidateCount" in _PROMPT_SUBMIT_SCRIPT
    assert "selectionReason" in _PROMPT_SUBMIT_SCRIPT
    assert "if (unique.length !== 1)" not in _PROMPT_SUBMIT_SCRIPT
    assert "document.querySelector('button[data-testid=\"send-button\"]')" not in _SEND_ACTION_DISPATCH_SCRIPT
    assert "send-logical-action-count" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "send-action-not-ready" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "retryable: logicalSendCount === 0" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "sendLogicalActionCount" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "sendSelectionReason" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "logical-send-dedup" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "multiple-logical-send-actions" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "d.semantic.kind === 'send'" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "kind = 'stop'" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "kind = 'voice'" in _SEND_ACTION_DISPATCH_SCRIPT
    assert "kind = 'attach'" in _SEND_ACTION_DISPATCH_SCRIPT


def test_submit_prompt_records_send_action_identity_evidence_after_logical_dedup() -> None:
    prompt = "send action smoke"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[
            composer(count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
            composer(empty=True, count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
        ],
        message_observations=[
            message_rows(url=chat.url),
            message_rows(("user", prompt, "u-1"), url=chat.url),
        ],
        submit_actions=[{
            "ok": True,
            "reason": "action-dispatched",
            "candidateCount": 2,
            "activeCandidateCount": 1,
            "logicalCandidateCount": 1,
            "selectionReason": "single-active-candidate",
            "sendRawCandidateCount": 4,
            "sendActiveCandidateCount": 2,
            "sendLogicalActionCount": 1,
            "sendSelectionReason": "logical-send-dedup",
            "sendActionKinds": ["send", "send", "voice", "attach"],
        }],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert result.ok
    assert result.evidence is not None
    assert "action.send.raw_candidate_count=4" in result.evidence.observations
    assert "action.send.active_candidate_count=2" in result.evidence.observations
    assert "action.send.logical_action_count=1" in result.evidence.observations
    assert "action.send.selection_reason=logical-send-dedup" in result.evidence.observations
    assert "action.send.candidate_kinds=send,send,voice,attach" in result.evidence.observations


def test_submit_prompt_waits_for_send_action_to_appear_after_prompt_input() -> None:
    prompt = "react render wait"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[
            composer(count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
            composer(empty=True, count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
        ],
        message_observations=[
            message_rows(url=chat.url),
            message_rows(("user", prompt, "u-1"), url=chat.url),
        ],
        submit_actions=[
            {
                "ok": True,
                "reason": "prompt-input-established",
                "candidateCount": 2,
                "activeCandidateCount": 1,
                "logicalCandidateCount": 1,
                "selectionReason": "single-active-candidate",
                "inputTextLength": len(prompt),
                "inputMatchesPrompt": True,
            },
            {
                "ok": False,
                "reason": "send-action-not-ready",
                "retryable": True,
                "candidateCount": 2,
                "activeCandidateCount": 1,
                "logicalCandidateCount": 1,
                "selectionReason": "single-active-candidate",
                "inputTextLength": len(prompt),
                "inputMatchesPrompt": True,
                "sendRawCandidateCount": 4,
                "sendActiveCandidateCount": 0,
                "sendLogicalActionCount": 0,
                "sendSelectionReason": "no-enabled-send-action",
                "sendActionKinds": ["attach", "other", "voice", "other"],
            },
            {
                "ok": True,
                "reason": "action-dispatched",
                "retryable": False,
                "candidateCount": 2,
                "activeCandidateCount": 1,
                "logicalCandidateCount": 1,
                "selectionReason": "single-active-candidate",
                "inputTextLength": len(prompt),
                "inputMatchesPrompt": True,
                "sendRawCandidateCount": 4,
                "sendActiveCandidateCount": 1,
                "sendLogicalActionCount": 1,
                "sendSelectionReason": "single-enabled-send-action",
                "sendActionKinds": ["attach", "other", "send", "other"],
            },
        ],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0.05,
        poll_interval_seconds=0.001,
    )
    assert result.ok
    assert result.evidence is not None
    assert "action.input.ok=True" in result.evidence.observations
    assert "action.input.prompt_correlated=True" in result.evidence.observations
    assert "action.send.wait_attempts=2" in result.evidence.observations
    assert "action.send.logical_action_count=1" in result.evidence.observations
    assert "action.send.selection_reason=single-enabled-send-action" in result.evidence.observations



def _exact_prompt_input_action(prompt: str) -> dict[str, Any]:
    return {
        "ok": True,
        "reason": "prompt-input-established",
        "candidateCount": 2,
        "activeCandidateCount": 1,
        "logicalCandidateCount": 1,
        "selectionReason": "single-active-candidate",
        "inputTextLength": len(prompt),
        "inputRawTextLength": len(prompt),
        "inputMatchesPrompt": True,
        "inputNormalizationVersion": "prompt-text-v3",
        "inputSerializationVersion": "prompt-dom-v1",
        "inputTextSource": "dom-serialized-contenteditable",
        "inputExpectedFingerprint": "cad98de9",
        "inputObservedFingerprint": "cad98de9",
        "inputFirstDifferenceIndex": -1,
    }


def _exact_send_dispatch_action(prompt: str) -> dict[str, Any]:
    return {
        "ok": True,
        "reason": "action-dispatched",
        "retryable": False,
        "candidateCount": 2,
        "activeCandidateCount": 1,
        "logicalCandidateCount": 1,
        "selectionReason": "single-active-candidate",
        "inputTextLength": len(prompt),
        "inputRawTextLength": len(prompt),
        "inputMatchesPrompt": True,
        "inputNormalizationVersion": "prompt-text-v3",
        "inputSerializationVersion": "prompt-dom-v1",
        "inputTextSource": "dom-serialized-contenteditable",
        "inputExpectedFingerprint": "cad98de9",
        "inputObservedFingerprint": "cad98de9",
        "inputFirstDifferenceIndex": -1,
        "sendRawCandidateCount": 5,
        "sendActiveCandidateCount": 1,
        "sendLogicalActionCount": 1,
        "sendSelectionReason": "single-enabled-send-action",
        "sendActionKinds": ["send", "other", "attach", "other", "other"],
    }


def test_submit_prompt_uses_causal_pre_dispatch_correlation_when_rendered_user_text_is_lossy() -> None:
    prompt = "line one\nline two"
    rendered = "line one line two"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[
            composer(empty=True, count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
            composer(empty=True, count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
        ],
        message_observations=[
            message_rows(url=chat.url),
            message_rows(("user", rendered, "u-1"), url=chat.url),
        ],
        submit_actions=[_exact_prompt_input_action(prompt), _exact_send_dispatch_action(prompt)],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert result.ok
    assert result.user_message is not None
    assert result.user_message.text == rendered
    assert result.evidence is not None
    observations = result.evidence.observations
    assert "postcondition.dispatch_prompt_correlated=True" in observations
    assert "postcondition.dispatch_prompt_fingerprint_match=True" in observations
    assert "postcondition.user_advanced=True" in observations
    assert "postcondition.rendered_user_matches_prompt=False" in observations
    assert "postcondition.composer_cleared=True" in observations
    assert "postcondition=causal-prompt-submission-established" in observations


def test_submit_prompt_causal_gate_still_requires_composer_clear() -> None:
    prompt = "composer must clear"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[
            composer(empty=True),
            composer(empty=False),
        ],
        message_observations=[
            message_rows(url=chat.url),
            message_rows(("user", prompt, "u-1"), url=chat.url),
        ],
        submit_actions=[_exact_prompt_input_action(prompt), _exact_send_dispatch_action(prompt)],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.TIMEOUT
    assert result.evidence is not None
    observations = result.evidence.observations
    assert "postcondition.user_advanced=True" in observations
    assert "postcondition.rendered_user_matches_prompt=True" in observations
    assert "postcondition.composer.empty=False" in observations
    assert "postcondition.composer_cleared=False" in observations
    assert "postcondition.causal_submission_established=False" in observations
    assert result.evidence.details["activation_attempted"] is True
    assert result.evidence.details["trusted_click"] is True
    assert result.evidence.details["web_submit_established"] is False
    assert result.evidence.details["baseline_user_count"] == 0
    assert result.evidence.details["final_user_count"] == 1
    assert result.evidence.details["composer_cleared"] is False


def test_submit_prompt_trusted_click_failure_never_claims_web_submit(evidence_case) -> None:
    prompt = "trusted click must establish"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[composer(empty=True)],
        message_observations=[message_rows(url=chat.url)],
        submit_actions=[_exact_prompt_input_action(prompt), _exact_send_dispatch_action(prompt)],
        trusted_click_actions=[{
            "ok": False, "reason": "trusted-click-failed",
            "activationAttempted": True, "trustedClick": False,
            "locatorCount": 1, "trustedClickEventCount": 0,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = submit_prompt(session, chat, prompt, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.RECONCILE_REQUIRED
    assert result.evidence is not None
    assert result.evidence.details["activation_attempted"] is True
    assert result.evidence.details["trusted_click"] is False
    assert result.evidence.details["web_submit_established"] is False
    assert result.evidence.details["send_state"] == "UNKNOWN"
    assert "action.send.trusted_click=False" in result.evidence.observations
    assert session.fixture_authority.current.mode == "RECONCILIATION_REQUIRED"
    clicks = [row for row in session.fixture_transport.primitive_commands if row["method"] == "Input.trustedClick"]
    assert len(clicks) == 1
    evidence_case(expected={"failure": "RECONCILE_REQUIRED", "send_state": "UNKNOWN", "activation_attempted": True,
                            "web_submit_established": False, "activation_calls": 1},
                  actual={"failure": result.failure.name, "send_state": result.evidence.details["send_state"],
                          "activation_attempted": result.evidence.details["activation_attempted"],
                          "web_submit_established": result.evidence.details["web_submit_established"], "activation_calls": len(clicks)},
                  commands=session.fixture_transport.audit, ac=["AC01", "AC02", "AC04"], tgt=["TGT-27"], bind=["BIND-03"])


def test_submit_prompt_causal_gate_still_requires_new_user_message() -> None:
    prompt = "user message must advance"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[
            composer(empty=True),
            composer(empty=True),
        ],
        message_observations=[
            message_rows(url=chat.url),
            message_rows(url=chat.url),
        ],
        submit_actions=[_exact_prompt_input_action(prompt), _exact_send_dispatch_action(prompt)],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.TIMEOUT
    assert result.evidence is not None
    observations = result.evidence.observations
    assert "postcondition.user_advanced=False" in observations
    assert "postcondition.latest_user_present=False" in observations
    assert "postcondition.composer_cleared=True" in observations
    assert "postcondition.causal_submission_established=False" in observations


def test_submit_prompt_fails_if_dispatched_action_lacks_exact_prompt_correlation_evidence(evidence_case) -> None:
    prompt = "correlation required"
    chat = durable_chat()
    bad_send = _exact_send_dispatch_action(prompt)
    bad_send["inputMatchesPrompt"] = False
    bad_send["inputObservedFingerprint"] = "deadbeef"
    page = FakePage(
        chat.url,
        composer_observations=[composer(empty=True)],
        message_observations=[message_rows(url=chat.url)],
        submit_actions=[_exact_prompt_input_action(prompt), bad_send],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = submit_prompt(
        session,
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.RECONCILE_REQUIRED
    assert result.evidence is not None
    assert "postcondition.dispatch_prompt_correlated=False" in result.evidence.observations
    assert "submission-uncertain=dispatch-prompt-correlation-not-established" in result.evidence.observations
    assert result.evidence.details["send_state"] == "UNKNOWN"
    assert result.evidence.details["web_submit_established"] is False
    assert session.fixture_authority.current.mode == "RECONCILIATION_REQUIRED"
    clicks = [row for row in session.fixture_transport.primitive_commands if row["method"] == "Input.trustedClick"]
    assert len(clicks) == 1
    evidence_case(expected={"failure": "RECONCILE_REQUIRED", "send_state": "UNKNOWN", "prompt_correlation": False,
                            "web_submit_established": False, "activation_calls": 1},
                  actual={"failure": result.failure.name, "send_state": result.evidence.details["send_state"],
                          "prompt_correlation": False, "web_submit_established": result.evidence.details["web_submit_established"],
                          "activation_calls": len(clicks)},
                  commands=session.fixture_transport.audit, ac=["AC01", "AC02", "AC04"], tgt=["TGT-27"], bind=["BIND-03"])


def test_submit_prompt_times_out_when_send_action_never_becomes_ready() -> None:
    prompt = "never ready"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[composer()],
        message_observations=[message_rows(url=chat.url)],
        submit_actions=[
            {
                "ok": True,
                "reason": "prompt-input-established",
                "logicalCandidateCount": 1,
                "inputTextLength": len(prompt),
                "inputMatchesPrompt": True,
            },
            {
                "ok": False,
                "reason": "send-action-not-ready",
                "retryable": True,
                "logicalCandidateCount": 1,
                "inputTextLength": len(prompt),
                "inputMatchesPrompt": True,
                "sendRawCandidateCount": 4,
                "sendActiveCandidateCount": 0,
                "sendLogicalActionCount": 0,
                "sendSelectionReason": "no-enabled-send-action",
                "sendActionKinds": ["attach", "other", "voice", "other"],
            },
        ],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.TIMEOUT
    assert result.evidence is not None
    assert "action.send.wait_attempts=1" in result.evidence.observations
    assert "action.send.logical_action_count=0" in result.evidence.observations
    assert "action.send.selection_reason=no-enabled-send-action" in result.evidence.observations


def test_submit_prompt_fails_closed_for_two_independent_logical_send_actions() -> None:
    prompt = "ambiguous send"
    chat = durable_chat()
    page = FakePage(
        chat.url,
        composer_observations=[
            composer(count=2, active_count=1, logical_count=1, selection_reason="single-active-candidate"),
        ],
        message_observations=[message_rows(url=chat.url)],
        submit_actions=[{
            "ok": True,
            "reason": "prompt-input-established",
            "candidateCount": 2,
            "activeCandidateCount": 1,
            "logicalCandidateCount": 1,
            "selectionReason": "single-active-candidate",
            "inputTextLength": len(prompt),
            "inputMatchesPrompt": True,
        }, {
            "ok": False,
            "reason": "send-logical-action-count",
            "candidateCount": 2,
            "activeCandidateCount": 1,
            "logicalCandidateCount": 1,
            "selectionReason": "single-active-candidate",
            "sendRawCandidateCount": 3,
            "sendActiveCandidateCount": 2,
            "sendLogicalActionCount": 2,
            "sendSelectionReason": "multiple-logical-send-actions",
            "sendActionKinds": ["send", "send", "voice"],
        }],
    )
    result = submit_prompt(
        make_bound_session(transport=FakeTransport([page])),
        chat,
        prompt,
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.AMBIGUOUS_OBSERVATION
    assert result.evidence is not None
    assert "action.send.logical_action_count=2" in result.evidence.observations
    assert "action.send.selection_reason=multiple-logical-send-actions" in result.evidence.observations


def test_start_new_chat_navigates_to_project_root_and_requires_all_postconditions() -> None:
    p = project()
    root = "https://chatgpt.com/g/g-p-abc123-spk-shirabe"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(root, p.project_key)],
        composer_observations=[composer(empty=True)],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(
        session,
        p,
        client_ref="execution",
        timeout_seconds=0,
        provisional_id_factory=lambda: "provisional-execution",
    )
    assert result.ok
    assert page.url == root
    assert result.chat is not None
    assert result.chat.project_key == p.project_key
    assert result.chat.conversation_id is None
    assert result.chat.provisional_id == "provisional-execution"
    assert result.chat.client_ref == "execution"
    assert result.evidence is not None
    assert "postcondition=zero-rendered-messages" in result.evidence.observations
    assert "postcondition=empty-composer-input-ready" in result.evidence.observations


def test_start_new_chat_does_not_claim_ready_if_project_identity_mismatches() -> None:
    p = project()
    root = "https://chatgpt.com/g/g-p-wrong-spk-shirabe"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(root, "g-p-wrong")],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH


def test_start_new_chat_fails_if_existing_conversation_is_still_bound() -> None:
    p = project()
    existing = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe/c/existing"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(existing, p.project_key, conversation_id="existing", messages=2)],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.POSTCONDITION_NOT_ESTABLISHED


def test_start_new_chat_fails_if_composer_is_not_empty() -> None:
    p = project()
    root = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(root, p.project_key)],
        composer_observations=[composer(empty=False)],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.POSTCONDITION_NOT_ESTABLISHED


def test_start_new_chat_fails_if_rendered_messages_are_not_zero() -> None:
    p = project()
    root = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(root, p.project_key, messages=1)],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok



def test_start_new_chat_accepts_project_suffix_route_with_semantic_postconditions() -> None:
    p = project()
    project_route = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe/project"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(project_route, p.project_key)],
        # Real-Web regression evidence observed two raw candidates while the
        # selected composer surface itself was visible/editable/empty.
        composer_observations=[
            composer(
                empty=True,
                editable=True,
                count=2,
                active_count=2,
                logical_count=1,
                selection_reason="logical-dedup",
            )
        ],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(
        session,
        p,
        timeout_seconds=0,
        provisional_id_factory=lambda: "p-project-route",
    )
    assert result.ok
    assert result.chat is not None
    assert result.chat.url == project_route
    assert result.chat.conversation_id is None
    assert result.evidence is not None
    assert "composer.candidate_count=2" in result.evidence.observations
    assert "postcondition=new-chat-route-established" in result.evidence.observations


def test_start_new_chat_accepts_project_suffix_route_with_query_and_fragment() -> None:
    p = project()
    project_route = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe/project?model=auto#composer"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(project_route, p.project_key)],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert result.ok


def test_start_new_chat_rejects_existing_conversation_even_under_project_suffix_shape() -> None:
    p = project()
    conversation_route = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe/c/conv-123"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(conversation_route, p.project_key, conversation_id="conv-123")],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.POSTCONDITION_NOT_ESTABLISHED


def test_start_new_chat_rejects_project_suffix_when_messages_exist() -> None:
    p = project()
    project_route = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe/project"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(project_route, p.project_key, messages=1)],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok


def test_start_new_chat_rejects_project_suffix_when_composer_not_ready_or_empty() -> None:
    p = project()
    project_route = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe/project"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(project_route, p.project_key)],
        composer_observations=[composer(empty=False)],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok


def test_start_new_chat_rejects_project_suffix_on_project_key_mismatch() -> None:
    p = project()
    wrong_route = "https://chatgpt.com/g/g-p-wrong-spk-shirabe/project"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(wrong_route, "g-p-wrong")],
        composer_observations=[composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = start_new_chat(session, p, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH

def test_caller_can_hold_separate_provisional_chat_identities_without_role_semantics_in_api() -> None:
    p = project()
    root = f"https://chatgpt.com/g/{p.project_key}-spk-shirabe"
    page = FakePage(
        p.url,
        chat_observations=[chat_obs(root, p.project_key), chat_obs(root, p.project_key)],
        composer_observations=[composer(), composer()],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    first = start_new_chat(
        session,
        p,
        client_ref="execution-chat",
        timeout_seconds=0,
        provisional_id_factory=lambda: "p-exec",
    )
    second = start_new_chat(
        session,
        p,
        client_ref="assessment-chat",
        timeout_seconds=0,
        provisional_id_factory=lambda: "p-assess",
    )
    assert first.ok and second.ok
    assert first.chat is not None and second.chat is not None
    assert first.chat.provisional_id != second.chat.provisional_id
    assert first.chat.client_ref != second.chat.client_ref


def test_bind_chat_rejects_provisional_identity_instead_of_guessing_existing_conversation() -> None:
    provisional = ChatIdentity(
        project_key="g-p-abc123",
        url="https://chatgpt.com/g/g-p-abc123-spk-shirabe",
        provisional_id="p1",
    )
    session = make_bound_session(transport=FakeTransport([]))
    result = bind_chat(session, provisional)
    assert not result.ok
    assert result.failure == FailureKind.RECONCILE_REQUIRED


def test_bind_chat_verifies_exact_project_and_conversation_identity() -> None:
    key = "g-p-abc123"
    conv = "conv-123"
    url = f"https://chatgpt.com/g/{key}-spk-shirabe/c/{conv}"
    identity = ChatIdentity(project_key=key, url=url, conversation_id=conv, client_ref="execution")
    page = FakePage(
        "https://chatgpt.com/",
        chat_observations=[chat_obs(url, key, conversation_id=conv, messages=2)],
        composer_observations=[composer(empty=True)],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = bind_chat(session, identity, timeout_seconds=0)
    assert result.ok
    assert result.chat is not None and result.chat.conversation_id == conv


def test_bind_chat_fails_closed_on_wrong_conversation_after_navigation() -> None:
    key = "g-p-abc123"
    expected = "expected"
    url = f"https://chatgpt.com/g/{key}-spk-shirabe/c/{expected}"
    identity = ChatIdentity(project_key=key, url=url, conversation_id=expected)
    observed_url = f"https://chatgpt.com/g/{key}-spk-shirabe/c/wrong"
    page = FakePage(
        "https://chatgpt.com/",
        chat_observations=[chat_obs(observed_url, key, conversation_id="wrong", messages=2)],
        composer_observations=[composer(empty=True)],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = bind_chat(session, identity, timeout_seconds=0)
    assert not result.ok
    assert result.failure == FailureKind.TARGET_IDENTITY_MISMATCH


def test_web002_modules_do_not_import_shirabe_runtime() -> None:
    from pathlib import Path

    root = (Path(__file__).resolve().parents[1] / "src" / "chatgpt_web_api")
    for name in ("models.py", "chats.py", "composer.py"):
        assert "shirabe_runtime" not in (root / name).read_text(encoding="utf-8")


def test_attach_files_verifies_sha_uploads_and_observes_filename_before_ready(tmp_path) -> None:
    import hashlib

    context = tmp_path / "context.md"
    context.write_text("# context\nliteral source\n", encoding="utf-8")
    raw = context.read_bytes()
    page = FakePage(
        durable_chat().url,
        composer_observations=[composer(empty=True)],
        attachment_observations=[{
            "ok": True,
            "reason": "all-attachment-filenames-observed",
            "observed": [context.name],
            "expectedCount": 1,
            "observedCount": 1,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = attach_files(
        session,
        durable_chat(),
        ({
            "path": str(context),
            "filename": context.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "text/markdown",
            "size_bytes": len(raw),
        },),
        timeout_seconds=0,
    )
    assert result.ok
    assert result.filenames == (context.name,)
    assert page.uploaded_files == [(str(context.resolve()),)]
    assert result.evidence is not None
    assert "attachment_postcondition=verified" in result.evidence.observations


def test_attach_files_rejects_sha_mismatch_before_browser_upload(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("data\n", encoding="utf-8")
    page = FakePage(durable_chat().url)
    session = make_bound_session(transport=FakeTransport([page]))
    result = attach_files(
        session,
        durable_chat(),
        ({
            "path": str(context),
            "filename": context.name,
            "sha256": "0" * 64,
            "media_type": "text/markdown",
            "size_bytes": len(context.read_bytes()),
        },),
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.POSTCONDITION_NOT_ESTABLISHED
    assert page.uploaded_files == []


class _FakeElementHandle:
    def __init__(self, accept: str = ".md") -> None:
        self._accept = accept

    def get_attribute(self, name: str):
        return self._accept if name == "accept" else None


class _FakeFileChooser:
    def __init__(self, *, multiple: bool = False, accept: str = ".md") -> None:
        self._multiple = multiple
        self.element = _FakeElementHandle(accept)
        self.set_files_calls: list[tuple[str, ...]] = []

    def is_multiple(self) -> bool:
        return self._multiple

    def set_files(self, paths) -> None:
        self.set_files_calls.append(tuple(str(path) for path in paths))


class _FakeChooserInfo:
    def __init__(self, page: "_FakePlaywrightPage", timeout: int) -> None:
        self._page = page
        self._timeout = timeout
        self.value = None

    def __enter__(self):
        self._page.expect_timeouts.append(self._timeout)
        self._page.expect_armed = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self._page.expect_armed = False
        if exc_type is not None:
            return False
        chooser = self._page.pending_chooser
        self._page.pending_chooser = None
        if chooser is None:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            raise PlaywrightTimeoutError("no file chooser")
        self.value = chooser
        return False


class _FakeLocator:
    def __init__(self, page: "_FakePlaywrightPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    def count(self) -> int:
        return self._page.locator_counts.get(self._selector, 1)

    def click(self) -> None:
        self._page.clicks.append(self._selector)
        behavior = self._page.click_behaviors.pop(0)
        if behavior == "chooser":
            assert self._page.expect_armed, "chooser-producing click must occur inside expect_file_chooser"
            self._page.pending_chooser = self._page.chooser
        elif behavior == "menu":
            self._page.menu_open = True
        elif behavior == "none":
            return
        else:
            raise AssertionError(f"unknown click behavior {behavior}")


class _FakePlaywrightPage:
    def __init__(
        self,
        *,
        attachment_kind: str,
        click_behaviors: list[str],
        attachment_ok: bool = True,
        attachment_reason: str = "single-attachment-action",
        attachment_logical_count: int = 1,
        menu_ok: bool = True,
        menu_reason: str = "single-upload-menu-action",
        menu_logical_count: int = 1,
    ) -> None:
        self.attachment_kind = attachment_kind
        self.click_behaviors = list(click_behaviors)
        self.attachment_ok = attachment_ok
        self.attachment_reason = attachment_reason
        self.attachment_logical_count = attachment_logical_count
        self.menu_ok = menu_ok
        self.menu_reason = menu_reason
        self.menu_logical_count = menu_logical_count
        self.menu_open = False
        self.expect_armed = False
        self.pending_chooser = None
        self.chooser = _FakeFileChooser()
        self.clicks: list[str] = []
        self.expect_timeouts: list[int] = []
        self.locator_counts: dict[str, int] = {}
        self.cleanup_calls: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        return "https://chatgpt.com/"

    def evaluate(self, expression: str, arg=None):
        if "SHIRABE_ATTACHMENT_ACTION_SELECT_V3" in expression:
            token = arg["token"]
            return {
                "ok": self.attachment_ok,
                "reason": self.attachment_reason,
                "actionRootReason": "minimal-semantic-neighborhood" if self.attachment_ok else self.attachment_reason,
                "actionRootCandidateCount": 1,
                "actionRootSelectedDepth": 2 if self.attachment_ok else None,
                "actionRootDetails": [
                    {"depth": 1, "rawInteractiveCount": 0, "activeInteractiveCount": 0, "attachmentCandidateCount": 0, "logicalAttachmentCount": 0},
                    {"depth": 2, "rawInteractiveCount": 4, "activeInteractiveCount": 3, "attachmentCandidateCount": self.attachment_logical_count, "logicalAttachmentCount": self.attachment_logical_count},
                ],
                "rawActionCount": 4,
                "activeActionCount": self.attachment_logical_count,
                "logicalActionCount": self.attachment_logical_count,
                "selectedKind": self.attachment_kind,
                "selectedSignature": "attach|test" if self.attachment_ok else None,
                "popupBaselineSignatures": ["baseline|menu"],
                "popupBaselineCount": 1,
                "triggerAriaControls": "composer-tools-menu" if self.attachment_kind == "menu-opener" else "",
                "triggerAriaOwns": "",
                "triggerAriaExpanded": "false",
                "triggerRect": {"left": 10, "top": 10, "width": 30, "height": 30},
                "causalDeltaToken": token,
                "baselineVisibleInteractiveCount": 12,
            }
        if "SHIRABE_UPLOAD_MENU_ACTION_SELECT_V4" in expression:
            if not self.menu_ok or not self.menu_open:
                return {"ok": False, "reason": self.menu_reason if not self.menu_ok else "upload-menu-action-not-found", "logicalMenuActionCount": self.menu_logical_count if not self.menu_ok else 0}
            return {
                "ok": True,
                "reason": self.menu_reason,
                "popupDiscoveryMode": "causal-interactive-delta",
                "popupBeforeRootCount": 0,
                "popupAfterRootCount": 1,
                "popupNewRootCount": 1,
                "popupRootCandidateCount": 1,
                "popupSelectedSignature": "generic-popup-neighborhood",
                "popupRootDetails": [{"signature":"generic-popup-neighborhood","newlyVisible":True}],
                "causalObservationDurationMs": 120,
                "causalPollCount": 3,
                "mutationAddedCount": 2,
                "mutationAttributeChangeCount": 1,
                "visibleInteractiveBeforeCount": 12,
                "visibleInteractiveAfterCount": 15,
                "newlyVisibleInteractiveCount": 3,
                "newlyVisibleInteractiveDetails": [{"text":"upload files","localUpload":True}],
                "popupNeighborhoodReason": "minimal-causal-interactive-neighborhood",
                "popupNeighborhoodSelectedDepth": 2,
                "popupNeighborhoodSignature": "generic-popup-neighborhood",
                "menuRootCount": 1,
                "rawMenuActionCount": 3,
                "activeMenuActionCount": self.menu_logical_count,
                "logicalMenuActionCount": self.menu_logical_count,
                "selectedSignature": "upload from computer",
                "activationTargetReason": "ranked-causal-row-target",
                "activationTargetCandidateCount": 1,
                "activationTargetSelectedRank": 0,
                "activationTargetSelectedSource": "semantic-self",
                "activationTargetSelectedTag": "button",
                "activationTargetSelectedRole": "menuitem",
                "activationTargetSelectedSignature": "upload from computer",
                "activationTargetDetails": [{"rank":0,"tag":"button","role":"menuitem","source":"semantic-self","signature":"upload from computer"}],
            }
        if "removeAttribute" in expression:
            self.cleanup_calls.append(dict(arg or {}))
            return None
        raise AssertionError(f"unexpected evaluate: {expression[:80]}")

    def locator(self, selector: str):
        return _FakeLocator(self, selector)

    def expect_file_chooser(self, *, timeout: int):
        return _FakeChooserInfo(self, timeout)


class _FakeContext:
    pass


def test_playwright_attachment_transport_binds_direct_filechooser_without_static_file_input(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("context", encoding="utf-8")
    page = _FakePlaywrightPage(attachment_kind="attachment-action", click_behaviors=["chooser"])
    ops = isolated_page_ops(_FakeContext(), page)

    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.upload_files_to_composer(
        [str(context)],
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is True
    assert result["transport"] == "composer-action-filechooser-v6"
    assert result["fileChooserEventObserved"] is True
    assert result["stage"] == "filechooser-set-files"
    assert page.chooser.set_files_calls == [(str(context.resolve()),)]
    assert len(page.clicks) == 1


def test_playwright_attachment_transport_handles_menu_opener_then_upload_filechooser(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("context", encoding="utf-8")
    page = _FakePlaywrightPage(attachment_kind="menu-opener", click_behaviors=["menu", "chooser"])
    ops = isolated_page_ops(_FakeContext(), page)

    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.upload_files_to_composer(
        [str(context)],
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is True
    assert result["menuLogicalActionCount"] == 1
    assert result["fileChooserEventObserved"] is True
    assert len(page.clicks) == 2
    assert page.chooser.set_files_calls == [(str(context.resolve()),)]


def test_playwright_attachment_transport_does_not_use_static_file_input_discovery() -> None:
    import inspect
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    source = inspect.getsource(_PlaywrightPageOps._bind_composer_filechooser) + inspect.getsource(_PlaywrightPageOps.upload_files_to_composer)
    # File-input enumeration is diagnostic-only in Retry 5; upload authority must
    # still never come from static input discovery or locator.set_input_files().
    assert "set_input_files" not in source
    assert "expect_file_chooser" in source
    assert "locator.click" in source
    assert "SHIRABE_UPLOAD_ACTIVATION_DIAGNOSTIC_ARM_V1" in source
    assert "SHIRABE_UPLOAD_ACTIVATION_DIAGNOSTIC_COLLECT_V1" in source
    assert "input[type" not in _ATTACHMENT_ACTION_SELECT_SCRIPT
    assert "input[type" not in _UPLOAD_MENU_ACTION_SELECT_SCRIPT


def test_playwright_attachment_transport_fails_closed_when_menu_upload_action_is_missing(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("context", encoding="utf-8")
    page = _FakePlaywrightPage(attachment_kind="menu-opener", click_behaviors=["menu"], menu_ok=False, menu_reason="upload-menu-action-not-found", menu_logical_count=0)
    ops = isolated_page_ops(_FakeContext(), page)

    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.upload_files_to_composer(
        [str(context)],
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is False
    assert result["stage"] == "upload-menu-action-select"
    assert result["reason"] == "upload-menu-action-not-found"
    assert page.chooser.set_files_calls == []


def test_playwright_attachment_transport_rejects_ambiguous_attachment_action_before_click(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("context", encoding="utf-8")
    page = _FakePlaywrightPage(
        attachment_kind="attachment-action",
        click_behaviors=[],
        attachment_ok=False,
        attachment_reason="attachment-action-ambiguous",
        attachment_logical_count=2,
    )
    ops = isolated_page_ops(_FakeContext(), page)

    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.upload_files_to_composer(
        [str(context)],
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is False
    assert result["stage"] == "attachment-action-select"
    assert result["reason"] == "attachment-action-ambiguous"
    assert result["attachmentLogicalActionCount"] == 2
    assert page.clicks == []
    assert page.chooser.set_files_calls == []


def test_playwright_attachment_transport_rejects_ambiguous_upload_menu_action(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("context", encoding="utf-8")
    page = _FakePlaywrightPage(
        attachment_kind="menu-opener",
        click_behaviors=["menu"],
        menu_ok=False,
        menu_reason="upload-menu-action-ambiguous",
        menu_logical_count=2,
    )
    ops = isolated_page_ops(_FakeContext(), page)

    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.upload_files_to_composer(
        [str(context)],
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is False
    assert result["stage"] == "upload-menu-action-select"
    assert result["reason"] == "upload-menu-action-ambiguous"
    assert result["menuLogicalActionCount"] == 2
    assert len(page.clicks) == 1
    assert page.chooser.set_files_calls == []


def test_playwright_attachment_transport_falls_back_to_menu_when_direct_action_opens_menu(tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("context", encoding="utf-8")
    page = _FakePlaywrightPage(attachment_kind="attachment-action", click_behaviors=["menu", "chooser"])
    ops = isolated_page_ops(_FakeContext(), page)

    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.upload_files_to_composer(
        [str(context)],
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is True
    assert result["attachmentClickReason"] == "filechooser-not-observed"
    assert result["menuLogicalActionCount"] == 1
    assert result["menuClickReason"] == "filechooser-observed"
    assert len(page.clicks) == 2
    assert page.chooser.set_files_calls == [(str(context.resolve()),)]


def test_stage1_attachment_transport_observation_stops_at_filechooser_without_upload_or_prompt() -> None:
    page = FakePage("https://chatgpt.com/")
    session = make_bound_session(transport=FakeTransport([page]))
    assert session.bind_chatgpt_page().ok
    result = observe_attachment_transport(session, timeout_seconds=1)
    assert result.ok
    assert result.stage == "picker-transport-observed"
    assert result.evidence is not None
    assert result.evidence.details["file_assigned"] is False
    assert result.evidence.details["prompt_dispatched"] is False
    assert result.evidence.details["filechooser"]["event_observed"] is True
    assert result.evidence.details["attachment_action"]["candidates"]
    assert page.uploaded_files == []


def test_stage1_accepts_native_show_open_file_picker_as_observed_transport() -> None:
    page = FakePage(
        "https://chatgpt.com/",
        attachment_transport_observations=[{
            "ok": True,
            "reason": "native-show-open-file-picker-observed",
            "transport": "composer-action-filechooser-v6",
            "stage": "native-picker-observed",
            "pickerTransport": "native-show-open-file-picker",
            "nativePickerApiObserved": True,
            "showOpenFilePickerCalls": 1,
            "fileChooserEventObserved": False,
            "fileAssigned": False,
            "promptDispatched": False,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    assert session.bind_chatgpt_page().ok
    result = observe_attachment_transport(session, timeout_seconds=1)
    assert result.ok
    assert result.stage == "picker-transport-observed"
    assert result.evidence is not None
    assert result.evidence.details["picker_transport"]["kind"] == "native-show-open-file-picker"
    assert result.evidence.details["picker_transport"]["native_api_observed"] is True
    assert result.evidence.details["file_assigned"] is False
    assert result.evidence.details["prompt_dispatched"] is False
    assert result.evidence.details["filechooser"]["event_observed"] is False
    assert page.uploaded_files == []


def test_stage1_attachment_transport_observation_preserves_failure_stage_and_candidates() -> None:
    page = FakePage(
        "https://chatgpt.com/",
        attachment_transport_observations=[{
            "ok": False, "reason": "upload-menu-action-not-found", "transport": "composer-action-filechooser-v6",
            "stage": "upload-menu-action-select", "attachmentActionReason": "single-attachment-action",
            "attachmentRawActionCount": 5, "attachmentActiveActionCount": 1, "attachmentLogicalActionCount": 1,
            "attachmentSelectedKind": "menu-opener", "attachmentSelectedSignature": "menu-opener|plus",
            "attachmentActionDetails": [{"kind":"menu-opener","testId":"composer-plus-btn","signature":"menu-opener|plus"}],
            "attachmentClickReason": "menu-opener-clicked", "menuActionReason": "upload-menu-action-not-found",
            "menuRootCount": 1, "menuRawActionCount": 4, "menuActiveActionCount": 0, "menuLogicalActionCount": 0,
            "menuActionDetails": [], "fileChooserEventObserved": False, "fileAssigned": False, "promptDispatched": False,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    assert session.bind_chatgpt_page().ok
    result = observe_attachment_transport(session, timeout_seconds=1)
    assert not result.ok
    assert result.stage == "upload-menu-action-select"
    assert result.evidence is not None
    assert result.evidence.details["attachment_action"]["selected_kind"] == "menu-opener"
    assert result.evidence.details["upload_menu"]["root_count"] == 1
    assert result.evidence.details["prompt_dispatched"] is False


def test_playwright_stage1_observation_uses_same_causal_binding_but_never_sets_files() -> None:
    page = _FakePlaywrightPage(attachment_kind="menu-opener", click_behaviors=["menu", "chooser"])
    ops = isolated_page_ops(_FakeContext(), page)
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    result = ops.observe_composer_attachment_transport(
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )
    assert result["ok"] is True
    assert result["stage"] == "filechooser-observed"
    assert result["fileChooserEventObserved"] is True
    assert result["fileAssigned"] is False
    assert result["promptDispatched"] is False
    assert page.chooser.set_files_calls == []
    assert len(page.clicks) == 2


def test_attachment_selection_scripts_emit_structured_candidate_details() -> None:
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    assert "activeActionDetails" in _ATTACHMENT_ACTION_SELECT_SCRIPT
    assert "activeMenuActionDetails" in _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    assert "aria:d.sem.aria" in _ATTACHMENT_ACTION_SELECT_SCRIPT
    assert "testId:d.sem.testId" in _UPLOAD_MENU_ACTION_SELECT_SCRIPT


def test_attachment_action_selector_uses_bounded_ancestor_semantic_neighborhood() -> None:
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT

    script = _ATTACHMENT_ACTION_SELECT_SCRIPT
    assert "SHIRABE_ATTACHMENT_ACTION_SELECT_V3" in script
    assert "MAX_ANCESTOR_DEPTH=8" in script
    assert "let cursor=composer.parentElement" in script
    assert "cursor=cursor.parentElement" in script
    assert "minimal-semantic-neighborhood" in script
    assert "attachment-semantic-not-found" in script
    assert "composer-action-root-not-established" in script
    assert "composer.closest('form" not in script


def test_stage1_observation_preserves_action_root_neighborhood_evidence() -> None:
    page = FakePage(
        "https://chatgpt.com/g/g-p-test-project/c/test",
        attachment_transport_observations=[{
            "ok": False,
            "reason": "attachment-semantic-not-found",
            "transport": "composer-action-filechooser-v6",
            "stage": "attachment-action-select",
            "actionRootReason": "attachment-semantic-not-found",
            "actionRootCandidateCount": 2,
            "actionRootSelectedDepth": None,
            "actionRootDetails": [
                {"depth":1,"tag":"div","rawInteractiveCount":0,"activeInteractiveCount":0,"attachmentCandidateCount":0,"logicalAttachmentCount":0},
                {"depth":2,"tag":"div","rawInteractiveCount":3,"activeInteractiveCount":2,"attachmentCandidateCount":0,"logicalAttachmentCount":0},
            ],
            "attachmentActionReason": "attachment-semantic-not-found",
            "attachmentRawActionCount": 3,
            "attachmentActiveActionCount": 0,
            "attachmentLogicalActionCount": 0,
            "fileChooserEventObserved": False,
            "fileAssigned": False,
            "promptDispatched": False,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    assert session.bind_chatgpt_page().ok

    result = observe_attachment_transport(session, timeout_seconds=1)

    assert not result.ok
    assert result.stage == "attachment-action-select"
    assert result.evidence is not None
    root = result.evidence.details["action_root"]
    assert root["reason"] == "attachment-semantic-not-found"
    assert root["candidate_count"] == 2
    assert root["selected_depth"] is None
    assert root["ancestors"][0]["activeInteractiveCount"] == 0
    assert root["ancestors"][1]["rawInteractiveCount"] == 3
    assert "action_root_reason=attachment-semantic-not-found" in result.evidence.observations


def test_playwright_binding_propagates_minimal_action_root_depth() -> None:
    page = _FakePlaywrightPage(attachment_kind="attachment-action", click_behaviors=["chooser"])
    ops = isolated_page_ops(_FakeContext(), page)
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    result = ops.observe_composer_attachment_transport(
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is True
    assert result["actionRootReason"] == "minimal-semantic-neighborhood"
    assert result["actionRootSelectedDepth"] == 2
    assert result["actionRootDetails"][0]["rawInteractiveCount"] == 0
    assert result["fileAssigned"] is False
    assert result["promptDispatched"] is False



def test_upload_menu_selector_uses_causal_popup_root_and_action_semantics() -> None:
    from chatgpt_web_api.composer import _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    script = _UPLOAD_MENU_ACTION_SELECT_SCRIPT
    assert "SHIRABE_UPLOAD_MENU_ACTION_SELECT_V4" in script
    assert "causalDeltaToken" in script
    assert "window.__shirabeAttachmentDelta" in script
    assert "newlyVisibleInteractiveCount" in script
    assert "minimal-causal-interactive-neighborhood" in script
    assert "causal-interactive-delta-not-observed" in script
    assert "local-upload-action-not-found" in script
    assert "local-upload-action-ambiguous" in script
    assert '[class*="menu"' not in script
    assert "strongLocal" in script
    assert "verb&&object" in script
    assert "data-radix-collection-item" in script
    assert "activationTargetReason" in script
    assert "ranked-causal-row-target" in script


def test_playwright_binding_propagates_causal_popup_evidence() -> None:
    page = _FakePlaywrightPage(attachment_kind="menu-opener", click_behaviors=["menu", "chooser"])
    ops = isolated_page_ops(_FakeContext(), page)
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    result = ops.observe_composer_attachment_transport(
        attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
        upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
        timeout_ms=1000,
    )

    assert result["ok"] is True
    assert result["transport"] == "composer-action-filechooser-v6"
    assert result["popupDiscoveryMode"] == "causal-interactive-delta"
    assert result["popupBeforeRootCount"] == 0
    assert result["popupAfterRootCount"] == 1
    assert result["popupNewRootCount"] == 1
    assert result["popupRootCandidateCount"] == 1
    assert result["popupSelectedSignature"] == "generic-popup-neighborhood"
    assert result["newlyVisibleInteractiveCount"] == 3
    assert result["popupNeighborhoodReason"] == "minimal-causal-interactive-neighborhood"
    assert result["fileChooserEventObserved"] is True
    assert result["fileAssigned"] is False
    assert result["promptDispatched"] is False


def test_real_playwright_local_fixture_binds_add_menu_to_filechooser_without_sidebar_false_positive() -> None:
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    html = r'''<!doctype html>
    <html><body>
      <aside>
        <button aria-label="pin フォルダ内ファイル確認コード">history pin</button>
        <button aria-label="open conversation options for obsidian ファイル更新方法">history options</button>
      </aside>
      <div class="composer-shell">
        <button id="add" data-testid="composer-plus-btn" aria-label="Add files and more" aria-expanded="false">+</button>
        <div class="editor-wrapper"><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
      </div>
      <div id="composer-tools-menu" style="display:none">
        <button role="menuitem" id="upload-local">Upload files</button>
        <button role="menuitem">Add from library</button>
      </div>
      <input id="local-file" type="file" style="display:none" />
      <script>
        document.getElementById('add').addEventListener('click', () => {
          setTimeout(() => {
            const menu=document.getElementById('composer-tools-menu');
            menu.style.display='block';
            document.getElementById('add').setAttribute('aria-expanded','true');
          }, 120);
        });
        document.getElementById('upload-local').addEventListener('click', () => document.getElementById('local-file').click());
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            result = ops.observe_composer_attachment_transport(
                attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
                upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
                timeout_ms=1500,
            )
            assert result["ok"] is True
            assert result["transport"] == "composer-action-filechooser-v6"
            assert result["attachmentSelectedKind"] == "menu-opener"
            assert result["popupDiscoveryMode"] == "causal-interactive-delta"
            assert result["newlyVisibleInteractiveCount"] >= 2
            assert result["causalPollCount"] >= 2
            assert result["mutationAttributeChangeCount"] >= 1
            assert result["popupNeighborhoodReason"] == "minimal-causal-interactive-neighborhood"
            assert result["menuLogicalActionCount"] == 1
            assert "upload files" in str(result["menuSelectedSignature"])
            assert result["fileChooserEventObserved"] is True
            assert result["fileAssigned"] is False
            assert result["promptDispatched"] is False
        finally:
            browser.close()


def test_real_playwright_resolves_non_actionable_semantic_copy_to_radix_activation_row() -> None:
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    html = r'''<!doctype html>
    <html><body>
      <aside>
        <button aria-label="pin フォルダ内ファイル確認コード">history pin</button>
      </aside>
      <div class="composer-shell">
        <button id="add" data-testid="composer-plus-btn" aria-label="Add files and more">+</button>
        <div><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
      </div>
      <div id="tools" style="display:none">
        <div id="upload-row" data-radix-collection-item tabindex="-1">
          <div id="upload-copy" tabindex="0">Add photos &amp; files<span>Upload from computer</span></div>
        </div>
        <div tabindex="0">Add from library<span>Browse and search your files</span></div>
      </div>
      <input id="local-file" type="file" style="display:none" />
      <script>
        document.getElementById('add').addEventListener('click', () => {
          setTimeout(() => { document.getElementById('tools').style.display='block'; }, 80);
        });
        document.getElementById('upload-row').addEventListener('click', () => document.getElementById('local-file').click());
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            result = ops.observe_composer_attachment_transport(
                attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
                upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
                timeout_ms=1500,
            )
            assert result["ok"] is True
            assert result["transport"] == "composer-action-filechooser-v6"
            assert result["menuLogicalActionCount"] == 1
            assert result["uploadActivationTargetReason"] == "ranked-causal-row-target"
            assert result["uploadActivationTargetSelectedSource"] == "ancestor"
            assert result["uploadActivationTargetSelectedTag"] == "div"
            assert result["uploadActivationTargetSelectedRank"] == 1
            assert result["fileChooserEventObserved"] is True
            assert result["fileAssigned"] is False
            assert result["promptDispatched"] is False
        finally:
            browser.close()


def test_real_playwright_no_picker_captures_activation_click_and_picker_invocation_diagnostics() -> None:
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    html = r'''<!doctype html>
    <html><body>
      <div class="composer-shell">
        <button id="add" data-testid="composer-plus-btn" aria-label="Add files and more">+</button>
        <div><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
      </div>
      <div id="tools" style="display:none">
        <div id="upload-copy" tabindex="0" style="cursor:pointer">Add photos &amp; files<span>Upload from computer</span></div>
        <div tabindex="0">Add from library<span>Browse and search your files</span></div>
      </div>
      <input id="local-file" type="file" style="display:none" />
      <script>
        document.getElementById('add').addEventListener('click', () => {
          setTimeout(() => { document.getElementById('tools').style.display='block'; }, 40);
        });
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            result = ops.observe_composer_attachment_transport(
                attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
                upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
                timeout_ms=350,
            )
            assert result["ok"] is False
            assert result["transport"] == "composer-action-filechooser-v6"
            assert result["stage"] == "filechooser"
            assert result["activationFailureMode"] == "trusted-click-delivered-no-picker-invocation"
            assert result["activationTrustedClickEventCount"] >= 1
            assert result["fileInputClickMethodCalls"] == 0
            assert result["fileInputShowPickerCalls"] == 0
            assert result["showOpenFilePickerCalls"] == 0
            assert result["fileInputBeforeCount"] == 1
            assert result["fileInputAfterCount"] == 1
            assert result["newFileInputCount"] == 0
            assert result["activationTargetVisibleAfter"] is True
            assert result["activationEventTrace"]
        finally:
            browser.close()


def test_real_playwright_file_input_click_success_records_activation_picker_diagnostics() -> None:
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    html = r'''<!doctype html>
    <html><body>
      <div class="composer-shell">
        <button id="add" data-testid="composer-plus-btn" aria-label="Add files and more">+</button>
        <div><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
      </div>
      <div id="tools" style="display:none">
        <div id="upload-copy" tabindex="0" style="cursor:pointer">Add photos &amp; files<span>Upload from computer</span></div>
        <div tabindex="0">Add from library<span>Browse and search your files</span></div>
      </div>
      <input id="local-file" type="file" style="display:none" accept=".md" />
      <script>
        document.getElementById('add').addEventListener('click', () => {
          setTimeout(() => { document.getElementById('tools').style.display='block'; }, 40);
        });
        document.getElementById('upload-copy').addEventListener('click', () => document.getElementById('local-file').click());
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            result = ops.observe_composer_attachment_transport(
                attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
                upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
                timeout_ms=800,
            )
            assert result["ok"] is True
            assert result["transport"] == "composer-action-filechooser-v6"
            assert result["fileChooserEventObserved"] is True
            assert result["activationTrustedClickEventCount"] >= 1
            assert result["fileInputClickMethodCalls"] >= 1
            assert result["fileInputBeforeCount"] == 1
            assert result["fileInputAfterCount"] == 1
        finally:
            browser.close()


def test_stage1_observation_preserves_causal_interactive_delta_evidence() -> None:
    page = FakePage(
        "https://chatgpt.com/g/g-p-test-project/c/test",
        attachment_transport_observations=[{
            "ok": False,
            "reason": "local-upload-action-not-found",
            "transport": "composer-action-filechooser-v6",
            "stage": "upload-menu-action-select",
            "actionRootReason": "minimal-semantic-neighborhood",
            "actionRootCandidateCount": 1,
            "actionRootSelectedDepth": 3,
            "actionRootDetails": [],
            "attachmentActionReason": "single-attachment-action",
            "attachmentRawActionCount": 5,
            "attachmentActiveActionCount": 1,
            "attachmentLogicalActionCount": 1,
            "attachmentSelectedKind": "menu-opener",
            "attachmentSelectedSignature": "menu-opener|composer-plus-btn|add files and more||",
            "attachmentTriggerAriaControls": "",
            "attachmentTriggerAriaOwns": "",
            "attachmentTriggerAriaExpanded": "",
            "attachmentTriggerRect": {"left":10,"top":10,"width":30,"height":30},
            "attachmentClickReason": "menu-opener-clicked",
            "menuActionReason": "local-upload-action-not-found",
            "popupDiscoveryMode": "causal-interactive-delta",
            "popupBeforeRootCount": 0,
            "popupAfterRootCount": 1,
            "popupNewRootCount": 1,
            "popupRootCandidateCount": 1,
            "popupSelectedSignature": "generic-popup-neighborhood",
            "popupRootDetails": [{"signature":"generic-popup-neighborhood","newlyVisible":True}],
            "causalObservationDurationMs": 420,
            "causalPollCount": 9,
            "mutationAddedCount": 3,
            "mutationAttributeChangeCount": 4,
            "mutationSamples": [{"type":"attributes","attributeName":"style","tag":"div"}],
            "visibleInteractiveBeforeCount": 25,
            "visibleInteractiveAfterCount": 29,
            "newlyVisibleInteractiveCount": 4,
            "newlyVisibleInteractiveDetails": [{"text":"create image","localUpload":False}],
            "popupNeighborhoodReason": "delta-common-neighborhood",
            "popupNeighborhoodSelectedDepth": 2,
            "popupNeighborhoodSignature": "generic-popup-neighborhood",
            "menuRootCount": 1,
            "menuRawActionCount": 4,
            "menuActiveActionCount": 0,
            "menuLogicalActionCount": 0,
            "menuActionDetails": [],
            "fileChooserEventObserved": False,
            "fileAssigned": False,
            "promptDispatched": False,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    assert session.bind_chatgpt_page().ok
    result = observe_attachment_transport(session, timeout_seconds=1)
    assert not result.ok
    assert result.evidence is not None
    popup = result.evidence.details["popup_root"]
    assert popup["discovery_mode"] == "causal-interactive-delta"
    assert popup["candidate_count"] == 1
    delta = result.evidence.details["causal_delta"]
    assert delta["observation_duration_ms"] == 420
    assert delta["poll_count"] == 9
    assert delta["mutation_added_count"] == 3
    assert delta["mutation_attribute_change_count"] == 4
    assert delta["newly_visible_interactive_count"] == 4
    assert delta["popup_neighborhood_reason"] == "delta-common-neighborhood"
    assert "newly_visible_interactive_count=4" in result.evidence.observations
    assert result.evidence.details["attachment_action"]["trigger_aria_controls"] == ""



def test_stage1_observation_preserves_upload_activation_target_evidence() -> None:
    page = FakePage(
        "https://chatgpt.com/g/g-p-test-project/c/test",
        attachment_transport_observations=[{
            "ok": False,
            "reason": "filechooser-not-observed",
            "transport": "composer-action-filechooser-v6",
            "stage": "filechooser",
            "actionRootReason": "minimal-semantic-neighborhood",
            "actionRootCandidateCount": 1,
            "actionRootSelectedDepth": 3,
            "attachmentActionReason": "single-attachment-action",
            "attachmentLogicalActionCount": 1,
            "attachmentSelectedKind": "menu-opener",
            "attachmentSelectedSignature": "menu-opener|composer-plus-btn|add files and more||",
            "attachmentClickReason": "menu-opener-clicked",
            "menuActionReason": "single-local-upload-action",
            "menuRootCount": 1,
            "menuRawActionCount": 16,
            "menuActiveActionCount": 1,
            "menuLogicalActionCount": 1,
            "menuSelectedSignature": "|||add photos & filesupload from computer",
            "uploadActivationTargetReason": "ranked-causal-row-target",
            "uploadActivationTargetCandidateCount": 3,
            "uploadActivationTargetSelectedRank": 1,
            "uploadActivationTargetSelectedSource": "ancestor",
            "uploadActivationTargetSelectedTag": "div",
            "uploadActivationTargetSelectedRole": "",
            "uploadActivationTargetSelectedSignature": "radix-row-signature",
            "uploadActivationTargetDetails": [{"rank": 1, "source": "ancestor", "tag": "div", "dataRadixCollectionItem": True, "signature": "radix-row-signature"}],
            "activationDiagnosticArmOk": True,
            "activationDiagnosticCollectOk": True,
            "activationFailureMode": "trusted-click-delivered-no-picker-invocation",
            "activationDiagnosticDurationMs": 5031,
            "activationTargetOuterHtml": "<div tabindex=\"0\">Add photos &amp; files</div>",
            "activationTargetCenterHitSignature": "div||||add photos & files",
            "activationTargetConnectedAfter": True,
            "activationTargetVisibleAfter": True,
            "activationTargetSignatureAfter": "div||||add photos & files",
            "activationCenterHitSignatureAfter": "span||||upload from computer",
            "activationActiveElementSignature": "div||||add photos & files",
            "activationTrustedClickEventCount": 1,
            "fileInputClickEventCount": 0,
            "fileInputClickMethodCalls": 0,
            "fileInputShowPickerCalls": 0,
            "showOpenFilePickerCalls": 0,
            "fileInputBeforeCount": 1,
            "fileInputAfterCount": 1,
            "newFileInputCount": 0,
            "fileInputsBefore": [{"signature":"input||file-local|||"}],
            "fileInputsAfter": [{"signature":"input||file-local|||"}],
            "activationEventTrace": [{"type":"click","isTrusted":True,"pathIncludesActivationTarget":True}],
            "fileChooserEventObserved": False,
            "fileAssigned": False,
            "promptDispatched": False,
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    assert session.bind_chatgpt_page().ok
    result = observe_attachment_transport(session, timeout_seconds=1)
    assert not result.ok
    assert result.evidence is not None
    menu = result.evidence.details["upload_menu"]
    assert menu["activation_target_reason"] == "ranked-causal-row-target"
    assert menu["activation_target_candidate_count"] == 3
    assert menu["activation_target_selected_rank"] == 1
    assert menu["activation_target_selected_source"] == "ancestor"
    assert menu["activation_target_selected_tag"] == "div"
    assert menu["activation_target_selected_signature"] == "radix-row-signature"
    diagnostic = result.evidence.details["activation_diagnostic"]
    assert diagnostic["failure_mode"] == "trusted-click-delivered-no-picker-invocation"
    assert diagnostic["trusted_click_event_count"] == 1
    assert diagnostic["file_input_click_method_calls"] == 0
    assert diagnostic["file_input_before_count"] == 1
    assert diagnostic["file_input_after_count"] == 1
    assert diagnostic["target_visible_after"] is True
    assert diagnostic["event_trace"][0]["isTrusted"] is True
    assert "upload_activation_selected_source=ancestor" in result.evidence.observations
    assert "activation_failure_mode=trusted-click-delivered-no-picker-invocation" in result.evidence.observations


def test_real_playwright_native_show_open_file_picker_bridge_delivers_exact_local_file(tmp_path) -> None:
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.composer import _ATTACHMENT_ACTION_SELECT_SCRIPT, _UPLOAD_MENU_ACTION_SELECT_SCRIPT

    attachment = tmp_path / "M1-WEBFIX-009_attachment_smoke.md"
    expected_text = "# SHIRABE attachment smoke\nmarker: WEBFIX009-BRIDGE-OK\n"
    attachment.write_text(expected_text, encoding="utf-8")
    html = r'''<!doctype html><html><body>
      <div class="composer-shell">
        <button id="add" data-testid="composer-plus-btn" aria-label="Add files and more">+</button>
        <div><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
      </div>
      <div id="tools" style="display:none">
        <div id="upload-copy" tabindex="0" style="cursor:pointer">Add photos &amp; files<span>Upload from computer</span></div>
        <div tabindex="0">Add from library<span>Browse and search your files</span></div>
      </div>
      <div id="attached"></div>
      <script>
        window.showOpenFilePicker = async () => { throw new Error('native picker must be bridged'); };
        window.__shirabeOriginalPicker = window.showOpenFilePicker;
        document.getElementById('add').addEventListener('click', () => {
          setTimeout(() => { document.getElementById('tools').style.display='block'; }, 30);
        });
        document.getElementById('upload-copy').addEventListener('click', async () => {
          const handles = await window.showOpenFilePicker({multiple:true, types:[{description:'Markdown'}]});
          const files = [];
          for (const handle of handles) files.push(await handle.getFile());
          window.__shirabeReceivedName = files[0].name;
          window.__shirabeReceivedText = await files[0].text();
          document.getElementById('attached').textContent = files[0].name;
        });
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            result = ops.upload_files_to_composer(
                [str(attachment)],
                attachment_action_script=_ATTACHMENT_ACTION_SELECT_SCRIPT,
                upload_menu_action_script=_UPLOAD_MENU_ACTION_SELECT_SCRIPT,
                timeout_ms=1200,
            )
            assert result["ok"] is True
            assert result["pickerTransport"] == "native-show-open-file-picker-bridge"
            assert result["nativePickerBridgeArmOk"] is True
            assert result["nativePickerBridgeCalls"] == 1
            assert result["nativePickerBridgeGetFileCalls"] == 1
            assert result["nativePickerBridgeReturnedCount"] == 1
            assert result["nativePickerBridgeReturnedNames"] == [attachment.name]
            assert result["fileAssigned"] is True
            assert page.evaluate("window.__shirabeReceivedName") == attachment.name
            assert page.evaluate("window.__shirabeReceivedText") == expected_text
            assert page.locator("#attached").inner_text() == attachment.name
            assert page.evaluate("window.showOpenFilePicker === window.__shirabeOriginalPicker") is True
        finally:
            browser.close()


def test_real_playwright_attach_files_establishes_filename_postcondition_through_native_bridge(tmp_path) -> None:
    import hashlib
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.session import _BoundPage
    from chatgpt_web_api.models import PageIdentity

    attachment = tmp_path / "M1-WEBFIX-009_attachment_smoke.md"
    attachment.write_text("# smoke\nmarker: WEBFIX009-ATTACHMENT-MARKER-V1\n", encoding="utf-8")
    html = r'''<!doctype html><html><body>
      <div class="composer-shell">
        <button id="add" data-testid="composer-plus-btn" aria-label="Add files and more">+</button>
        <div><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
        <div id="chips"></div>
      </div>
      <div id="tools" style="display:none">
        <div id="upload-copy" tabindex="0" style="cursor:pointer">Add photos &amp; files<span>Upload from computer</span></div>
        <div tabindex="0">Add from library<span>Browse and search your files</span></div>
      </div>
      <script>
        window.showOpenFilePicker = async () => { throw new Error('native picker must be bridged'); };
        document.getElementById('add').addEventListener('click', () => setTimeout(() => { document.getElementById('tools').style.display='block'; }, 20));
        document.getElementById('upload-copy').addEventListener('click', async () => {
          const handles = await window.showOpenFilePicker({multiple:true});
          for (const handle of handles) {
            const file = await handle.getFile();
            const chip = document.createElement('span'); chip.textContent = file.name; document.getElementById('chips').appendChild(chip);
          }
          document.getElementById('tools').style.display='none';
        });
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            session = make_bound_session(transport=FakeTransport([]))
            session._connected = True
            session._bound = _BoundPage(ops=ops, identity=PageIdentity(url="https://chatgpt.com/"))
            payload = attachment.read_bytes()
            result = attach_files(
                session,
                ChatIdentity(project_key="g-p-test", url="https://chatgpt.com/"),
                ({
                    "path": str(attachment),
                    "filename": attachment.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "media_type": "text/markdown",
                    "size_bytes": len(payload),
                },),
                timeout_seconds=3,
                poll_interval_seconds=0.02,
            )
            assert result.ok
            assert result.evidence is not None
            assert "upload.picker_transport=native-show-open-file-picker-bridge" in result.evidence.observations
            assert "upload.native_picker_bridge_calls=1" in result.evidence.observations
            assert page.locator("#chips").inner_text() == attachment.name
        finally:
            browser.close()


def test_attach_files_timeout_preserves_attachment_entity_delta_diagnostics(tmp_path) -> None:
    import hashlib

    context = tmp_path / "context.md"
    context.write_text("# context\n", encoding="utf-8")
    raw = context.read_bytes()
    page = FakePage(
        durable_chat().url,
        composer_observations=[composer(empty=True)],
        attachment_baselines=[{
            "ok": False,
            "reason": "attachment-entity-identity-pending",
            "observed": [], "expectedCount": 1, "observedCount": 0,
            "rootReason": "closest-form", "scannedVisibleCount": 8,
            "candidateCount": 1, "candidateSignatures": ["baseline-card"],
            "candidates": [{"signature": "baseline-card", "attachmentLike": True}],
            "newCandidateCount": 0, "newCandidates": [],
            "newAttachmentCandidateCount": 0, "newAttachmentCandidates": [],
        }],
        attachment_observations=[{
            "ok": False,
            "reason": "attachment-entity-identity-pending",
            "observed": [], "expectedCount": 1, "observedCount": 0,
            "rootReason": "closest-form", "scannedVisibleCount": 11,
            "candidateCount": 2,
            "candidates": [{"signature": "baseline-card", "attachmentLike": True}, {"signature": "new-chip", "attachmentLike": True, "title": "uploaded markdown"}],
            "newCandidateCount": 1,
            "newCandidates": [{"signature": "new-chip", "attachmentLike": True, "title": "uploaded markdown"}],
            "newAttachmentCandidateCount": 1,
            "newAttachmentCandidates": [{"signature": "new-chip", "attachmentLike": True, "title": "uploaded markdown"}],
        }],
    )
    session = make_bound_session(transport=FakeTransport([page]))
    result = attach_files(
        session,
        durable_chat(),
        ({
            "path": str(context), "filename": context.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "text/markdown", "size_bytes": len(raw),
        },),
        timeout_seconds=0,
    )
    assert not result.ok
    assert result.failure == FailureKind.TIMEOUT
    assert result.evidence is not None
    assert "attachment_entity.new_attachment_candidate_count=1" in result.evidence.observations
    assert result.evidence.details["newAttachmentCandidateCount"] == 1
    assert result.evidence.details["baseline"]["candidateSignatures"] == ["baseline-card"]


def test_real_playwright_attach_files_observes_filename_from_attachment_entity_title(tmp_path) -> None:
    import hashlib
    import pytest
    from playwright.sync_api import sync_playwright
    from chatgpt_web_api.session import _BoundPage
    from chatgpt_web_api.models import PageIdentity

    attachment = tmp_path / "M1-WEBFIX-009_attachment_smoke.md"
    attachment.write_text("# smoke\nmarker: WEBFIX009-ATTACHMENT-MARKER-V1\n", encoding="utf-8")
    html = r'''<!doctype html><html><body>
      <form class="composer-shell">
        <div id="attachment-zone"></div>
        <button id="add" type="button" data-testid="composer-plus-btn" aria-label="Add files and more">+</button>
        <div><div id="prompt-textarea" role="textbox" contenteditable="true"></div></div>
      </form>
      <div id="tools" style="display:none">
        <div id="upload-copy" tabindex="0" style="cursor:pointer">Add photos &amp; files<span>Upload from computer</span></div>
        <div tabindex="0">Add from library<span>Browse and search your files</span></div>
      </div>
      <script>
        window.showOpenFilePicker = async () => { throw new Error('native picker must be bridged'); };
        document.getElementById('add').addEventListener('click', () => setTimeout(() => { document.getElementById('tools').style.display='block'; }, 20));
        document.getElementById('upload-copy').addEventListener('click', async () => {
          const handles = await window.showOpenFilePicker({multiple:true});
          for (const handle of handles) {
            const file = await handle.getFile();
            const card = document.createElement('div');
            card.className = 'attachment-card';
            card.setAttribute('title', file.name);
            card.setAttribute('data-attachment-name', file.name);
            card.textContent = 'Markdown attachment';
            document.getElementById('attachment-zone').appendChild(card);
          }
          document.getElementById('tools').style.display='none';
        });
      </script>
    </body></html>'''

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        except Exception as exc:
            pytest.skip(f"local chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            ops = isolated_page_ops(_FakeContext(), page)
            session = make_bound_session(transport=FakeTransport([]))
            session._connected = True
            session._bound = _BoundPage(ops=ops, identity=PageIdentity(url="https://chatgpt.com/"))
            payload = attachment.read_bytes()
            result = attach_files(
                session,
                ChatIdentity(project_key="g-p-test", url="https://chatgpt.com/"),
                ({
                    "path": str(attachment), "filename": attachment.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "media_type": "text/markdown", "size_bytes": len(payload),
                },),
                timeout_seconds=3,
                poll_interval_seconds=0.02,
            )
            assert result.ok
            assert result.evidence is not None
            assert "attachment_postcondition_mode=exact-entity-identity" in result.evidence.observations
            assert result.evidence.details["observed"] == [attachment.name]
            matches = result.evidence.details["matchDetails"][attachment.name]
            assert any(match["title"] == attachment.name for match in matches)
            assert result.evidence.details["newAttachmentCandidateCount"] >= 1
        finally:
            browser.close()
